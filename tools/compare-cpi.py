#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""compare-cpi.py —— CPI 交叉验证：NBS（上年=100）vs FRED（OECD 派生，2015=100 指数）。

背景：为什么补这一条
--------------------
CPI 此前是本项目**唯一零交叉验证**的指标（3 条落盘序列，占 1/3）。
FRED 的 CHNCPIALLMINMEI 源自 OECD Main Economic Indicators，是不同于 NBS 的
二次汇编方 —— 但要说清楚：OECD 的原始数据仍来自中国国家统计局，所以"一致"只能证明
**转述与换算没出错**，不能证明这是独立测量。本脚本如实报出一致程度，不代它宣称独立性。

口径转换（本脚本唯一的建模决定）
--------------------------------
* NBS：年度「上年=100」（如 101.4 表示 +1.4%）
* FRED：月度「2015=100」指数
  1. 按**年均值**聚合到年（当年必须满 12 个月，否则不参与 —— 2025 年只有 4 个月，被排除）
  2. 转成同比：(本年均值 / 上一年年均值 - 1) * 100 + 100

阈值（百分点）
--------------
=================  ==========
|diff_pp|          判定
=================  ==========
< 0.3              一致
0.3 ~ 1.0          可接受
1.0 ~ 2.0          警告
> 2.0              冲突
=================  ==========

断言（失败即 exit 1）
--------------------
* n_common < 5 -> 失败（防止上游改结构导致整张表变空却静默通过）
* max |diff_pp| > 2.0 -> 失败

输出
----
    data/validated/cross_check/cpi_3way_YYYYMMDD.json

用法
----
    ./.venv/Scripts/python.exe tools/compare-cpi.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from econ_core import cross_check_store, fred_client, nbs_client, normalize  # noqa: E402

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

REGION_CODE = "000000000000"
REGION_NAME = "全国"
CPI_CODE = 21                      # NBS 默认指标：年度 CPI
CPI_SERIES_NAME = "全国居民消费价格指数（上年=100） (%)"
FRED_SERIES = "CHNCPIALLMINMEI"    # FRED：中国 CPI 全项指数（OECD 派生，2015=100，月度）

YEARS: list[int] = list(range(2015, 2025))
MIN_MONTHS = 12                    # 年均值必须满 12 个月才参与同比

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


def _num(v: Any) -> Optional[float]:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    return None


def _fmt_pp(v: Any, width: int = 14) -> str:
    f = _num(v)
    return f"{f:>+{width},.2f}" if f is not None else f"{'—':>{width}}"


# --------------------------------------------------------------------------- #
# 数据加载
# --------------------------------------------------------------------------- #

def load_nbs_cpi() -> tuple[dict[str, float], dict[str, Any]]:
    """NBS 全国 CPI（上年=100）-> ({年份: 值}, 来源信息)。"""
    raw = nbs_client.get_default_indicator(CPI_CODE)
    meta = normalize.source_meta_from_parsed(
        "getDefaultIndicData", {"code": CPI_CODE}, region_name=REGION_NAME)
    rows = normalize.normalize_cpi_wide(raw.get("xData"), raw.get("yData"), {
        "source": "nbs",
        "region_code": REGION_CODE,
        "region_name": REGION_NAME,
        "catalog_id": raw.get("catalogId"),
        "catalog_name": raw.get("catalogName"),
        "fetched_at": meta.get("fetched_at", ""),
        "raw_cache": meta.get("raw_cache", ""),
    })
    out: dict[str, float] = {}
    for r in rows:
        if str(r.get("indicator_name")) != CPI_SERIES_NAME:
            continue
        v = _num(r.get("value"))
        if v is not None:
            out[str(r["period"])[:4]] = v
    return out, {"indicator_name": CPI_SERIES_NAME, "code": CPI_CODE,
                 "raw_cache": meta.get("raw_cache", ""), "n_rows": len(rows)}


def load_fred_index() -> tuple[dict[str, float], dict[str, int], dict[str, Any]]:
    """FRED 月度指数 -> (年均值, 每年月数, 来源信息)。只有满 12 个月的年份进年均值。"""
    rows = fred_client.fetch_series(FRED_SERIES)
    meta = fred_client.last_meta()
    buckets: dict[str, list[float]] = {}
    for r in rows:
        v = _num(r.get("value"))
        if v is not None:
            buckets.setdefault(str(r["period"])[:4], []).append(v)
    counts = {y: len(vs) for y, vs in buckets.items()}
    means = {y: sum(vs) / len(vs) for y, vs in buckets.items() if len(vs) >= MIN_MONTHS}
    return means, counts, {"series_id": FRED_SERIES, "n_rows": len(rows),
                           "frequency": meta.get("frequency"),
                           "raw_cache": meta.get("raw_cache", "")}


