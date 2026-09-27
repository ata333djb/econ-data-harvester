#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""compare-gdp-3way.py —— NBS vs World Bank vs IMF WEO 三方交叉验证。

为什么要三方
------------
NBS 是国内官方源；World Bank 的 China NY.GDP.MKTP.CN 与 NBS **逐位相同**
（见 tools/compare-gdp.py），所以那两个不构成独立交叉验证。
IMF WEO 是真正独立的源（独立采集方 / 独立修订周期 / 独立口径），
NBS vs IMF 的差异是**真实统计口径分歧**，这正是验证层要处理的东西。

口径统一：全部换到「亿美元」
----------------------------
=================  ==================  ============================================
来源                原始口径             换算
=================  ==================  ============================================
NBS                GDP 本币（亿元）      `亿元 / (LCU/USD) = 亿美元`
World Bank         GDP 本币（元，LCU）   `元 / (LCU/USD) / 1e8 = 亿美元`
IMF WEO (NGDPD)    GDP 现价美元（十亿）  `十亿美元 x 10 = 亿美元`
=================  ==================  ============================================

汇率来源：World Bank `PA.NUS.FCRF`（Official exchange rate, LCU per US$，年度均值）。
**不自己编汇率**，全部走 worldbank_client。

注意：IMF 的 NGDPD 含**预测值**（实测 CHN 覆盖 1980–2031），
本脚本按 YEARS 显式截到实际年份区间，不做无意识的预测值混入。

输出
----
    data/validated/cross_check/gdp_3way_YYYYMMDD.json

退出码：断言全过 -> 0；否则 -> 1（供 tools/run-all-checks.py 当门禁用）。

用法
----
    .\\.venv\\Scripts\\python.exe tools\\compare-gdp-3way.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from econ_core import cross_validation, imf_client, nbs_client, normalize, worldbank_client  # noqa: E402

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

NBS_CID = "f7fd25aaad184414875632cf2327da60"
NBS_INDICATOR_ID = "db8e5a86c08246e79b1b11251927e740"   # 响应里的 i
NBS_TREE_NODE_ID = "7dc6a2ee6c614960b7059991e0cc4d96"   # 请求里的 id
NBS_ROOT_ID = "71d41888d5a44bb2a67402ef4e60003e"
NBS_REGION_CODE = "000000000000"
NBS_REGION_NAME = "全国"
YEARS: list[int] = list(range(2015, 2025))
PERIODS: list[str] = [f"{y}YY" for y in YEARS]

WB_COUNTRY = "CHN"
WB_INDICATOR_LCU = "NY.GDP.MKTP.CN"      # GDP (current LCU)，单位为元
WB_INDICATOR_FX = "PA.NUS.FCRF"          # Official exchange rate (LCU per US$)
WB_DATE_RANGE = "2015:2024"

IMF_INDICATOR = "NGDPD"                  # GDP, current prices（十亿 美元）
IMF_COUNTRY = "CHN"

#: 展示基准单位
BASE_UNIT = "亿美元"

RESULTS_DIR = Path(__file__).resolve().parents[1] / "data" / "validated" / "cross_check"

#: 行判定的严重度排序（用于把两对比较合成一列"判定"）
_RANK: dict[str, int] = {"一致": 0, "可接受": 1, "警告": 2, "冲突": 3, "缺值": 4}

# --------------------------------------------------------------------------- #
# 数据加载
# --------------------------------------------------------------------------- #

def load_nbs_lcu() -> list[dict[str, Any]]:
    """NBS 中国 GDP：本币亿元的长表行。"""
    raw = nbs_client.fetch_indicator_data(
        cid=NBS_CID, indicator_id=NBS_TREE_NODE_ID, root_id=NBS_ROOT_ID,
        da=NBS_REGION_CODE, dts=PERIODS,
    )
    request_params = {"cid": NBS_CID, "id": NBS_TREE_NODE_ID, "da": NBS_REGION_CODE,
                      "dt": "", "rootId": NBS_ROOT_ID, "dts": PERIODS}
    meta = normalize.source_meta_from_parsed(
        "getEsDataByIndicatorIdAndDa", request_params,
        region_name=NBS_REGION_NAME, tree_node_id=NBS_TREE_NODE_ID,
    )
    return normalize.normalize_observations(raw, meta)


def load_wb(indicator: str) -> list[dict[str, Any]]:
    """World Bank 某指标：长表行（单位见 unit 列，GDP 本币那支是「元」）。"""
    raw = worldbank_client.fetch_indicator(WB_COUNTRY, indicator, WB_DATE_RANGE)
    request = worldbank_client.indicator_request(WB_COUNTRY, indicator, WB_DATE_RANGE)
    meta = normalize.source_meta_from_parsed_worldbank("country_indicator", request)
    return normalize.normalize_worldbank_observations(raw, meta)


