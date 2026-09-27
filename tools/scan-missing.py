#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scan-missing.py —— 扫描已落盘/已接入的数据，给每条序列做缺失分类。

它做什么
--------
1. **声明式清单**：对本项目已接入的源头（NBS / World Bank / IMF / FRED / BIS）逐条构造长表序列，
   过 missing.classify_missing，得到缺口与分类。
2. **落盘扫描**：再扫 data/validated/ 下所有形如 {"rows": [...]} 的长表 JSON，
   同样分类（同一序列在"声明式清单"里已出现的，不重复计）。
3. 汇总：按 classification 分组统计，写 data/validated/missing_report.json。

为什么要"声明式清单"而不是只扫 data/validated/
----------------------------------------------
因为目前只有 normalize 自检写过一份 `data/validated/nbs/selftest_*.json`；
其余序列（CPI、登记/调查失业率、IMF LUR 等）是各对比脚本现取现用的，**没有落盘长表**。
只扫目录会漏掉它们，而它们恰恰是有分类价值的那些。所以两者都做。

窗口（expected_periods）为什么必须显式给
----------------------------------------
`series_start`（起点晚）只有在期望网格比观测范围更宽时才可能存在。本脚本对年度序列
统一给 2015-2024 的窗口，于是：
  * NBS 全国城镇调查失业率（实测 2018 起）-> series_start(2015..2017)
  * IMF LUR（实测 2017 起）              -> series_start(2015..2016)

断言（供 tools/run-all-checks.py 当门禁用）
------------------------------------------
两条已知事实必须被重算出来，否则 exit 1：
  * NBS 城镇登记失业率：含一条 `discontinued`（2022 起停更）
  * NBS 全国城镇调查失业率：含一条 `series_start`

**本脚本不填任何缺失**，只分类。动作建议见 missing.RECOMMENDED_ACTIONS。

