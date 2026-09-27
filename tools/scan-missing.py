#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scan-missing.py —— 扫描已落盘/已接入的数据，给每条序列做缺失分类。

它做什么
--------
1. **声明式清单**：对本项目已接入的源头（NBS / World Bank / IMF）逐条构造长表序列，
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

from econ_core import imf_client, missing, nbs_client, normalize, worldbank_client  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
VALIDATED_DIR = PROJECT_ROOT / "data" / "validated"
OUT_PATH = VALIDATED_DIR / "missing_report.json"

YEARS: list[int] = list(range(2015, 2025))
Y_WINDOW: list[str] = [str(y) for y in YEARS]
PERIODS: list[str] = [f"{y}YY" for y in YEARS]

REGION_CODE = "000000000000"

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
IMF_SERIES = [("NGDPD", "GDP, current prices (十亿美元)"),
              ("LUR", "Unemployment rate (%)")]


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


def build_declared() -> list[dict[str, Any]]:
    """构造声明式清单：每条 {label, rows, meta, expect(可选)}。"""
    out: list[dict[str, Any]] = []

    # 1) NBS GDP（年度）
    out.append({
        "label": "NBS 国内生产总值 (亿元) 2015-2024",
        "rows": _nbs_rows(NBS_GDP, PERIODS),
        "meta": {"expected_periods": Y_WINDOW, "series_key": "nbs|gdp|cny_100m"},
    })

    # 2) NBS CPI（默认指标 code=21，宽表 -> 长表；每条 yData 一个序列）
    cpi = nbs_client.get_default_indicator(21)
    xlabels = [str(x).replace("年", "") for x in (cpi.get("xData") or [])]
    for i, y in enumerate(cpi.get("yData") or []):
        name = str(y.get("name") or f"series{i}").strip()
        # 注意：默认指标接口的 yData[].value 是**字符串**数组（如 "101.4"），
        # 且末位可能是 null。必须过 normalize.parse_value，否则会被当成"全缺失"，
        # 进而误判成 discontinued（实测踩过）。
        rows = [{"period": p, "value": _parse(v)}
                for p, v in zip(xlabels, (y.get("value") or []))]
        out.append({
            "label": f"NBS CPI 居民消费价格指数[{name}] {xlabels[0]}-{xlabels[-1] if xlabels else '?'}",
            "rows": rows,
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
    monthly = [f"{y}{m:02d}MM" for y in range(2017, 2025) for m in range(1, 13)]
    mrows = _nbs_rows(NBS_SUR, monthly)
    buckets: dict[str, list[float]] = {}
    for r in mrows:
        v = r.get("value")
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            buckets.setdefault(str(r["period"])[:4], []).append(float(v))
    arows = [{"period": y, "value": sum(vs) / len(vs)} for y, vs in buckets.items()]
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
    out.append({
        "label": f"World Bank {WB_GDP['indicator']} (LCU) 2015-2024",
        "rows": normalize.normalize_worldbank_observations(wraw, wmeta),
        "meta": {"expected_periods": Y_WINDOW,
                 "series_key": f"worldbank|{WB_GDP['indicator']}"},
    })

    # 6/7) IMF 两条
    for code, desc in IMF_SERIES:
        rows = [{"period": str(r["period"]), "value": r.get("value")}
                for r in imf_client.fetch_indicator(code, "CHN")]
        out.append({
            "label": f"IMF {code} {desc}",
            "rows": rows,
            "meta": {"expected_periods": Y_WINDOW, "series_key": f"imf|{code}"},
        })

    return out


# --------------------------------------------------------------------------- #
# 落盘长表扫描
# --------------------------------------------------------------------------- #

def scan_materialized() -> list[dict[str, Any]]:
    """扫 data/validated/ 下所有形如 {"rows": [...]} 的长表 JSON。

    按 `source|region_code|indicator_id` 分组（复用 missing 里同一套键规则，
    保证两处口径一致）。这些序列**不给 expected_periods**，因此只判"数据内部"的缺口，
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
        for r in obj["rows"]:
            # 必须是**长表行**：period 与 value 两个键都要有。
            # 否则会把对比脚本的结果行（如 gdp_3way 的 rows 只有 period +
            # nbs_usd_100m/... 而没有 value）误当成序列，整段判成缺失（实测踩过）。
            if not isinstance(r, dict) or "period" not in r or "value" not in r:
                continue
            key = missing._auto_series_key([r])
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

    _section(f"[3] data/validated/ 落盘长表扫描（{len(materialized)} 条序列）")
    if not materialized:
        print("  （没有找到带 rows 的长表 JSON）")
    for item in materialized:
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

