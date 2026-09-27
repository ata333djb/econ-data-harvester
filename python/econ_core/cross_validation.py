#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨源交叉验证 —— 最小可用版（第二阶段）。

定位
----
拿两条**已 normalize 的长表行**序列（例如 NBS 与 World Bank 的同一指标），
按某个键（默认 @@period@@）对齐，逐期算差异率并给出判定，最后汇总一个
"能不能直接拼接"的结论。

判定阈值（差异率 = |A - B| / |B|，分母取 B 侧即"参照源"）
--------------------------------------------------------
=========================  ==========  ================================
区间                        行判定      含义
=========================  ==========  ================================
< 0.5%                     一致        可直接拼接
0.5% ~ 2%                  可接受      差异可归因于舍入/口径微调
2% ~ 5%                    警告        需人工复核
> 5%                       冲突        不可拼接
=========================  ==========  ================================

注意：需求文档里 rows[].verdict 写的是"一致/警告/冲突"三值，但阈值表给的是
四段。二者不自洽，本模块**以阈值表为准**（四段），并额外在 summary 里用
"可直接拼接/需人工复核/不可拼接"三值回答拼接问题。

单位为上：本模块不做单位推断，只提供 @@unit_factor_to_yuan@@ 与
@@rescale_series@@ 两个工具，由调用方（如 tools/compare-gdp.py）显式对齐后再比对，
避免"偷偷换算"掩盖真实口径差异。

用法
----
    # 自检（合成序列，覆盖全部四段阈值 + 缺值 + 无重叠）
    .\\.venv\\Scripts\\python.exe -m econ_core.cross_validation --test

    # 库用法
    from econ_core.cross_validation import compare_series, unit_factor_to_yuan
    result = compare_series(nbs_rows, wb_rows, key="period", value="value")
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any, Iterable, Optional, Sequence

__all__ = [
    "CrossValidationError",
    "compare_series",
    "unit_factor_to_yuan",
    "rescale_series",
    "THRESHOLDS",
    "UNIT_TO_YUAN",
    "VERDICT_OK",
    "VERDICT_WARN",
    "VERDICT_BAD",
]

#: 行判定的四段阈值：(上界, 标签)，按上界升序；超过最后一段上界即"冲突"
THRESHOLDS: tuple[tuple[float, str], ...] = (
    (0.005, "一致"),
    (0.02, "可接受"),
    (0.05, "警告"),
)

#: 超过最后一段阈值时的行判定
VERDICT_CONFLICT: str = "冲突"

#: 有值但缺一侧数值时的行判定
VERDICT_MISSING: str = "缺值"

#: summary 的三值结论
VERDICT_OK: str = "可直接拼接"
VERDICT_WARN: str = "需人工复核"
VERDICT_BAD: str = "不可拼接"

#: summary 判定用的阈值（与四段边界一致：<2% 可拼接，<=5% 复核，>5% 不可拼接）
SUMMARY_OK_BELOW: float = 0.02
SUMMARY_WARN_MAX: float = 0.05

#: 常见人民币单位的"合多少元"系数
UNIT_TO_YUAN: dict[str, float] = {
    "元": 1.0,
    "人民币元": 1.0,
    "CNY": 1.0,
    "千元": 1.0e3,
    "万元": 1.0e4,
    "十万元": 1.0e5,
    "百万元": 1.0e6,
    "亿元": 1.0e8,
    "十亿元": 1.0e9,
    "万亿元": 1.0e12,
}


class CrossValidationError(RuntimeError):
    """交叉验证层可预期的失败（输入不是长表行、键缺失等）。"""


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #

def _sort_key(s: str) -> tuple[tuple[int, ...], str]:
    """期间排序键：先抽数字段（@@2020-01@@ -> (2020, 1)），再退回原串，保证稳定。"""
    return (tuple(int(x) for x in re.findall(r"\d+", s)), s)