用法
----
    .\\.venv\\Scripts\\python.exe tools\\scan-missing.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from econ_core import (  # noqa: E402
    bis_client, fred_client, imf_client, missing, nbs_client, normalize, worldbank_client,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
VALIDATED_DIR = PROJECT_ROOT / "data" / "validated"
OUT_PATH = VALIDATED_DIR / "missing_report.json"

YEARS: list[int] = list(range(2015, 2025))
Y_WINDOW: list[str] = [str(y) for y in YEARS]
PERIODS: list[str] = [f"{y}YY" for y in YEARS]

REGION_CODE = "000000000000"


def _month_window(y0: int, m0: int, y1: int, m1: int) -> list[str]:
    """生成闭区间 ``YYYY-MM`` 月度窗口（含首尾）。"""
    out: list[str] = []
    y, m = y0, m0
    while (y, m) <= (y1, m1):
        out.append(f"{y}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out

#: FRED CPI（OECD 派生）：月度指数 2015=100，本项目按年均值聚合到年
FRED_CPI = {
    "series_id": "CHNCPIALLMINMEI",
    "display_name": "Consumer Price Index: All Items: Total for China（指数）",
    "unit": "指数（2015=100）",
}

#: BIS 中国 CPI 两条（方向 C 第三轮接入）。**月度原生粒度，不聚合**，理由：
#:   ① BIS 的价值就在 1995 起的月度覆盖（380/368 期）；聚合到年只剩 31/32 行，
#:      恰好把接它的理由丢掉一半。本项目已支持月度序列（调查失业率就是月度）。
#:   ② 拼接器的断点检测在月度分辨率下才有意义 —— 年度 3 点斜率会横跨拼接点两侧
#:      好几年的真实变化，噪声大到判不出台阶。
#:   ③ 想跟年度 CPI 对比时可以在**对比脚本**里年化；反过来从年度恢复月度不可能。
#:      保留信息量大的形态是单向安全的选择。
#: 两条的窗口各自独立：628（指数）比 771（同比）多 1995 全年 12 期 —— 同比需要上年基期，
#: 所以 628 从 1995-01 起、771 从 1996-01 起。给同一个窗口会让 771 头部多出 12 期
#: 「缺行」（不是缺值），凭空造出一个 series_start，所以窗口按各自的实测范围声明。
BIS_CPI = [
    {
        "key": "bis|WS_LONG_CPI|M.CN.771",
        "unit": "771",
        "display_name": "中国 CPI 同比（月度，BIS 转载）",
        "unit_label": "%",
        "window": _month_window(1996, 1, 2026, 8),
    },
    {
        "key": "bis|WS_LONG_CPI|M.CN.628",
        "unit": "628",
        "display_name": "中国 CPI 指数（月度，BIS 转载并重定基 2010=100）",
        "unit_label": "指数（2010=100）",
        "window": _month_window(1995, 1, 2026, 8),
    },
]

# --- 已知取数参数（全部来自前几轮实测） ---
NBS_GDP = {"cid": "f7fd25aaad184414875632cf2327da60",
           "tree_node": "7dc6a2ee6c614960b7059991e0cc4d96",
           "root_id": "71d41888d5a44bb2a67402ef4e60003e"}
NBS_REG = {"cid": "19839dbd8e82481b9524f031c6d816c5",
           "tree_node": "456ef2f3d75f472b930384237bc6100f",
           "root_id": "71d41888d5a44bb2a67402ef4e60003e"}
NBS_SUR = {"cid": "ee3b7046b390415b9b7745e3d16f6052",
           "tree_node": "3888eac6062945a79c8a27e5f13d4953",
           "root_id": "3c9c459384c74f578f3541b2198aac70"}
WB_GDP = {"country": "CHN", "indicator": "NY.GDP.MKTP.CN", "date_range": "2015:2024"}
IMF_SERIES = [("NGDPD", "GDP, current prices", "十亿美元"),
              ("LUR", "Unemployment rate", "%")]


def _parse(v: Any) -> Optional[float]:
    """宽容解析：字符串数字 -> float；空/null -> None；解析不了也不抛。"""
    try:
        return normalize.parse_value(v)
    except normalize.NormalizeError:
        return None


def _nbs_rows(p: dict[str, Any], dts: list[str]) -> list[dict[str, Any]]:
    """取 NBS 年度/月度序列并 normalize 成行。"""
    raw = nbs_client.fetch_indicator_data(
        cid=p["cid"], indicator_id=p["tree_node"], root_id=p["root_id"],
        da=REGION_CODE, dts=dts,
    )
    req = {"cid": p["cid"], "id": p["tree_node"], "da": REGION_CODE,
           "dt": "", "rootId": p["root_id"], "dts": dts}
    meta = normalize.source_meta_from_parsed(
        "getEsDataByIndicatorIdAndDa", req,
        region_name="全国", tree_node_id=p["tree_node"],
    )
    return normalize.normalize_observations(raw, meta)


def _pad_to_window(rows: list[dict[str, Any]], window: list[str],
                   period_type: str = "annual") -> list[dict[str, Any]]:
    """把序列补齐到期望窗口：窗口内源数据没有观测行的期，补一行 value=None。

    为什么要补：不补的话，缺口里"完全没有行"的那几期会被 fill_strategy **现场合成**
    占位行（只有 period/value/缺失元数据），于是没有 row_sha16 —— 导出层的
    "每行都有指纹"就做不到（实测 147/152）。补在 validated 层更诚实：
    这些行有完整的指标元数据 + 指纹，且 raw_fields 里标了 padded=true 说明来历。

    补行会改内容，所以 row_sha16 必须**重算**。
    """
    if not rows or not window:
        return rows
    have = {str(r.get("period")) for r in rows}
    missing = [p for p in window if p not in have]
    if not missing:
        return rows
    tpl = rows[0]
    out = list(rows)
    for p in missing:
        row = dict(tpl)
        row["period"] = p
        row["period_type"] = period_type
        row["value"] = None
        row["raw_fields"] = json.dumps(
            {"padded": True, "reason": "期望窗口内该期源数据没有观测行（补齐以便逐行可追溯）"},
            ensure_ascii=False, sort_keys=True)
        row["row_sha16"] = normalize._row_sha16(row)
        out.append(row)
    out.sort(key=lambda r: str(r.get("period")))
    return out


def build_declared() -> list[dict[str, Any]]:
    """构造声明式清单：每条 {label, rows, meta, expect(可选)}。"""
    out: list[dict[str, Any]] = []

    # 1) NBS GDP（年度）
    out.append({
        "label": "NBS 国内生产总值 (亿元) 2015-2024",
        "rows": _nbs_rows(NBS_GDP, PERIODS),
        "meta": {"expected_periods": Y_WINDOW, "series_key": "nbs|gdp|cny_100m"},
    })

    # 2) NBS CPI（默认指标 code=21）—— **改走统一入口** normalize.normalize_cpi_wide
    #    以前是就地手搓 {period, value}，导致没有 row_sha16 / indicator_id / unit。
    cpi = nbs_client.get_default_indicator(21)
    cpi_meta = normalize.source_meta_from_parsed(
        "getDefaultIndicData", {"code": 21}, region_name="全国")
    cpi_rows = normalize.normalize_cpi_wide(cpi.get("xData"), cpi.get("yData"), {
        "source": "nbs",
        "region_code": REGION_CODE,
        "region_name": "全国",
        "catalog_id": cpi.get("catalogId"),
        "catalog_name": cpi.get("catalogName"),
        "fetched_at": cpi_meta.get("fetched_at", ""),
        "raw_cache": cpi_meta.get("raw_cache", ""),
    })
    xlabels = sorted({str(r["period"]) for r in cpi_rows})
    cpi_groups: dict[str, list[dict[str, Any]]] = {}
    for r in cpi_rows:
        cpi_groups.setdefault(str(r["indicator_name"]), []).append(r)
    for name in sorted(cpi_groups):
        out.append({
            "label": f"NBS CPI 居民消费价格指数[{name}] "
                     f"{xlabels[0] if xlabels else '?'}-{xlabels[-1] if xlabels else '?'}",
            "rows": cpi_groups[name],
            "meta": {"expected_periods": xlabels, "series_key": f"nbs|cpi|{name}"},
        })

    # 3) NBS 城镇登记失业率（年度；实测 2022 起为空）
    out.append({
        "label": "NBS 城镇登记失业率 (%) 2015-2024",
        "rows": _nbs_rows(NBS_REG, PERIODS),
        "meta": {"expected_periods": Y_WINDOW, "series_key": "nbs|registered_unemployment"},
        "expect": "discontinued",
    })

    # 4) NBS 全国城镇调查失业率（月度 -> 年均值；实测 2018 起）
    #    **月度行先过 normalize**（于是有 row_sha16 / unit / raw_cache / raw_fields），
    #    再按年聚合；聚合行的 period/value 都变了，所以 row_sha16 必须**重算**，
    #    并留下 aggregated_from_months 说明来源。
    monthly = [f"{y}{m:02d}MM" for y in range(2017, 2025) for m in range(1, 13)]
    mrows = _nbs_rows(NBS_SUR, monthly)
    by_year: dict[str, list[dict[str, Any]]] = {}
    for r in mrows:
        by_year.setdefault(str(r["period"])[:4], []).append(r)
    arows: list[dict[str, Any]] = []
    for year in sorted(by_year):
        rs = by_year[year]
        vals = [float(r["value"]) for r in rs
                if isinstance(r.get("value"), (int, float))
                and not isinstance(r.get("value"), bool)]
        if not vals:
            continue
        row = dict(rs[0])                 # 以月度行为模板，继承 source/unit/raw_cache/fetched_at
        row["period"] = year
        row["period_type"] = "annual"
        row["value"] = sum(vals) / len(vals)
        row["aggregated_from_months"] = len(vals)
        row["raw_fields"] = json.dumps(
            {"aggregated_from_months": len(vals),
             "months": [str(r["period"]) for r in rs],
             "note": "由月度序列算术平均得到；row_sha16 已按聚合后的行重算"},
            ensure_ascii=False, sort_keys=True)
        row["row_sha16"] = normalize._row_sha16(row)   # 聚合后重算（用 missing/normalize 同一套指纹规则）
        arows.append(row)
    arows = _pad_to_window(arows, Y_WINDOW)          # 2015-2017 源数据没有观测行 -> 补 None 行
    out.append({
        "label": "NBS 全国城镇调查失业率 (%) 月均->年 2015-2024 窗口",
        "rows": arows,
        "meta": {"expected_periods": Y_WINDOW, "series_key": "nbs|surveyed_unemployment"},
        "expect": "series_start",
    })

    # 5) World Bank GDP（本币）
    wraw = worldbank_client.fetch_indicator(WB_GDP["country"], WB_GDP["indicator"],
                                            WB_GDP["date_range"])
    wreq = worldbank_client.indicator_request(WB_GDP["country"], WB_GDP["indicator"],
                                              WB_GDP["date_range"])
    wmeta = normalize.source_meta_from_parsed_worldbank("country_indicator", wreq)
    # 8) FRED CPI（OECD 派生；月度指数 2015=100 -> 按年均值聚合到年）
    #    与上面 NBS 调查失业率同一套做法：先聚合、再重算 row_sha16、留 aggregated_from_months。
    #    只有满 12 个月的年份进序列（2025 年 FRED 只到 4 月，被排除）。
    frows = fred_client.fetch_series(FRED_CPI["series_id"])
    fmeta = fred_client.last_meta()
    fbuckets: dict[str, list[float]] = {}
    for r in frows:
        v = _parse(r.get("value"))
        if v is not None:
            fbuckets.setdefault(str(r["period"])[:4], []).append(v)
    farows: list[dict[str, Any]] = []
    for year in Y_WINDOW:
        vals = fbuckets.get(year, [])
        if len(vals) < 12:
            continue
        farows.append(normalize._mk_row(
            REGION_CODE, "全国", FRED_CPI["series_id"], "",
            FRED_CPI["display_name"], year, "annual",
            sum(vals) / len(vals), FRED_CPI["unit"], "fred",
            str(fmeta.get("fetched_at", "")), str(fmeta.get("raw_cache", "")),
            {"series_id": FRED_CPI["series_id"], "aggregated_from_months": len(vals),
             "note": "由 FRED 月度指数（2015=100）算术平均得到；row_sha16 已按聚合后的行重算"},
            extra={"aggregated_from_months": len(vals)}))
    out.append({
        "label": f"FRED {FRED_CPI['series_id']} 月均->年 2015-2024",
        "rows": farows,
        "meta": {"expected_periods": Y_WINDOW,
                 "series_key": f"fred|{FRED_CPI['series_id']}"},
    })

    out.append({
        "label": f"World Bank {WB_GDP['indicator']} (LCU) 2015-2024",
        "rows": normalize.normalize_worldbank_observations(wraw, wmeta),
        "meta": {"expected_periods": Y_WINDOW,
                 "series_key": f"worldbank|{WB_GDP['indicator']}"},
    })

    # 6/7) IMF 两条 —— **改走统一入口** normalize.normalize_imf_observations
    #    （以前直通 {period, value}，所以没有 row_sha16 / indicator_id / indicator_name / unit）
    for code, desc, unit in IMF_SERIES:
        imf_meta = imf_client.last_meta()
        raw_imf = imf_client.fetch_indicator(code, "CHN")
        imf_meta = imf_client.last_meta()       # fetch 之后再取一次，拿本次的 raw_cache/fetched_at
        out.append({
            "label": f"IMF {code} {desc}",
            "rows": normalize.normalize_imf_observations(raw_imf, {
                "indicator": code,
                "country": "CHN",
                "indicator_name": desc,
                "unit": unit,
                "region_name": "China",
                "source": "imf",
                "fetched_at": imf_meta.get("fetched_at", ""),
                "raw_cache": imf_meta.get("raw_cache", ""),
            }),
            "meta": {"expected_periods": Y_WINDOW, "series_key": f"imf|{code}"},
        })
        # LUR 实测从 2017 起 -> 2015/2016 补 None 行（见 _pad_to_window 的说明）
        out[-1]["rows"] = _pad_to_window(out[-1]["rows"], Y_WINDOW)

    # 9/10) BIS 中国 CPI 两条（月度原生粒度，**不聚合** —— 理由见 BIS_CPI 的注释）
    #    为什么走 normalize._mk_row 而不是某个 normalize_*_observations 入口：
    #    现有三个入口分别对应 NBS / World Bank / IMF 的响应形状，SDMX-JSON 是第四种；
    #    为它加一个入口要动 normalize 层（本轮约束不允许）。BIS 的响应里只有
    #    period/value，指标元数据本来就得由调用方给 —— 与 IMF 的情形同构，
    #    所以复用同一套 _mk_row（14 列 + row_sha16），**不新造行形状**。
    for spec in BIS_CPI:
        unit = str(spec["unit"])
        raw = bis_client.fetch_cpi(unit=unit, freq="M", country="CN")
        bis_meta = bis_client.last_meta()    # fetch 之后再取，拿本次的 raw_cache/fetched_at
        brows: list[dict[str, Any]] = []
        for r in raw:
            brows.append(normalize._mk_row(
                REGION_CODE, "中国", f"WS_LONG_CPI|M.CN.{unit}", "",
                spec["display_name"], str(r["period"]), "monthly",
                _parse(r.get("value")), str(spec["unit_label"]), "bis",
                str(bis_meta.get("fetched_at", "")), str(bis_meta.get("raw_cache", "")),
                {"dataset": "WS_LONG_CPI", "key": f"M.CN.{unit}",
                 "unit_meaning": bis_meta.get("unit_meaning", ""),
                 "note": "BIS 转载 NBS 并做拼接 + 重定基；见 source_profiles.yaml 的 bis 条目"}))
        out.append({
            "label": f"BIS WS_LONG_CPI M.CN.{unit} 月度 "
                     f"{spec['window'][0]}~{spec['window'][-1]}",
            "rows": brows,
            "meta": {"expected_periods": spec["window"], "series_key": spec["key"]},
        })

    return out


# --------------------------------------------------------------------------- #
# 落盘长表扫描
# --------------------------------------------------------------------------- #

def scan_materialized() -> list[dict[str, Any]]:
    """扫 data/validated/ 下所有形如 {"rows": [...]} 的长表 JSON。

    **series_key 取值优先级：产物自带的 `series_key` > 从行推 `source|region_code|indicator_id`。**

    为什么必须让产物自带的键优先（实测修的一个静默 bug）：`tools/materialize-validated.py`
    落盘时写的是**声明式清单的规范键**（如 `bis|WS_LONG_CPI|M.CN.771`、`nbs|gdp|cny_100m`），
    而 `missing._auto_series_key` 推出来的是 `source|region_code|indicator_id`
    （如 `bis|000000000000|WS_LONG_CPI|M.CN.771`）——**两者对所有落盘文件都不一样**。
    于是主流程里「按 series_key 跳过已覆盖的」这层去重**从来没生效过**：
    实测修之前 12 个落盘文件里 **12 个都和声明式清单对不上**，同一序列被扫两遍。

    这也是本项目已经确立的约定：`fill_strategy._scan_validated_series()` 同样是
    「落盘文件的 series_key 字段优先，否则才 `_auto_series_key` 推导」
    （见 source_profiles.yaml 的 fields.series_key 说明）。本函数之前没跟上。

    这些序列**不给 expected_periods**，因此只判"数据内部"的缺口，
    不会产生 series_start —— 落盘文件里没有"期望窗口"这个信息。
    """
    if not VALIDATED_DIR.is_dir():
        return []

    grouped: dict[str, dict[str, Any]] = {}
    for f in sorted(VALIDATED_DIR.rglob("*.json")):
        if f.resolve() == OUT_PATH.resolve():
            continue
        try:
            obj = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(obj, dict) or not isinstance(obj.get("rows"), list):
            continue
        rel = str(f.relative_to(PROJECT_ROOT))
        # 产物自带的规范键（materialize-validated 写的）优先；没有才退回自动推导。
        declared_key = str(obj.get("series_key") or "").strip()
        for r in obj["rows"]:
            # 必须是**长表行**：period 与 value 两个键都要有。
            # 否则会把对比脚本的结果行（如 gdp_3way 的 rows 只有 period +
            # nbs_usd_100m/... 而没有 value）误当成序列，整段判成缺失（实测踩过）。
            if not isinstance(r, dict) or "period" not in r or "value" not in r:
                continue
            key = declared_key or missing._auto_series_key([r])
            entry = grouped.setdefault(key, {"file": rel, "rows": []})
            entry["rows"].append({"period": r.get("period"), "value": r.get("value")})

    return [
        {"label": f"{v['file']} :: {k}", "rows": v["rows"],
         "meta": {"series_key": k}, "source_file": v["file"]}
        for k, v in grouped.items()
    ]


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def _section(title: str) -> None:
    print()
    print("=" * 100)
    print(title)
    print("=" * 100)


def _print_result(res: dict[str, Any]) -> None:
    s = res["summary"]
    print(f"  {res['label']}")
    print(f"    series_key={res['series_key']}")
    print(f"    网格 {s['period_range'][0]}..{s['period_range'][-1]}"
          f"（{s['period_kind']}，{s['n_periods']} 期）"
          f"  有值={s['n_ok']}  缺失={s['n_missing']}"
          f"（显式 null {s['n_explicit_null']} + 缺行 {s['n_absent_rows']}）")
    if not res["gaps"]:
        print("    （无缺口）")
    for g in res["gaps"]:
        print(f"    缺口 {g['start']}..{g['end']}  n={g['n_missing']}"
              f"（约 {g['span_years']} 年）-> {g['classification']}"
              f"  建议动作={g['recommended_action']}")
        print(f"        evidence: {g['evidence']}")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    _section("[1] 缺失分类规则（先分类，再决定补不补）")
    print(f"  discontinued      尾部缺失且 >= {missing.DISCONTINUED_MIN_YEARS:g} 年 -> leave_null")
    print("  not_yet_published 尾部缺失且 <  3 年           -> wait")
    print("  series_start      头部缺失                     -> leave_null")
    print("  true_gap          中段空洞                     -> interpolate")
    print("  优先级：先判尾部（>=3 年 / <3 年），再判头部，最后中段。")
    print("  说明：series_start 需要显式 expected_periods；年度序列统一给 2015-2024 窗口。")

    declared = build_declared()
    materialized = scan_materialized()

    results: list[dict[str, Any]] = []
    problems: list[str] = []

    _section(f"[2] 声明式清单（已接入源，共 {len(declared)} 条序列）")
    for item in declared:
        res = missing.classify_missing(item["rows"], item["meta"])
        res["label"] = item["label"]
        res["group"] = "declared"
        results.append(res)
        _print_result(res)
        if item.get("expect"):
            got = [g["classification"] for g in res["gaps"]]
            if item["expect"] not in got:
                problems.append(
                    f"{item['label']}: 期望分类含 {item['expect']!r}，实际 {got}")

    # materialize-validated.py 会把声明式清单落盘到 data/validated/，于是同一序列会同时
    # 出现在"声明式清单"和"落盘扫描"里。docstring 承诺过"已出现的不重复计"，这里兑现它 ——
    # 否则报告里每条序列都有两份、by_classification 统计翻倍（实测踩过）。
    declared_keys = {str((it.get("meta") or {}).get("series_key")) for it in declared}

    _section(f"[3] data/validated/ 落盘长表扫描（{len(materialized)} 条序列）")
    if not materialized:
        print("  （没有找到带 rows 的长表 JSON）")
    for item in materialized:
        key = str((item.get("meta") or {}).get("series_key"))
        if key in declared_keys:
            print(f"  （跳过 {key}：已由声明式清单覆盖，不重复计）")
            continue
        res = missing.classify_missing(item["rows"], item["meta"])
        res["label"] = item["label"]
        res["group"] = "materialized"
        results.append(res)
        _print_result(res)

    _section("[4] 按分类分组统计")
    by_series: dict[str, int] = {}
    by_gaps: dict[str, int] = {}
    for res in results:
        for c in sorted({g["classification"] for g in res["gaps"]}):
            by_series[c] = by_series.get(c, 0) + 1
        for g in res["gaps"]:
            by_gaps[g["classification"]] = by_gaps.get(g["classification"], 0) + 1

    print(f"  序列数={len(results)}  缺口段合计={sum(len(r['gaps']) for r in results)}")
    for c in missing.CLASSIFICATIONS:
        print(f"    {c:<18} 涉及序列 {by_series.get(c, 0):>2} 条 / 缺口段 {by_gaps.get(c, 0):>2} 段"
              f"   -> {missing.RECOMMENDED_ACTIONS[c]}")

    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rules": {
            "priority": ["tail >= 3y -> discontinued", "tail < 3y -> not_yet_published",
                         "head -> series_start", "middle -> true_gap"],
            "discontinued_min_years": missing.DISCONTINUED_MIN_YEARS,
            "recommended_actions": missing.RECOMMENDED_ACTIONS,
            "note": "series_start 需要显式 expected_periods；年度序列用 2015-2024 窗口",
        },
        "n_series": len(results),
        "n_gaps": sum(len(r["gaps"]) for r in results),
        "by_classification_series": by_series,
        "by_classification_gaps": by_gaps,
        "series": [
            {"label": r["label"], "group": r["group"], "series_key": r["series_key"],
             "summary": r["summary"], "gaps": r["gaps"]}
            for r in results
        ],
        "problems": problems,
    }
    VALIDATED_DIR.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  [saved] {OUT_PATH}")

    _section("[5] 断言")
    if problems:
        for p in problems:
            print(f"  [FAIL] {p}", file=sys.stderr)
        return 1
    print("  [PASS] 两条已知事实均被重算出来：登记失业率=discontinued，调查失业率=series_start")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

