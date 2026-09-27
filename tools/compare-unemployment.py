#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""compare-unemployment.py —— 失业率三方对比：**第一次遇到真实分歧**。

背景（为什么 GDP 那两个脚本证明不了验证层有用）
------------------------------------------------
NBS / World Bank / IMF 对中国 GDP 用的其实是**同一套数字**：
实然增速差异 0.00 pp（见 tools/compare-gdp-real.py），美元口径那点差异也只是换算。
当前测试集里**没有任何分歧**，所以验证层"看起来完美"其实是"没东西可验"。

失业率就不同了——口径本身就分叉：

* **NBS 城镇登记失业率**（旧口径，只统计到人社部门登记的失业人员）：约 3.6%-4.2%
* **NBS 全国城镇调查失业率**（新口径，抽样调查）：约 5%
* **IMF LUR**：实测与"调查失业率"几乎重合，与"登记失业率"差 1-2 pp

本脚本的作用是**展示差异的结构**，不是判断对错。

两条 NBS 序列的取数位置（实测）
--------------------------------
======================  ==================  =====================================================
序列                     树                   cid / indicator_id
======================  ==================  =====================================================
城镇登记失业率 (%)        年度树 (code=3)      cid=19839dbd8e82481b9524f031c6d816c5
                                            id=456ef2f3d75f472b930384237bc6100f
全国城镇调查失业率 (%)    月度树 (code=1)      cid=ee3b7046b390415b9b7745e3d16f6052
                                            id=3888eac6062945a79c8a27e5f13d4953
======================  ==================  =====================================================

两个必须知道的数据事实：

1. **"城镇调查失业率"不在年度树里**。年度树 13 个"失业"节点只有"城镇登记失业率 (%)"和一堆
   失业保险基金项；调查失业率只在**月度树**里。因此本脚本取月度序列后**按年均值**聚合到年
   （这是本脚本唯一的建模决定，见 `_annual_mean` —— 实现落在 load_nbs_surveyed_annual）。
2. **登记失业率 2022 年起为空**（2015-2021 有值，2022/2023/2024 的 v 是空串）。
   所以"登记 vs IMF"的共同年份只有 2017-2021。

阈值（百分点）
--------------
=================  ==========
|diff_pp|          判定
=================  ==========
< 0.5              一致
0.5 ~ 1.5          可接受
1.5 ~ 3.0          警告
> 3.0              冲突
=================  ==========

**刻意不做"差异是否可接受"的 fail 判定**：登记失业率与 IMF LUR 本就不是一个口径，
差 1.5 pp 是**结构事实**而不是错误。唯一断言是 `n_common < 5 -> exit 1`
（防止某天上游改结构导致整张表变空却静默通过）。

输出
----
    data/validated/cross_check/unemployment_3way_YYYYMMDD.json

用法
----
    .\\.venv\\Scripts\\python.exe tools\\compare-unemployment.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from econ_core import imf_client, nbs_client, normalize  # noqa: E402

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

REGION_CODE = "000000000000"
REGION_NAME = "全国"

# --- NBS 城镇登记失业率（年度树 code=3） ---
REG_CID = "19839dbd8e82481b9524f031c6d816c5"
REG_NODE = "456ef2f3d75f472b930384237bc6100f"
ANNUAL_ROOT = "71d41888d5a44bb2a67402ef4e60003e"

# --- NBS 全国城镇调查失业率（月度树 code=1，按年均值聚合） ---
SUR_CID = "ee3b7046b390415b9b7745e3d16f6052"
SUR_NODE = "3888eac6062945a79c8a27e5f13d4953"
MONTHLY_ROOT = "3c9c459384c74f578f3541b2198aac70"

YEARS: list[int] = list(range(2015, 2025))
ANNUAL_PERIODS: list[str] = [f"{y}YY" for y in YEARS]
#: 月度取到 2017 起（IMF LUR 对 CHN 从 2017 才开始，再往前没有对比对象）
MONTHLY_PERIODS: list[str] = [
    f"{y}{m:02d}MM" for y in range(2017, 2025) for m in range(1, 13)
]

#: IMF 失业率
IMF_INDICATOR = "LUR"
IMF_COUNTRY = "CHN"

RESULTS_DIR = Path(__file__).resolve().parents[1] / "data" / "validated" / "cross_check"


def verdict_pp(diff_pp: Optional[float]) -> str:
    """按百分点阈值给判定（见模块 docstring 的表）。"""
    if diff_pp is None:
        return "缺值"
    a = abs(diff_pp)
    if a < 0.5:
        return "一致"
    if a < 1.5:
        return "可接受"
    if a <= 3.0:
        return "警告"
    return "冲突"


def _num(v: Any) -> Optional[float]:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    return None


def _max_abs(rows: list[dict[str, Any]], field: str) -> Optional[float]:
    """rows[].<field> 里所有数值的绝对值最大值（供 measured.diff_pp 用）。"""
    vals = [abs(float(r[field])) for r in rows
            if isinstance(r.get(field), (int, float)) and not isinstance(r.get(field), bool)]
    return max(vals) if vals else None