def _verdict_of_rate(rate: Optional[float]) -> str:
    """按四段阈值给单行判定；rate 为 None 表示缺值。"""
    if rate is None:
        return VERDICT_MISSING
    for upper, label in THRESHOLDS:
        if rate < upper:
            return label
    if rate <= SUMMARY_WARN_MAX:
        return "警告"
    return VERDICT_CONFLICT


def _is_number(v: Any) -> bool:
    """bool 是 int 的子类，必须排除，否则 True 会被当 1.0 参与比对。"""
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _index_series(series: Iterable[dict[str, Any]], key: str, value: str,
                  label: str) -> tuple[dict[str, Any], list[str]]:
    """把长表行按 key 建成 @@{key_str: value}@@，返回 (索引, 重复键列表)。

    键为 None 的行跳过（无法对齐，不是错误）。
    """
    idx: dict[str, Any] = {}
    dups: list[str] = []
    for i, row in enumerate(series):
        if not isinstance(row, dict):
            raise CrossValidationError(
                f"{label} 第 {i} 条不是 dict: {type(row).__name__} -> {row!r}"
            )
        k = row.get(key)
        if k is None:
            continue
        ks = str(k)
        if ks in idx:
            dups.append(ks)
        idx[ks] = row.get(value)
    return idx, dups


# --------------------------------------------------------------------------- #
# 主接口
# --------------------------------------------------------------------------- #

def compare_series(series_a: Iterable[dict[str, Any]],
                   series_b: Iterable[dict[str, Any]],
                   key: str = "period", value: str = "value") -> dict[str, Any]:
    """逐期比对两条长表序列，返回差异表与汇总判定。

    :param series_a: A 侧长表行（如 NBS），每条至少含 @@key@@ 与 @@value@@ 字段。
    :param series_b: B 侧长表行（如 World Bank）。**差异率的分母取 B 侧**，
                     即把 B 当作参照源；只关心绝对差异时该选择无影响。
    :param key: 对齐键，默认 @@period@@。
    :param value: 数值字段名，默认 @@value@@。
    :returns: 形如::

        {
          "overlap_periods": ["2015", ...],      # 两侧共有的期间（按时间排序）
          "common_keys":     ["2015", ...],      # 同上（同一份数据的别名，便于调用方取用）
          "rows": [
            {"period": "2020", "value_a": 1034867.6, "value_b": 1034867.6,
             "diff": 0.0, "diff_rate": 0.0, "verdict": "一致"}, ...
          ],
          "summary": {
            "n_common": 10, "n_compared": 10,
            "max_diff_rate": 0.0, "mean_diff_rate": 0.0,
            "verdict": "可直接拼接"
          },
          "thresholds": {...}, "notes": [...]
        }

        数值缺失（一侧为 None）的行仍会列出，verdict 为 @@缺值@@，
        且**不参与** max/mean 统计。
    :raises CrossValidationError: 输入不是 dict 行。
    """
    idx_a, dups_a = _index_series(series_a, key, value, "series_a")
    idx_b, dups_b = _index_series(series_b, key, value, "series_b")

    common = sorted(set(idx_a) & set(idx_b), key=_sort_key)

    rows: list[dict[str, Any]] = []
    for k in common:
        va, vb = idx_a[k], idx_b[k]
        diff: Optional[float] = None
        rate: Optional[float] = None
        if _is_number(va) and _is_number(vb):
            diff = float(va) - float(vb)
            rate = abs(diff) / abs(float(vb)) if float(vb) != 0 else None
        rows.append({
            "period": k,
            "value_a": va,
            "value_b": vb,
            "diff": diff,
            "diff_rate": rate,
            "verdict": _verdict_of_rate(rate),
        })

    rates = [r["diff_rate"] for r in rows if r["diff_rate"] is not None]
    max_rate = max(rates) if rates else None
    mean_rate = (sum(rates) / len(rates)) if rates else None

    if not rows or not rates:
        verdict = VERDICT_BAD
    elif max_rate is not None and max_rate < SUMMARY_OK_BELOW:
        verdict = VERDICT_OK
    elif max_rate is not None and max_rate <= SUMMARY_WARN_MAX:
        verdict = VERDICT_WARN
    else:
        verdict = VERDICT_BAD

    only_a = sorted(set(idx_a) - set(idx_b), key=_sort_key)
    only_b = sorted(set(idx_b) - set(idx_a), key=_sort_key)
    n_missing = sum(1 for r in rows if r["verdict"] == VERDICT_MISSING)

    notes: list[str] = []
    if not rows:
        notes.append("两侧无任何重叠期间，无法比对。")
    if n_missing:
        notes.append(f"{n_missing} 个重叠期间有一侧数值缺失（verdict=缺值），已排除出统计。")
    if only_a:
        notes.append(f"A 侧独有 {len(only_a)} 个期间: {only_a[:20]}")
    if only_b:
        notes.append(f"B 侧独有 {len(only_b)} 个期间: {only_b[:20]}")
    if dups_a:
        notes.append(f"A 侧存在重复键（后者覆盖前者）: {sorted(set(dups_a))[:20]}")
    if dups_b:
        notes.append(f"B 侧存在重复键（后者覆盖前者）: {sorted(set(dups_b))[:20]}")
    notes.append("差异率分母为 B 侧数值；单位必须先对齐（见 unit_factor_to_yuan）。")

    return {
        "overlap_periods": common,
        "common_keys": common,
        "rows": rows,
        "summary": {
            "n_common": len(common),
            "n_compared": len(rates),
            "max_diff_rate": max_rate,
            "mean_diff_rate": mean_rate,
            "verdict": verdict,
        },
        "thresholds": {
            "一致": "< 0.5%",
            "可接受": "0.5% ~ 2%",
            "警告": "2% ~ 5%",
            "冲突": "> 5%",
            "summary_rule": "< 2% 可直接拼接 / <= 5% 需人工复核 / > 5% 不可拼接",
        },
        "notes": notes,
    }


