#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""compare-gdp.py —— NBS vs World Bank 中国 GDP 的第一次交叉验证。

流程（对应任务 5 的 1~7 步）
---------------------------
1. nbs_client.fetch_indicator_data 取中国 2015-2024 GDP（NBS 口径：亿元）
2. worldbank_client.fetch_indicator 取中国 2015-2024 GDP（本币 LCU 口径）
3. 各自经 normalize 成同形长表
4. 报告 World Bank 那边的单位到底是什么（LCU 可能是元/万元）
5. 单位对齐（统一换算到亿元）后跑 cross_validation.compare_series
6. 输出逐年对比表：NBS 值 / WB 值 / 差异 / 差异率 / 判定
7. 输出 summary

注意：脚本只做**显式**单位对齐，并把推理过程原样打印，不做隐式换算。

用法
----
    .\\.venv\\Scripts\\python.exe tools\\compare-gdp.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# tools/ 不在 python 包内，需把 python/ 挂到 sys.path 才能 import econ_core.*
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from econ_core import cross_validation, nbs_client, normalize, worldbank_client  # noqa: E402

# --------------------------------------------------------------------------- #
# 常量：NBS 侧 GDP 的参数（来自第一阶段的探测结论）
# --------------------------------------------------------------------------- #

NBS_CID = "f7fd25aaad184414875632cf2327da60"
NBS_INDICATOR_ID = "db8e5a86c08246e79b1b11251927e740"   # 响应里的 i（语义标识）
NBS_TREE_NODE_ID = "7dc6a2ee6c614960b7059991e0cc4d96"   # 请求里的 id（取数用）
NBS_ROOT_ID = "71d41888d5a44bb2a67402ef4e60003e"
NBS_REGION_CODE = "000000000000"                        # 全国
NBS_REGION_NAME = "全国"
PERIODS: list[str] = [f"{y}YY" for y in range(2015, 2025)]

#: World Bank 侧
WB_COUNTRY = "CHN"
WB_INDICATOR_LCU = "NY.GDP.MKTP.CN"                     # GDP (current LCU)
WB_DATE_RANGE = "2015:2024"

#: 展示基准单位（两者都换算到这个单位再比）
BASE_UNIT = "亿元"
BASE_UNIT_TO_YUAN = 1.0e8

RESULTS_DIR = Path(__file__).resolve().parents[1] / "data" / "validated" / "cross_check"


def _units(rows: list[dict[str, Any]]) -> list[str]:
    """取出序列里出现过的单位（去重排序）。"""
    return sorted({str(r.get("unit") or "") for r in rows})


def load_nbs() -> list[dict[str, Any]]:
    """① 取 NBS 中国 GDP 并 normalize 成长表。"""
    print("=" * 78)
    print("[1] NBS 侧：nbs_client.fetch_indicator_data + normalize")
    print("=" * 78)
    raw = nbs_client.fetch_indicator_data(
        cid=NBS_CID,
        indicator_id=NBS_TREE_NODE_ID,
        root_id=NBS_ROOT_ID,
        da=NBS_REGION_CODE,
        dts=PERIODS,
    )
    request_params = {
        "cid": NBS_CID, "id": NBS_TREE_NODE_ID, "da": NBS_REGION_CODE,
        "dt": "", "rootId": NBS_ROOT_ID, "dts": PERIODS,
    }
    meta = normalize.source_meta_from_parsed(
        "getEsDataByIndicatorIdAndDa", request_params,
        region_name=NBS_REGION_NAME, tree_node_id=NBS_TREE_NODE_ID,
    )
    rows = normalize.normalize_observations(raw, meta)
    print(f"  原始观测  : {len(raw)} 条")
    print(f"  长表行    : {len(rows)} 条")
    print(f"  unit 取值 : {_units(rows)}")
    print(f"  indicator : {sorted({str(r.get('indicator_name')) for r in rows})}")
    print(f"  raw_cache : {meta.get('raw_cache', '')}")
    print(f"  parsed    : {meta.get('parsed_file', '')}")
    print(f"  首行      : {json.dumps(rows[0], ensure_ascii=False)}")
    return rows


def load_wb() -> list[dict[str, Any]]:
    """② 取 World Bank 中国 GDP（本币）并 normalize 成长表。"""
    print()
    print("=" * 78)
    print("[2] World Bank 侧：worldbank_client.fetch_indicator + normalize")
    print("=" * 78)
    raw = worldbank_client.fetch_indicator(WB_COUNTRY, WB_INDICATOR_LCU, WB_DATE_RANGE)
    request = worldbank_client.indicator_request(WB_COUNTRY, WB_INDICATOR_LCU, WB_DATE_RANGE)
    meta = normalize.source_meta_from_parsed_worldbank("country_indicator", request)
    rows = normalize.normalize_worldbank_observations(raw, meta)
    print(f"  原始观测  : {len(raw)} 条")
    print(f"  长表行    : {len(rows)} 条")
    print(f"  unit 取值 : {_units(rows)}")
    print(f"  indicator : {sorted({str(r.get('indicator_name')) for r in rows})}")
    print(f"  raw_cache : {meta.get('raw_cache', '')}")
    print(f"  parsed    : {meta.get('parsed_file', '')}")
    print(f"  首行      : {json.dumps(rows[0], ensure_ascii=False)}")
    print()
    print("  [3] World Bank 单位判定（原始证据）")
    print(f"      观测记录 unit 字段取值 : {_units(rows)}  <- 为空串，拿不到单位")
    for r in raw[:1]:
        print(f"      indicator.value        : {json.dumps((r.get('indicator') or {}).get('value'), ensure_ascii=False)}")
        print(f"      country                : {json.dumps(r.get('country'), ensure_ascii=False)}")
    print("      结论: World Bank 数据端点不返回单位字段，单位写在指标名 'GDP (current LCU)' 里；")
    print("      对 CHN 而言 LCU 就是人民币元（CNY），即该序列单位为 元。")
    return rows