def load_imf_usd() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """IMF NGDPD。

    :returns: (截到 YEARS 的行, 未截的完整行) —— 后者用于展示"含预测值"这件事。
    """
    full = imf_client.fetch_indicator(IMF_INDICATOR, IMF_COUNTRY)
    want = {str(y) for y in YEARS}
    return [r for r in full if str(r.get("period")) in want], full


# --------------------------------------------------------------------------- #
# 换算工具
# --------------------------------------------------------------------------- #

def _rate_map(rows: list[dict[str, Any]]) -> dict[str, float]:
    """{period: LCU per US$}，只收正数。"""
    out: dict[str, float] = {}
    for r in rows:
        p = str(r.get("period"))
        v = r.get("value")
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v:
            out[p] = float(v)
    return out


def _convert(rows: list[dict[str, Any]], rate_map: dict[str, float],
             factor: float) -> list[dict[str, Any]]:
    """value -> value / rate * factor；没有汇率、或值非数值的年份丢弃。"""
    out: list[dict[str, Any]] = []
    for r in rows:
        p = str(r.get("period"))
        rate = rate_map.get(p)
        v = r.get("value")
        if not rate or not isinstance(v, (int, float)) or isinstance(v, bool):
            continue
        out.append({"period": p, "value": float(v) / rate * factor})
    return out


def _fmt_num(v: Any, width: int = 16) -> str:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return f"{v:>{width},.1f}"
    return f"{'—':>{width}}"


def _fmt_rate(v: Any, width: int = 12) -> str:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return f"{v * 100:>{width},.2f}%"
    return f"{'—':>{width}}"


