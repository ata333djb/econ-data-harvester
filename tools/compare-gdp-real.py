#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""compare-gdp-real.py —— 用**实际增速**做无汇率污染的口径对比。

为什么需要它
------------
tools/compare-gdp-3way.py 把三方统一到美元后对比，实测 NBS vs IMF 差 0.2%-1.1%。
但那个数字**混淆了两件事**：

1. IMF 对 GDP 名义水平的调整；
2. 各家把本币换成美元时用的**汇率**不同。

而且仅凭 NGDPD **无法**把两者分开：用 NBS(亿元)/IMF(亿美元) 反推"IMF 隐含汇率"，
再与 WB 的 PA.NUS.FCRF 相比，得到的差异率**恒等于**那个 GDP 差异率（同义反复），
所以那一步不构成独立证据。datamapper 目录里也没有 IMF 自己的市场汇率序列
（只有 PPPEX 隐含 PPP 转换率，不能用于名义换算）。

实际增速是**比值**，天然不含汇率，因此可以直接对比口径：

* IMF 侧：`NGDP_RPCH`（Real GDP growth，年 %）
* NBS 侧：`国内生产总值指数(上年=100)` -> 增速 = 指数 - 100

阈值（增速差异用**百分点 pp**，不是百分比）
------------------------------------------
=================  ==========
|diff_pp|          判定
=================  ==========
< 0.3              一致
0.3 ~ 1.0          可接受
1.0 ~ 2.0          警告
> 2.0              冲突
=================  ==========

断言（供 tools/run-all-checks.py 当门禁用）：`n_common < 8` 或
`max|diff_pp| > 2.0` -> exit 1。

实测结论（2026-09 这一版数据）：10/10 年差异 **0.00 pp**，两源实际增速完全一致。
这反过来证明 3way 里那 0.2%-1.1% 的美元差异**不来自**增长 / 实际活动口径。

输出
----
    data/validated/cross_check/gdp_real_YYYYMMDD.json

用法
----
    .\\.venv\\Scripts\\python.exe tools\\compare-gdp-real.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from econ_core import (  # noqa: E402
    cross_check_store, imf_client, nbs_client, normalize,
)

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

#: NBS 年度数据 > 国民经济核算 > 国内生产总值指数 > 国内生产总值指数 (上年=100)
#: （由 python/_probes 式的树遍历定位：1011 个 GDP/指数相关节点里挑出的那一支）
IDX_CID = "489888799f8d470786bc01a4057efc38"
IDX_TREE_NODE = "93dd15c8a3a3400ea89f8dceec7ab2b3"
ROOT_ID = "71d41888d5a44bb2a67402ef4e60003e"
REGION_CODE = "000000000000"
REGION_NAME = "全国"

YEARS: list[int] = list(range(2015, 2025))
PERIODS: list[str] = [f"{y}YY" for y in YEARS]

IMF_INDICATOR = "NGDP_RPCH"   # Real GDP growth (annual percent change)
IMF_COUNTRY = "CHN"

#: 指数口径：上年=100，故增速 = 指数 - 100
INDEX_BASE = 100.0

RESULTS_DIR = Path(__file__).resolve().parents[1] / "data" / "validated" / "cross_check"


def verdict_pp(diff_pp: Optional[float]) -> str:
    """按百分点阈值给判定（见模块 docstring 的表）。"""
    if diff_pp is None:
        return "缺值"
    a = abs(diff_pp)
    if a < 0.3:
        return "一致"
    if a < 1.0:
        return "可接受"
    if a <= 2.0:
        return "警告"
    return "冲突"


# --------------------------------------------------------------------------- #
# 数据加载
# --------------------------------------------------------------------------- #

def load_nbs_index() -> list[dict[str, Any]]:
    """NBS 国内生产总值指数(上年=100) -> [{period, index, growth, indicator_name}]。"""
    raw = nbs_client.fetch_indicator_data(
        cid=IDX_CID, indicator_id=IDX_TREE_NODE, root_id=ROOT_ID,
        da=REGION_CODE, dts=PERIODS,
    )
    request_params = {"cid": IDX_CID, "id": IDX_TREE_NODE, "da": REGION_CODE,
                      "dt": "", "rootId": ROOT_ID, "dts": PERIODS}
    meta = normalize.source_meta_from_parsed(
        "getEsDataByIndicatorIdAndDa", request_params,
        region_name=REGION_NAME, tree_node_id=IDX_TREE_NODE,
    )
    rows = normalize.normalize_observations(raw, meta)

    out: list[dict[str, Any]] = []
    for r in rows:
        idx = r.get("value")
        ok = isinstance(idx, (int, float)) and not isinstance(idx, bool)
        out.append({
            "period": str(r["period"]),
            "index": idx,
            "growth": (float(idx) - INDEX_BASE) if ok else None,
            "indicator_name": r.get("indicator_name"),
        })
    return out


def load_imf_growth() -> list[dict[str, Any]]:
    """IMF NGDP_RPCH -> [{period, growth}]（截到 YEARS）。"""
    want = {str(y) for y in YEARS}
    rows = imf_client.fetch_indicator(IMF_INDICATOR, IMF_COUNTRY)
    return [{"period": str(r.get("period")), "growth": r.get("value")}
            for r in rows if str(r.get("period")) in want]