def _fmt(v: Any, width: int = 16) -> str:
    f = _num(v)
    return f"{f:>{width},.2f}" if f is not None else f"{'—':>{width}}"


def _fmt_pp(v: Any, width: int = 14) -> str:
    f = _num(v)
    return f"{f:>+{width},.2f}" if f is not None else f"{'—':>{width}}"


# --------------------------------------------------------------------------- #
# 数据加载
# --------------------------------------------------------------------------- #

def load_nbs_registered() -> list[dict[str, Any]]:
    """NBS 城镇登记失业率（年度树）-> [{period, value}]，value 可能为 None。"""
    raw = nbs_client.fetch_indicator_data(
        cid=REG_CID, indicator_id=REG_NODE, root_id=ANNUAL_ROOT,
        da=REGION_CODE, dts=ANNUAL_PERIODS,
    )
    request_params = {"cid": REG_CID, "id": REG_NODE, "da": REGION_CODE,
                      "dt": "", "rootId": ANNUAL_ROOT, "dts": ANNUAL_PERIODS}
    meta = normalize.source_meta_from_parsed(
        "getEsDataByIndicatorIdAndDa", request_params,
        region_name=REGION_NAME, tree_node_id=REG_NODE,
    )
    rows = normalize.normalize_observations(raw, meta)
    return [{"period": str(r["period"]), "value": r.get("value")} for r in rows]


def load_nbs_surveyed_annual() -> tuple[dict[str, Optional[float]], dict[str, int], int]:
    """NBS 全国城镇调查失业率（月度树）-> 按**年均值**聚合到年。

    这是本脚本唯一的建模决定：NBS 只在月度树暴露调查失业率，而 IMF LUR 是年度
    均值口径，所以这里对每年 12 个月取算术平均（跳过空值）。宁可显式做这一步并
    把月数一并报出来，也不偷偷用某个月份代表全年。

    :returns: (annual_mean, months_count, raw_month_rows)
    """
    raw = nbs_client.fetch_indicator_data(
        cid=SUR_CID, indicator_id=SUR_NODE, root_id=MONTHLY_ROOT,
        da=REGION_CODE, dts=MONTHLY_PERIODS,
    )
    request_params = {"cid": SUR_CID, "id": SUR_NODE, "da": REGION_CODE,
                      "dt": "", "rootId": MONTHLY_ROOT, "dts": MONTHLY_PERIODS}
    meta = normalize.source_meta_from_parsed(
        "getEsDataByIndicatorIdAndDa", request_params,
        region_name=REGION_NAME, tree_node_id=SUR_NODE,
    )
    rows = normalize.normalize_observations(raw, meta)

    buckets: dict[str, list[float]] = {}
    for r in rows:
        year = str(r["period"])[:4]          # normalize 把 202001MM 变成 "2020-01"
        v = _num(r.get("value"))
        if v is not None:
            buckets.setdefault(year, []).append(v)

    means = {y: sum(vs) / len(vs) for y, vs in buckets.items()}
    counts = {y: len(vs) for y, vs in buckets.items()}
    return means, counts, len(rows)


def load_imf() -> list[dict[str, Any]]:
    """IMF LUR / CHN -> [{period, value}]。"""
    rows = imf_client.fetch_indicator(IMF_INDICATOR, IMF_COUNTRY)
    return [{"period": str(r.get("period")), "value": r.get("value")} for r in rows]


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

#: 判定严重度（用于把"登记 vs IMF"和"调查 vs IMF"合成一列）
_RANK: dict[str, int] = {"一致": 0, "可接受": 1, "警告": 2, "冲突": 3, "缺值": 4}


def _worst(a: Optional[str], b: Optional[str]) -> str:
    cands = [x for x in (a, b) if x and x != "缺值"]
    if not cands:
        return "缺值"
    return max(cands, key=lambda x: _RANK.get(x, 9))