# --------------------------------------------------------------------------- #
# 单位对齐工具
# --------------------------------------------------------------------------- #

def unit_factor_to_yuan(unit: Any) -> Optional[float]:
    """把单位文本映射成"合多少元"的系数；认不出来返回 None（不猜）。

    先精确匹配 @@UNIT_TO_YUAN@@，再按"最长键优先"做子串匹配，
    因此 @@人民币亿元@@ / @@亿元(现价)@@ 这类带修饰的串也能命中。
    """
    s = str(unit or "").strip()
    if not s:
        return None
    if s in UNIT_TO_YUAN:
        return UNIT_TO_YUAN[s]
    for k in sorted(UNIT_TO_YUAN, key=len, reverse=True):
        if k in s:
            return UNIT_TO_YUAN[k]
    return None


def rescale_series(series: Iterable[dict[str, Any]], factor: float,
                   value: str = "value") -> list[dict[str, Any]]:
    """把序列的数值字段整体乘 @@factor@@，返回**新的**行列表（不改原对象）。

    非数值（None 等）原样保留。
    """
    out: list[dict[str, Any]] = []
    for row in series:
        r = dict(row)
        v = r.get(value)
        if _is_number(v):
            r[value] = float(v) * factor
        out.append(r)
    return out


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #

def _series(scale: float = 1.0, years: Sequence[int] = tuple(range(2015, 2025)),
            none_year: Optional[int] = None) -> list[dict[str, Any]]:
    """造一段合成长表：基准 1000.0/年，可整体缩放、可指定某年缺值。"""
    rows: list[dict[str, Any]] = []
    for y in years:
        v: Optional[float] = 1000.0 * scale
        if none_year is not None and y == none_year:
            v = None
        rows.append({"period": str(y), "value": v, "source": "synthetic"})
    return rows