def _worst(a: Optional[str], b: Optional[str]) -> str:
    """两对比较的判定取更严重的一个（一致 < 可接受 < 警告 < 冲突 < 缺值）。"""
    cands = [x for x in (a, b) if x]
    if not cands:
        return "—"
    return max(cands, key=lambda x: _RANK.get(x, 9))


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def _section(title: str) -> None:
    print()
    print("=" * 100)
    print(title)
    print("=" * 100)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    # ---- 1) NBS ----
    _section("[1] NBS：GDP 本币（亿元）")
    nbs_rows = load_nbs_lcu()
    print(f"  行数  : {len(nbs_rows)}")
    print(f"  unit  : {sorted({str(r.get('unit')) for r in nbs_rows})}")
    print(f"  source: {nbs_rows[0].get('source') if nbs_rows else '-'}")

    # ---- 2) World Bank 本币 ----
    _section(f"[2] World Bank：{WB_INDICATOR_LCU}（GDP 本币，LCU = 元）")
    wb_rows = load_wb(WB_INDICATOR_LCU)
    print(f"  行数  : {len(wb_rows)}")
    print(f"  unit  : {sorted({str(r.get('unit')) for r in wb_rows})}（空串，单位写在指标名里）")
    print(f"  source: {wb_rows[0].get('source') if wb_rows else '-'}")

    # ---- 3) World Bank 汇率 ----
    _section(f"[3] World Bank：{WB_INDICATOR_FX}（Official exchange rate, LCU per US$）")
    fx_rows = load_wb(WB_INDICATOR_FX)
    rates = _rate_map(fx_rows)
    print(f"  行数  : {len(fx_rows)}  有效汇率年份: {len(rates)}")
    for p in sorted(rates):
        print(f"    {p}: {rates[p]:.4f}")

    # ---- 4) IMF ----
    _section(f"[4] IMF WEO：{IMF_INDICATOR}（GDP 现价美元，十亿）")
    imf_rows, imf_full = load_imf_usd()
    if imf_full:
        print(f"  完整序列: {len(imf_full)} 条，范围 {imf_full[0]['period']}-{imf_full[-1]['period']}（含预测值）")
    print(f"  截到 {YEARS[0]}-{YEARS[-1]}: {len(imf_rows)} 条")
    y2020 = next((r for r in imf_rows if str(r["period"]) == "2020"), None)
    print(f"  IMF 2020 原始值: {y2020['value'] if y2020 else None}（十亿美元）")

    # ---- 5) 统一口径 ----
    _section(f"[5] 统一到 {BASE_UNIT}")
    nbs_usd = _convert(nbs_rows, rates, 1.0)
    wb_usd = _convert(wb_rows, rates, 1e-8)
    imf_usd = cross_validation.rescale_series(imf_rows, 10.0)
    print(f"  换算规则: NBS 亿元/汇率 ; WB 元/汇率/1e8 ; IMF 十亿美元 x 10")
    for label, rows in (("NBS", nbs_usd), ("WB", wb_usd), ("IMF", imf_usd)):
        print(f"  {label:<3} {len(rows)} 个年份")

    # ---- 6) 两两比较 ----
    res_ni = cross_validation.compare_series(nbs_usd, imf_usd)
    res_wi = cross_validation.compare_series(wb_usd, imf_usd)
    ni_by = {str(r["period"]): r for r in res_ni["rows"]}
    wi_by = {str(r["period"]): r for r in res_wi["rows"]}
    nbs_by = {str(r["period"]): r["value"] for r in nbs_usd}
    wb_by = {str(r["period"]): r["value"] for r in wb_usd}
    imf_by = {str(r["period"]): r["value"] for r in imf_usd}

    _section("[6] 三方对比表")
    print(f"{'年份':<6}{'NBS(亿美元)':>16}{'WB(亿美元)':>16}{'IMF(亿美元)':>16}"
          f"{'NBS vs IMF':>14}{'WB vs IMF':>14}  判定")
    print("-" * 100)
    table: list[dict[str, Any]] = []
    for p in sorted(set(nbs_by) | set(wb_by) | set(imf_by)):
        ni = ni_by.get(p) or {}
        wi = wi_by.get(p) or {}
        verdict = _worst(ni.get("verdict"), wi.get("verdict"))
        print(f"{p:<6}{_fmt_num(nbs_by.get(p))}{_fmt_num(wb_by.get(p))}{_fmt_num(imf_by.get(p))}"
              f"{_fmt_rate(ni.get('diff_rate'))}{_fmt_rate(wi.get('diff_rate'))}  {verdict}")
        table.append({
            "period": p,
            "nbs_usd_100m": nbs_by.get(p),
            "wb_usd_100m": wb_by.get(p),
            "imf_usd_100m": imf_by.get(p),
            "nbs_vs_imf_diff_rate": ni.get("diff_rate"),
            "wb_vs_imf_diff_rate": wi.get("diff_rate"),
            "verdict": verdict,
        })

    # ---- 7) summary 与断言 ----
    _section("[7] summary 与断言")
    print(f"  NBS vs IMF: {json.dumps(res_ni['summary'], ensure_ascii=False)}")
    print(f"  WB  vs IMF: {json.dumps(res_wi['summary'], ensure_ascii=False)}")

    common3 = sorted(set(nbs_by) & set(wb_by) & set(imf_by))
    max_ni = res_ni["summary"]["max_diff_rate"]
    mean_ni = res_ni["summary"]["mean_diff_rate"]
    max_ni_s = "None" if max_ni is None else f"{max_ni:.2%}"
    mean_ni_s = "None" if mean_ni is None else f"{mean_ni:.2%}"

    if max_ni is not None and max_ni < 0.05:
        print(f"  [OK] 三方一致性良好（NBS vs IMF 最大差异率 {max_ni_s}，均值 {mean_ni_s}）")
    else:
        print(f"  [WARN] 三方存在实质差异，需人工核查（NBS vs IMF 最大差异率 {max_ni_s}）")

    problems: list[str] = []
    if len(common3) < 10:
        problems.append(f"三方共同年份只有 {len(common3)} 个（要求 >= 10）")
    if res_ni["summary"]["verdict"] == "不可拼接":
        problems.append("NBS vs IMF 判定为不可拼接")
    if res_wi["summary"]["verdict"] == "不可拼接":
        problems.append("WB vs IMF 判定为不可拼接")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    out = RESULTS_DIR / f"gdp_3way_{stamp}.json"
    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "base_unit": BASE_UNIT,
        "sources": {
            "nbs": {"endpoint": "getEsDataByIndicatorIdAndDa", "raw_unit": "亿元"},
            "worldbank_lcu": {"indicator": WB_INDICATOR_LCU, "raw_unit": "元"},
            "worldbank_fx": {"indicator": WB_INDICATOR_FX, "raw_unit": "LCU per US$"},
            "imf": {"indicator": IMF_INDICATOR, "raw_unit": "十亿美元"},
        },
        "fx_rates": rates,
        "nbs_vs_imf": res_ni["summary"],
        "wb_vs_imf": res_wi["summary"],
        # 数据驱动字段：本文件产出两对，放 pairs 数组（arbiter 逐条读）
        "pairs": [
            {"series_a": "nbs|gdp|cny_100m", "series_b": "imf|NGDPD",
             "measured": {"diff_pp": (res_ni.get("summary") or {}).get("max_diff_rate"),
                          "diff_type": "percent",
                          "source": "data/validated/cross_check/" + out.name}},
            {"series_a": "worldbank|NY.GDP.MKTP.CN", "series_b": "imf|NGDPD",
             "measured": {"diff_pp": (res_wi.get("summary") or {}).get("max_diff_rate"),
                          "diff_type": "percent",
                          "source": "data/validated/cross_check/" + out.name}},
        ],
        "common_years": common3,
        "rows": table,
        "problems": problems,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  [saved] {out}")

    if problems:
        print()
        for p in problems:
            print(f"  [FAIL] {p}", file=sys.stderr)
        return 1
    print("  [PASS] 三方交叉验证通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