def fred_prev100(means: dict[str, float]) -> dict[str, float]:
    """年均值指数 -> 上年=100 的同比（需要上一年的年均值）。"""
    out: dict[str, float] = {}
    for y in sorted(means):
        prev = str(int(y) - 1)
        if prev in means and means[prev] != 0:
            out[y] = (means[y] / means[prev] - 1.0) * 100.0 + 100.0
    return out


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

    problems: list[str] = []

    _section("[1] NBS 全国 CPI（上年=100，年度）")
    nbs, nbs_meta = load_nbs_cpi()
    print(f"  序列名: {nbs_meta['indicator_name']}   code={nbs_meta['code']}")
    for y in sorted(nbs):
        print(f"    {y}  {nbs[y]:.2f}")

    _section("[2] FRED CHNCPIALLMINMEI（月度指数 2015=100 -> 年均值 -> 同比）")
    means, counts, fred_meta = load_fred_index()
    print(f"  series_id={fred_meta['series_id']} 频率={fred_meta['frequency']} 原始行数={fred_meta['n_rows']}")
    prev100 = fred_prev100(means)
    for y in sorted(means):
        mark = "" if counts.get(y, 0) >= MIN_MONTHS else "（月数不足，不参与）"
        pv = prev100.get(y)
        pv_s = f"{pv:.2f}" if pv is not None else "—"
        print(f"    {y}  年均值={means[y]:.3f}  月数={counts.get(y, 0):>2}  同比(上年=100)={pv_s} {mark}")

    _section("[3] 对比表（差异用百分点 pp）")
    header = f"{'年份':<6}{'NBS CPI':>12}{'FRED CPI':>12}{'差异(pp)':>14}{'判定':>10}"
    print(header)
    print("-" * len(header))
    rows: list[dict[str, Any]] = []
    for y in YEARS:
        key = str(y)
        a = nbs.get(key)
        b = prev100.get(key)
        diff = (b - a) if (a is not None and b is not None) else None
        vd = verdict_pp(diff)
        rows.append({"period": key, "nbs_cpi_prev100": a, "fred_cpi_prev100": b,
                     "diff_pp": diff, "verdict": vd})
        a_s = f"{a:>12.2f}" if a is not None else f"{'—':>12}"
        b_s = f"{b:>12.2f}" if b is not None else f"{'—':>12}"
        print(f"{key:<6}{a_s}{b_s}{_fmt_pp(diff)}{vd:>10}")

    diffs = [abs(float(r["diff_pp"])) for r in rows if r["diff_pp"] is not None]
    n_common = len(diffs)
    max_abs = max(diffs) if diffs else None
    mean_abs = (sum(diffs) / len(diffs)) if diffs else None
    print()
    print(f"  共同年份: {n_common}  最大|差异|={max_abs:.3f} pp  平均|差异|="
          f"{mean_abs:.3f} pp" if max_abs is not None else "  无共同年份")

    if n_common < 5:
        problems.append(f"共同年份只有 {n_common} 个（< 5），上游结构可能变了")
    if max_abs is not None and max_abs > 2.0:
        problems.append(f"最大差异 {max_abs:.3f} pp 超过 2.0 pp 阈值")

    _section("[4] 判定与结论")
    sev: dict[str, int] = {}
    for r in rows:
        sev[r["verdict"]] = sev.get(r["verdict"], 0) + 1
    print(f"  判定分布: {json.dumps(sev, ensure_ascii=False)}")
    if max_abs is not None and max_abs < 0.3:
        print("  结论: NBS 与 FRED(OECD) 的 CPI 年通胀高度一致（全部年份 < 0.3 pp）")
        print("        => 换算是通的；但两次来源同源于中国国家统计局，这**不等于独立验证**")
    for p in problems:
        print(f"  [FAIL] {p}")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / f"cpi_3way_{datetime.now(timezone.utc).strftime('%Y%m%d')}.json"
    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "diff_unit": "百分点(pp)",
        "thresholds_pp": {"一致": "< 0.3", "可接受": "0.3 ~ 1.0",
                          "警告": "1.0 ~ 2.0", "冲突": "> 2.0"},
        "sources": {
            "nbs": nbs_meta,
            "fred": fred_meta,
        },
        "basis": "两边都转成「上年=100」的年度同比",
        "n_common": n_common,
        # 数据驱动字段：arbiter 靠 series_a / series_b 知道这对是「谁和谁」
        "series_a": f"nbs|cpi|{CPI_SERIES_NAME}",
        "series_b": f"fred|{FRED_SERIES}",
        "measured": {"diff_pp": max_abs, "diff_type": "pp",
                     "source": "data/validated/cross_check/" + out.name},
        # 注意键名：**故意不叫 max_abs_diff_pp** —— 那个名字是 tools/compare-gdp-real.py 的
        # 形状标记，arbiter._adapt 会据此把它认成「NBS GDP 指数 × IMF NGDP_RPCH」。
        # 叫同名会让 CPI 这对被**误认成 GDP 那对**（实测踩到过：7 对里有 1 对是错配）。
        "max_abs_diff_cpi_pp": max_abs,
        "mean_abs_diff_cpi_pp": mean_abs,
        "rows": rows,
        "problems": problems,
    }
    removed = cross_check_store.save_result(payload, out, "cpi_3way", RESULTS_DIR)
    print()
    print(f"  结果已写入: {out.relative_to(Path(__file__).resolve().parents[1])}")
    if removed:
        print(f"  [pruned] 清掉同族旧日期文件 {len(removed)} 个: "
              f"{', '.join(p.name for p in removed)}")
    print()
    print("=" * 104)
    print("[PASS] CPI 交叉验证完成" if not problems else f"[FAIL] {len(problems)} 个问题")
    print("=" * 104)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())