def _selftest() -> int:
    print("=" * 78)
    print("self-test: cross_validation（合成序列，覆盖四段阈值 + 缺值 + 无重叠）")
    print("=" * 78)

    base = _series(1.0)
    cases = [
        ("完全相同 (0%)", base, _series(1.0)),
        ("+0.3% -> 一致", base, _series(1.003)),
        ("+1.0% -> 可接受", base, _series(1.01)),
        ("+3.0% -> 警告", base, _series(1.03)),
        ("+10.0% -> 冲突", base, _series(1.10)),
        ("+5.0% (边界) -> 警告", base, _series(1.05)),
    ]

    failures = 0
    for label, sa, sb in cases:
        res = compare_series(sa, sb)
        s = res["summary"]
        print("-" * 78)
        print(f"CASE  {label}")
        print(f"  n_common={s['n_common']} n_compared={s['n_compared']} "
              f"max_diff_rate={s['max_diff_rate']:.6f} mean_diff_rate={s['mean_diff_rate']:.6f}")
        print(f"  行判定集合: {sorted({r['verdict'] for r in res['rows']})}")
        print(f"  summary.verdict: {s['verdict']}")
        print(f"  首行: {json.dumps(res['rows'][0], ensure_ascii=False)}")

    # 缺值
    print("-" * 78)
    res = compare_series(_series(1.0, none_year=2016), base)
    print("CASE  A 侧 2016 缺值")
    print(f"  n_common={res['summary']['n_common']} n_compared={res['summary']['n_compared']}")
    print(f"  2016 行: {json.dumps([r for r in res['rows'] if r['period'] == '2016'][0], ensure_ascii=False)}")
    print(f"  notes: {json.dumps(res['notes'], ensure_ascii=False)}")
    if res["summary"]["n_compared"] != 9:
        print("  X n_compared 应为 9")
        failures += 1
    else:
        print("  OK 缺值行已排除出统计")

    # 无重叠
    print("-" * 78)
    res = compare_series(_series(1.0, years=(2015, 2016, 2017)),
                         _series(1.0, years=(2020, 2021, 2022)))
    print("CASE  无重叠期间")
    print(f"  n_common={res['summary']['n_common']} verdict={res['summary']['verdict']}")
    print(f"  notes: {json.dumps(res['notes'], ensure_ascii=False)}")
    if res["summary"]["verdict"] != VERDICT_BAD:
        print("  X 无重叠应为不可拼接")
        failures += 1
    else:
        print("  OK 无重叠 -> 不可拼接")

    # 单位工具
    print("-" * 78)
    print("CASE  unit_factor_to_yuan / rescale_series")
    for u in ("亿元", "万元", "元", "人民币亿元", "GDP (current LCU)", ""):
        print(f"  unit_factor_to_yuan({u!r}) = {unit_factor_to_yuan(u)!r}")
    scaled = rescale_series([{"period": "2020", "value": 1.0e8}], 1.0 / 1.0e8)
    print(f"  1e8 元 -> rescale(1/1e8) = {scaled[0]['value']!r}")
    if unit_factor_to_yuan("亿元") != 1.0e8 or scaled[0]["value"] != 1.0:
        print("  X 单位工具结果不符")
        failures += 1
    else:
        print("  OK 单位工具结果正确")

    print()
    print("=" * 78)
    print("self-test 完成" if failures == 0 else f"self-test 失败项: {failures}")
    print("=" * 78)
    return 0 if failures == 0 else 1


def _main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    p = argparse.ArgumentParser(prog="cross_validation",
                                description="跨源交叉验证（最小可用版）")
    p.add_argument("--test", action="store_true", help="跑自检（合成序列）")
    a = p.parse_args(argv)
    if a.test:
        try:
            return _selftest()
        except CrossValidationError as exc:
            print(f"[FAIL] {exc}", file=sys.stderr)
            return 2
    p.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())