def _print_table(rows: list[dict[str, Any]]) -> None:
    """打印逐年对比表。"""
    header = (f"{'年份':<6}{'NBS(亿元)':>16}{'WB(亿元)':>16}"
              f"{'差异(亿元)':>16}{'差异率':>13}  判定")
    print(header)
    print("-" * len(header.encode("gbk", errors="replace")))
    for r in rows:
        va, vb = r.get("value_a"), r.get("value_b")
        diff, rate = r.get("diff"), r.get("diff_rate")
        sa = f"{va:>16,.1f}" if cross_validation._is_number(va) else f"{'—':>16}"
        sb = f"{vb:>16,.1f}" if cross_validation._is_number(vb) else f"{'—':>16}"
        sd = f"{diff:>16,.1f}" if cross_validation._is_number(diff) else f"{'—':>16}"
        sr = f"{rate * 100:>12.4f}%" if cross_validation._is_number(rate) else f"{'—':>13}"
        print(f"{r.get('period', ''):<6}{sa}{sb}{sd}{sr}  {r.get('verdict', '')}")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    nbs_rows = load_nbs()
    wb_rows = load_wb()

    print()
    print("=" * 78)
    print("[4] 单位判定与对齐")
    print("=" * 78)
    nbs_unit = (_units(nbs_rows) or [""])[0]
    wb_unit = (_units(wb_rows) or [""])[0]
    nbs_to_yuan = cross_validation.unit_factor_to_yuan(nbs_unit)
    wb_to_yuan = cross_validation.unit_factor_to_yuan(wb_unit)
    print(f"  NBS unit 字段   : {nbs_unit!r} -> 1 {nbs_unit} = {nbs_to_yuan!r} 元")
    print(f"  WB  unit 字段   : {wb_unit!r} -> unit_factor_to_yuan = {wb_to_yuan!r}")
    if wb_to_yuan is None:
        name = str(wb_rows[0].get("indicator_name") or "")
        if "current LCU" in name:
            wb_to_yuan = 1.0
            print(f"  WB 单位回退判定 : indicator_name={name!r} 含 'current LCU'，"
                  f"CHN 的 LCU = 人民币元 -> 系数 1.0")
    if nbs_to_yuan is None or wb_to_yuan is None:
        print("  X 单位无法判定，拒绝比对（不做隐式换算）")
        return 2

    nbs_factor = nbs_to_yuan / BASE_UNIT_TO_YUAN
    wb_factor = wb_to_yuan / BASE_UNIT_TO_YUAN
    print(f"  统一展示单位    : {BASE_UNIT}（1 {BASE_UNIT} = {BASE_UNIT_TO_YUAN:.0e} 元）")
    print(f"  对齐系数        : NBS x {nbs_factor:g} ；WB x {wb_factor:g}")
    print(f"  等价说法        : NBS 值 x {(nbs_to_yuan / wb_to_yuan):g} = WB 值"
          f"（即 NBS 的 1 {nbs_unit} 对应 WB 的 {nbs_to_yuan / wb_to_yuan:g} 元）")

    nbs_aligned = cross_validation.rescale_series(nbs_rows, nbs_factor)
    wb_aligned = cross_validation.rescale_series(wb_rows, wb_factor)

    print()
    print("=" * 78)
    print(f"[5]/[6] compare_series(NBS, WorldBank) —— 均为 {BASE_UNIT}")
    print("=" * 78)
    result = cross_validation.compare_series(nbs_aligned, wb_aligned,
                                             key="period", value="value")
    _print_table(result["rows"])

    print()
    print("=" * 78)
    print("[7] summary")
    print("=" * 78)
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    print()
    print("thresholds:")
    print(json.dumps(result["thresholds"], ensure_ascii=False, indent=2))
    print()
    print("notes:")
    for n in result["notes"]:
        print(f"  - {n}")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / "gdp_nbs_vs_worldbank.json"
    payload = {
        "nbs": {"source": "NBS", "unit_raw": nbs_unit, "factor_to_yuan": nbs_to_yuan},
        "wb": {"source": "WorldBank", "unit_raw": wb_unit, "factor_to_yuan": wb_to_yuan},
        "base_unit": BASE_UNIT,
        "result": result,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print(f"[saved] {out}")

    # 门禁只看退出码，"没崩"不等于"验证通过"，所以这里必须自己给出硬边界。
    # 刻意**不**要求 verdict == "可直接拼接"：接入 IMF 等第三个源后差异率可能落在
    # 1~3%（判为"可接受"），那也应该算通过。硬边界只取两条：重叠期不足、或判定"不可拼接"。
    summary = result["summary"]
    if summary["n_common"] < 10 or summary["verdict"] == "不可拼接":
        print(f"[FAIL] 交叉验证未通过: {summary}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())