def _section(title: str) -> None:
    print()
    print("=" * 104)
    print(title)
    print("=" * 104)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    # ---- 1) NBS 登记失业率 ----
    _section("[1] NBS 城镇登记失业率 (%)（年度树 code=3）")
    reg = load_nbs_registered()
    print(f"  行数: {len(reg)}   cid={REG_CID}  id={REG_NODE}")
    for r in reg:
        print(f"    {r['period']}  value={r['value']!r}")

    # ---- 2) NBS 调查失业率（月度 -> 年均值）----
    _section("[2] NBS 全国城镇调查失业率 (%)（月度树 code=1 -> 按年均值）")
    sur, months, raw_n = load_nbs_surveyed_annual()
    print(f"  月度原始行数: {raw_n}   cid={SUR_CID}  id={SUR_NODE}")
    for y in sorted(sur):
        print(f"    {y}  年均值={sur[y]:.3f}%   （{months.get(y, 0)} 个月）")

    # ---- 3) IMF ----
    _section(f"[3] IMF：{IMF_INDICATOR} / {IMF_COUNTRY}（Unemployment rate）")
    imf = load_imf()
    print(f"  行数: {len(imf)}")
    if imf:
        print(f"  年份范围: {imf[0]['period']} - {imf[-1]['period']}（含预测值）")
    for r in imf:
        if str(r["period"]) in {str(y) for y in YEARS}:
            print(f"    {r['period']}  value={r['value']}")

    # ---- 4) 对比表 ----
    _section("[4] 三方对比表（差异用百分点 pp）")
    reg_by = {r["period"]: _num(r["value"]) for r in reg}
    sur_by = {y: _num(v) for y, v in sur.items()}
    imf_by = {r["period"]: _num(r["value"]) for r in imf}
    # 只列 IMF 有值、且落在实际年份窗口内的年份：IMF LUR 覆盖到 2031（含预测），
    # 把 2025-2031 那些"只有预测值"的年份列进来只会得到一屏"缺值"噪音。
    all_years = [y for y in sorted(imf_by) if y in {str(v) for v in YEARS}]

    print(f"{'年份':<6}{'NBS 登记(%)':>16}{'NBS 调查(%)':>16}{'IMF LUR(%)':>16}"
          f"{'登记 vs IMF':>16}{'调查 vs IMF':>16}  判定")
    print("-" * 104)
    table: list[dict[str, Any]] = []
    n_common = 0
    for y in all_years:
        rv, sv, iv = reg_by.get(y), sur_by.get(y), imf_by.get(y)
        d_reg = round(rv - iv, 6) if (rv is not None and iv is not None) else None
        d_sur = round(sv - iv, 6) if (sv is not None and iv is not None) else None
        v = _worst(verdict_pp(d_reg), verdict_pp(d_sur))
        if iv is not None and (rv is not None or sv is not None):
            n_common += 1
        print(f"{y:<6}{_fmt(rv)}{_fmt(sv)}{_fmt(iv)}{_fmt_pp(d_reg)}{_fmt_pp(d_sur)}  {v}")
        table.append({
            "period": y,
            "nbs_registered_pct": rv,
            "nbs_surveyed_annual_mean_pct": sv,
            "nbs_surveyed_months": months.get(y),
            "imf_lur_pct": iv,
            "registered_vs_imf_pp": d_reg,
            "surveyed_vs_imf_pp": d_sur,
            "verdict": v,
        })

    # ---- 5) summary 与断言 ----
    _section("[5] summary 与断言")
    print(f"  n_common（IMF 有值且至少一条 NBS 有值的年份）= {n_common}")
    print("  说明：登记失业率 2022 年起为空，故『登记 vs IMF』只有 2017-2021；")
    print("        调查失业率覆盖 2019-2024，故『调查 vs IMF』有 2019-2024。")
    print("        本脚本**不做**『差异是否可接受』的判定 —— 口径不同，差异是结构事实。")

    problems: list[str] = []
    if n_common < 5:
        problems.append(f"n_common={n_common} < 5（共同年份太少，可能上游结构变了）")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    out = RESULTS_DIR / f"unemployment_3way_{stamp}.json"
    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "diff_unit": "百分点(pp)",
        "thresholds_pp": {"一致": "< 0.5", "可接受": "0.5 ~ 1.5",
                          "警告": "1.5 ~ 3.0", "冲突": "> 3.0"},
        "sources": {
            "nbs_registered": {"name": "城镇登记失业率 (%)", "tree": "annual(code=3)",
                               "cid": REG_CID, "tree_node_id": REG_NODE},
            "nbs_surveyed": {"name": "全国城镇调查失业率 (%)", "tree": "monthly(code=1)",
                             "cid": SUR_CID, "tree_node_id": SUR_NODE,
                             "aggregation": "annual mean of monthly values"},
            "imf": {"indicator": IMF_INDICATOR, "country": IMF_COUNTRY},
        },
        "n_common": n_common,
        # 数据驱动字段：本文件产出两对（登记 vs LUR、调查 vs LUR），差值取各年 |差异| 的最大值
        "pairs": [
            {"series_a": "nbs|registered_unemployment", "series_b": "imf|LUR",
             "measured": {"diff_pp": _max_abs(table, "registered_vs_imf_pp"),
                          "diff_type": "pp",
                          "source": "data/validated/cross_check/" + out.name}},
            {"series_a": "nbs|surveyed_unemployment", "series_b": "imf|LUR",
             "measured": {"diff_pp": _max_abs(table, "surveyed_vs_imf_pp"),
                          "diff_type": "pp",
                          "source": "data/validated/cross_check/" + out.name}},
        ],
        "rows": table,
        "problems": problems,
        "note": "展示口径差异的结构，不判定差异是否可接受",
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  [saved] {out}")

    if problems:
        print(file=sys.stderr)
        for p in problems:
            print(f"  [FAIL] {p}", file=sys.stderr)
        return 1
    print("  [PASS] 失业率三方对比完成（展示型，无差异阈值断言）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

