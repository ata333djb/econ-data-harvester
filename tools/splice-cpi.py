#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""splice-cpi.py —— 首次真实拼接：BIS 中国 CPI 同比（月度->年）⊗ NBS 年度 CPI。

为什么用这两条
--------------
PROJECT_STATE 长期记着「系统至今**从未真正拼接**过序列」。要证明拼接器真的能用，
需要一对**有重叠期、但来源与加工链不同**的序列：

* **BIS `M.CN.771`**（同比 %，1996-01 起 368 期）—— 转载 NBS，但由 BIS 自建指数
  折算并做过拼接/重定基。本项目按年均值聚合到年。
* **NBS「居民消费价格指数（上年=100）」**（年度，实测 2015-2025）—— 原始发布方，
  口径是"上年=100"的指数，比较前必须 -100 转成同比 %。

所以这是一次**跨频率 + 跨口径**的拼接：BIS 月度要年化，NBS 指数要转百分点。
两者重叠 2015-2025（11 期），既有重叠对照、又有真实的"来源切换"语义。

为什么不选 BIS 628（指数）
--------------------------
628 是 2010=100 的**水平指数**，NBS 是"上年=100"的**同比指数**，两者要先做一次
水平重定基才能比 —— 那会把"拼接"和"重定基"两件事混在一起，反而看不清断点。
771 与 NBS 都在**百分点**量纲上，直接可比，演示价值最高。

产出
----
    data/validated/spliced/cpi_bis_nbs_spliced.json
    {"generated_at", "sources", "basis", "overlap", "splice_point", "breaks",
     "summary", "spliced", "windows"}

用法
----
    .\\.venv\\Scripts\\python.exe tools\\splice-cpi.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "python"))

from econ_core import bis_client, splicer  # noqa: E402

VALIDATED_DIR = PROJECT_ROOT / "data" / "validated"
OUT_DIR = VALIDATED_DIR / "spliced"
OUT_PATH = OUT_DIR / "cpi_bis_nbs_spliced.json"

#: NBS 全国 CPI（上年=100）的落盘长表（materialize-validated 产物）
NBS_CPI_FILE = VALIDATED_DIR / "nbs" / "nbs_cpi_全国居民消费价格指数（上年=100） (%).json"

#: 强制拼接点：BIS 提供 1996 起的历史，NBS 提供 2015 起的权威近期值。
#: 所以窗口上让 BIS 当 series_a（早源）、NBS 当 series_b（晚源），
#: 重叠期（2015-2025）按 later_wins 取 **NBS** —— 理由是 NBS 是原始发布方，
#: 重叠期内它的值优先；BIS 只在 NBS 未覆盖的历史区间（1996-2014）出现。
STRATEGY = "later_wins"


def _load_nbs_annual() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """读 NBS 年度 CPI，并把「上年=100 指数」转成「同比 %」（-100）。"""
    obj = json.loads(NBS_CPI_FILE.read_text(encoding="utf-8"))
    series_key = str(obj.get("series_key") or "nbs|cpi|全国居民消费价格指数（上年=100） (%)")
    rows: list[dict[str, Any]] = []
    for r in obj["rows"]:
        v = r.get("value")
        if v is None or v == "":
            continue
        try:
            yoy = float(v) - 100.0          # 指数 -> 百分点
        except (TypeError, ValueError):
            continue
        rows.append({
            "period": str(r["period"]),
            "value": yoy,
            "series_key": series_key,
            "source": "nbs",
        })
    info = {
        "series_key": series_key,
        "file": str(NBS_CPI_FILE.relative_to(PROJECT_ROOT)),
        "unit_raw": "指数（上年=100）",
        "transform": "value - 100 -> 同比 %（与 BIS 771 同量纲）",
        "n_rows": len(rows),
    }
    return rows, info