def _num(v: Any) -> Optional[float]:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    return None


def _fmt(v: Any, width: int = 16) -> str:
    f = _num(v)
    return f"{f:>{width},.2f}" if f is not None else f"{'—':>{width}}"


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def _section(title: str) -> None:
    print()
    print("=" * 92)
    print(title)
    print("=" * 92)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    # ---- 1) NBS 指数 ----
    _section("[1] NBS：国内生产总值指数 (上年=100)")
    nbs = load_nbs_index()
    print(f"  行数          : {len(nbs)}")
    print(f"  indicator_name: {sorted({str(r['indicator_name']) for r in nbs})}")
    print(f"  换算          : 增速 = 指数 - {INDEX_BASE:g}")
    for r in nbs:
        g = _num(r["growth"])
        gs = "None" if g is None else f"{g:.1f}%"
        print(f"    {r['period']}  指数={r['index']}  -> 增速={gs}")

    # ---- 2) IMF 增速 ----
    _section(f"[2] IMF：{IMF_INDICATOR}（Real GDP growth，年 %）")
    imf = load_imf_growth()
    print(f"  行数: {len(imf)}")
    for r in imf:
        print(f"    {r['period']}  增速={r['growth']}%")

    # ---- 3) 对比表 ----
    _section("[3] 对比表（增速差异用百分点 pp，不是百分比）")
    nbs_by = {r["period"]: r for r in nbs}
    imf_by = {r["period"]: r for r in imf}
    common = sorted(set(nbs_by) & set(imf_by))

    print(f"{'年份':<6}{'NBS 增速(%)':>16}{'IMF 增速(%)':>16}{'差异(pp)':>16}  判定")
    print("-" * 76)
    table: list[dict[str, Any]] = []
    diffs: list[float] = []
    for p in common:
        ng = _num(nbs_by[p]["growth"])
        ig = _num(imf_by[p]["growth"])
        d: Optional[float] = None
        if ng is not None and ig is not None:
            d = round(ng - ig, 6)
            diffs.append(d)
        v = verdict_pp(d)
        print(f"{p:<6}{_fmt(ng)}{_fmt(ig)}{_fmt(d)}  {v}")
        table.append({"period": p, "nbs_growth_pct": ng, "imf_growth_pct": ig,
                      "diff_pp": d, "verdict": v})

    # ---- 4) summary 与断言 ----
    _section("[4] summary 与断言")
    max_abs = max((abs(d) for d in diffs), default=None)
    mean_abs = (sum(abs(d) for d in diffs) / len(diffs)) if diffs else None
    print(f"  n_common     = {len(common)}")
    print(f"  n_compared   = {len(diffs)}")
    print(f"  max |diff_pp|= {max_abs}")
    print(f"  mean|diff_pp|= {mean_abs}")

    if max_abs is not None and max_abs < 0.3:
        print(f"  [OK] 两源实际增速一致（max |diff| = {max_abs:.2f} pp）"
              f"—— 无汇率污染口径下无实质差异")
    elif max_abs is not None:
        print(f"  [WARN] 两源实际增速存在差异（max |diff| = {max_abs:.2f} pp），需人工核查")

    problems: list[str] = []
    if len(common) < 8:
        problems.append(f"共同年份只有 {len(common)} 个（要求 >= 8）")
    if max_abs is not None and max_abs > 2.0:
        problems.append(f"最大增速差异 {max_abs:.2f} pp 超过 2.0 pp 阈值")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    out = RESULTS_DIR / f"gdp_real_{stamp}.json"
    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "basis": "real GDP growth（无汇率污染）",
        "diff_unit": "百分点(pp)",
        "thresholds_pp": {"一致": "< 0.3", "可接受": "0.3 ~ 1.0",
                          "警告": "1.0 ~ 2.0", "冲突": "> 2.0"},
        "sources": {
            "nbs": {"indicator_name": "国内生产总值指数 (上年=100)",
                    "cid": IDX_CID, "tree_node_id": IDX_TREE_NODE, "index_base": INDEX_BASE},
            "imf": {"indicator": IMF_INDICATOR, "country": IMF_COUNTRY},
        },
        "n_common": len(common),
        "n_compared": len(diffs),
        "max_abs_diff_pp": max_abs,
        "mean_abs_diff_pp": mean_abs,
        # 数据驱动字段（arbiter 只读这三个；上面的 max_abs_diff_pp 留给人和旧读者）
        "series_a": "nbs|gdp|index_prev_year_100",
        "series_b": "imf|NGDP_RPCH",
        "measured": {"diff_pp": max_abs, "diff_type": "pp",
                     "source": "data/validated/cross_check/" + out.name},
        "rows": table,
        "problems": problems,
    }
    removed = cross_check_store.save_result(payload, out, "gdp_real", RESULTS_DIR)
    print(f"  [saved] {out}")
    if removed:
        print(f"  [pruned] 清掉同族旧日期文件 {len(removed)} 个: "
              f"{', '.join(p.name for p in removed)}")

    if problems:
        print(file=sys.stderr)
        for p in problems:
            print(f"  [FAIL] {p}", file=sys.stderr)
        return 1
    print("  [PASS] 实际增速口径对比通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