def _load_bis_annual() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """取 BIS 771 月度，按年均值聚合到年（只有满 12 个月的年份进序列）。"""
    monthly = bis_client.fetch_cpi(unit="771", freq="M", country="CN")
    meta = bis_client.last_meta()
    buckets: dict[str, list[float]] = {}
    for r in monthly:
        v = r.get("value")
        if v is None:
            continue
        buckets.setdefault(str(r["period"])[:4], []).append(float(v))
    rows: list[dict[str, Any]] = []
    dropped: list[str] = []
    for year in sorted(buckets):
        vals = buckets[year]
        if len(vals) < 12:
            dropped.append(f"{year}({len(vals)}月)")
            continue
        rows.append({
            "period": year,
            "value": sum(vals) / len(vals),
            "series_key": "bis|WS_LONG_CPI|M.CN.771",
            "source": "bis",
        })
    info = {
        "series_key": "bis|WS_LONG_CPI|M.CN.771",
        "unit_raw": "同比变化（%），月度",
        "transform": "按年均值聚合到年（只保留满 12 个月的年份）",
        "n_monthly": len(monthly),
        "n_rows": len(rows),
        "dropped_incomplete_years": dropped,
    }
    return rows, info


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    print("=" * 100)
    print("splice-cpi: 首次真实拼接 —— BIS 中国 CPI 同比（月->年）⊗ NBS 年度 CPI")
    print("=" * 100)

    nbs_rows, nbs_info = _load_nbs_annual()
    bis_rows, bis_info = _load_bis_annual()

    print("\n[1] 两条输入")
    print(f"  series_a（早源）BIS  : {bis_info['n_rows']:>3} 年（自 {bis_info['n_monthly']} 个月度观测年化）")
    print(f"        {bis_info['transform']}")
    if bis_info["dropped_incomplete_years"]:
        print(f"        丢弃不满 12 个月的年份: {bis_info['dropped_incomplete_years']}")
    print(f"  series_b（晚源）NBS  : {nbs_info['n_rows']:>3} 年")
    print(f"        {nbs_info['transform']}")

    print(f"\n[2] 拼接（strategy={STRATEGY}，rebase=none 基线）")
    res = splicer.splice(bis_rows, nbs_rows, strategy=STRATEGY)
    ov, sm = res["overlap"], res["summary"]
    print(f"  并集期数 : {sm['n_total']}（{res['spliced'][0]['period']} ~ {res['spliced'][-1]['period']}）")
    print(f"  来自 BIS : {sm['n_from_a']} 年")
    print(f"  来自 NBS : {sm['n_from_b']} 年")
    print(f"  拼接点   : {res['splice_point']}（来源在此切换）")

    print("\n[3] 重叠期一致性（两条序列的可比窗口）")
    print(f"  重叠期数 : {ov['n_overlap']}   {ov['periods'][0]} ~ {ov['periods'][-1]}"
          if ov["n_overlap"] else f"  重叠期数 : 0（check={ov['check']}）")
    if ov["n_overlap"]:
        print(f"  相对口径 : max_diff_rate  = {ov['max_diff_rate']:.6f} "
              f"({ov['max_diff_rate'] * 100:.4f} %)、mean = {ov['mean_diff_rate'] * 100:.4f} %")
        print(f"  绝对口径 : max_abs_diff   = {ov['max_abs_diff']:.6f} pp、"
              f"mean = {ov['mean_abs_diff']:.6f} pp   <-- 量纲与输入一致，更有意义")
        if ov.get("saturated_periods"):
            print(f"  贴零期   : {ov['saturated_periods']}（相对量已记 0，见 saturation_note）")

    print("\n[4] 断点检测")
    for br in res["breaks"]:
        mag = f"{br['magnitude']:.4f}" if br["magnitude"] is not None else "-"
        print(f"  {br['type']:<15} at={br['at']}  magnitude={mag:<10} verdict={br['verdict']}")
        print(f"      {br['evidence']}")

    print(f"\n[5] 结论 verdict = {sm['verdict']}")

    print("\n[6] 拼接结果对比表（BIS 年化 vs NBS vs 采用值）")
    bis_map = {r["period"]: r["value"] for r in bis_rows}
    nbs_map = {r["period"]: r["value"] for r in nbs_rows}
    print(f"  {'年份':<6} {'BIS(年化)':>11} {'NBS(指数-100)':>15} {'差(pp)':>9} "
          f"{'采用值':>10} {'取自':>5} {'切换点':>7}")
    for r in res["spliced"]:
        p = r["period"]
        b = bis_map.get(p)
        n = nbs_map.get(p)
        diff = (b - n) if (b is not None and n is not None) else None
        if p < "1996" or p > "2026":
            continue
        print(f"  {p:<6} {('%.3f' % b) if b is not None else '-':>11} "
              f"{('%.3f' % n) if n is not None else '-':>15} "
              f"{('%+.3f' % diff) if diff is not None else '-':>9} "
              f"{('%.3f' % r['value']) if r['value'] is not None else '-':>10} "
              f"{r['source']:>5} {'  <-- ' if r['splice_point'] else '':>7}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "basis": ("BIS WS_LONG_CPI M.CN.771（月度同比，按年均值聚合到年）拼 NBS 全国 CPI"
                  "（上年=100 指数 -100 转同比 %），重叠期按 later_wins 取 NBS"),
        "sources": {"series_a": bis_info, "series_b": nbs_info},
        "windows": {
            "series_a": {"first": bis_rows[0]["period"], "last": bis_rows[-1]["period"],
                         "n": len(bis_rows)},
            "series_b": {"first": nbs_rows[0]["period"], "last": nbs_rows[-1]["period"],
                         "n": len(nbs_rows)},
            "spliced": {"first": res["spliced"][0]["period"],
                        "last": res["spliced"][-1]["period"],
                        "n": sm["n_total"]},
        },
        "overlap": ov,
        "splice_point": res["splice_point"],
        "breaks": res["breaks"],
        "rebase": res["rebase"],
        "summary": sm,
        "spliced": res["spliced"],
    }
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  [saved] {OUT_PATH.relative_to(PROJECT_ROOT)}")

    # ------------------------------------------------------------------ #
    # [7] 三次拼接对比：none / ratio / difference（方向 C 第五轮）
    # ------------------------------------------------------------------ #
    print("\n" + "=" * 100)
    print("[7] rebase 三模式对比（none 基线 / ratio / difference）")
    print("=" * 100)
    print("  前置诊断已确认：本用例的「需桥接」由 **trend_break** 触发，而 rebase 只调水平、")
    print("  改不了斜率 —— 所以预期三种模式的 verdict **完全相同**。下面的数字是对该预期的实测。")

    runs: dict[str, dict[str, Any]] = {}
    for mode in ("none", "ratio", "difference"):
        runs[mode] = splicer.splice(bis_rows, nbs_rows, strategy=STRATEGY, rebase=mode)

    print(f"\n  {'模式':<12} {'verdict':<20} {'before':<14} {'factor':>10} {'offset':>10} "
          f"{'sanity':>7} {'applied':>8} {'effect':>12}")
    for mode in ("none", "ratio", "difference"):
        r = runs[mode]
        s = r["summary"]
        rb = r["rebase"]
        fac = f"{rb['factor']:.6f}" if (rb and rb["factor"] is not None) else "-"
        off = f"{rb['offset']:+.6f}" if (rb and rb["offset"] is not None) else "-"
        san = rb["sanity_check"] if rb else "-"
        app = str(rb["applied"]) if rb else "-"
        eff = rb["verdict_effect"] if rb else "-"
        print(f"  {mode:<12} {s['verdict']:<20} {s['verdict_before_rebase']:<14} "
              f"{fac:>10} {off:>10} {san:>7} {app:>8} {eff:>12}")

    print("\n  各模式的断点（调整后）：")
    for mode in ("none", "ratio", "difference"):
        r = runs[mode]
        brs = "、".join(f"{b['type']}={b['verdict']}" for b in r["breaks"])
        print(f"    {mode:<12} {brs}")

    # ⚠️ 关键限定：本用例里 **rebase 根本没被执行**（applied=False）。
    #
    # NBS（series_b）的跨度 2015~2025 **完全落在** BIS（series_a）的 1996~2025 之内，
    # 所以 B 的每一期都是重叠期 -> 没有非重叠期可调 -> splice() 判定无需执行。
    # 两个后果必须说清：
    #   ① 上面那张三模式表**不能**用作"rebase 修不了 trend_break"的证据 —— 它只证明"没跑"；
    #   ② 它也确实**无法**被修好：触发项是 trend_break（斜率），rebase 只动水平。
    #      rebase 的落点是 B 的非重叠期，而接缝两侧永远是「A 的接缝前一期」与
    #      「B 的重叠期值」—— 后者按 strategy 直接选原文，不受调整影响。
    r_probe = runs["ratio"]
    print(f"\n  ⚠️ 限定条件：applied={r_probe['rebase']['applied']}（{r_probe['rebase']['mode']}）")
    print(f"     原因 = {r_probe['rebase'].get('not_applied_reason')}")
    print("     结论分两层，别混：")
    print("       (a) 对这个数据形状，rebase 是**空操作** —— NBS 被 BIS 的时间跨度完全包住，")
    print("           没有非重叠期可调。所以三模式 verdict 相同**不能**当作 rebase 无效的证据。")
    print("       (b) 但 trend_break 本身 rebase 也修不了：它只调水平，不改变斜率。")
    print("           splice() 的断点检测里 trend_break 与 level_jump 是独立两项，")
    print("           rebase 只可能影响 level_jump（而这里 level_jump 本来就 ok）。")
    print("     真正验证「rebase 能修水平台阶」的是 splicer --test 的场景 [9]/[10]（合成数据）。")

    # 三个产物各落一份（none 的那份与上面 OUT_PATH 内容一致，但文件名按需求单列）
    for mode in ("none", "ratio", "difference"):
        r = runs[mode]
        p = OUT_DIR / f"cpi_bis_nbs_{mode}.json"
        obj = {
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "basis": (f"BIS WS_LONG_CPI M.CN.771（月度同比 -> 年均值）⊗ NBS 全国 CPI"
                      f"（上年=100 -100），strategy={STRATEGY}，rebase={mode}"),
            "sources": {"series_a": bis_info, "series_b": nbs_info},
            "overlap": r["overlap"],
            "splice_point": r["splice_point"],
            "breaks": r["breaks"],
            "rebase": r["rebase"],
            "summary": r["summary"],
            "spliced": r["spliced"],
        }
        p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  [saved] {p.relative_to(PROJECT_ROOT)}")

    print("\n" + "=" * 100)
    print(f"拼接完成：{sm['n_total']} 期，verdict={sm['verdict']}，"
          f"拼接点={res['splice_point']}")
    print("=" * 100)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
