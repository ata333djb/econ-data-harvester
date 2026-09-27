#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""缺失检测与**分类** —— 先分类，再决定补不补。

为什么不能直接插值
------------------
真实世界的"缺失"至少有四种完全不同的成因，它们的正确处理方式彼此不相容：

====================  ============================================  ==================
分类                   含义                                          建议动作
====================  ============================================  ==================
`true_gap`            真缺失：两侧都有观测，中间有洞                    interpolate
`not_yet_published`   未发布：尾部缺失，但序列还在正常更新               wait
`discontinued`        制度下线：尾部有长段缺失（>=3 年），不再恢复       leave_null
`series_start`        起点晚：头部缺失，序列本来就从那时才开始           leave_null
====================  ============================================  ==================

**直接做插值填补会犯大错**：把"制度下线"的序列（如城镇登记失业率 2022 起停更）
用历史外推，那不是补数据，是**伪造数据**。同理，"起点晚"的序列往前插值等于凭空
发明历史。只有 `true_gap` 和（在拿到新一期之后的）`not_yet_published` 才涉及填充，
且填充方式还不同。

分类规则与优先级（先判尾部，再判头部）
--------------------------------------
1. 缺失段触及**序列末尾** 且 年数 >= 3  -> `discontinued`
2. 缺失段触及**序列末尾** 且 年数 < 3   -> `not_yet_published`
3. 缺失段触及**序列开头**               -> `series_start`
4. 其余（两侧都有观测）                  -> `true_gap`

"年数"按网格粒度折算：月度网格 12 期算 1 年，季度 4 期算 1 年，年度 1 期算 1 年。

关于"起点晚"需要调用方给窗口（重要）
------------------------------------
`series_start` 只有在**期望网格比观测范围更宽**时才可能存在。若只从数据自身推网格
（min..max），一个 2018 年才开始发布的序列根本没有"头部缺失"可言。
因此 `series_meta["expected_periods"]` 可以显式给出窗口；不传时退化为
"从观测范围自动枚举"，此时**不会**产生 `series_start`。这一点写在 docstring 里，
免得以后有人以为分类器漏判。

输入输出
--------
输入是 normalize 输出的长表行（`period` + `value` 两个键就够，其余忽略）。
输出::

    {"series_key": "...",
     "by_period": [{"period": "2022", "state": "missing", "value": None}, ...],
     "gaps": [{"start": "2022", "end": "2024", "n_missing": 3,
               "classification": "discontinued",
               "evidence": "...", "recommended_action": "leave_null"}, ...],
     "summary": {...}}

`state` 三态：`ok`（有数值）/ `missing`（行在但值为 null）/ `absent`（行都没有）。
两者都算"缺失"，但分开记录，因为"上游返回了空值"和"上游压根没返回这一行"是不同证据。

用法
----
    .\\.venv\\Scripts\\python.exe -m econ_core.missing --test
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any, Iterable, Optional, Sequence

__all__ = [
    "MissingError",
    "classify_missing",
    "CLASSIFICATIONS",
    "RECOMMENDED_ACTIONS",
]

#: 四种分类（顺序即判定优先级，便于阅读）
CLASSIFICATIONS: tuple[str, ...] = (
    "discontinued",
    "not_yet_published",
    "series_start",
    "true_gap",
)

#: 分类 -> 建议动作（任务③的核心：动作由分类决定，不是一律插值）
RECOMMENDED_ACTIONS: dict[str, str] = {
    "discontinued": "leave_null",
    "not_yet_published": "wait",
    "series_start": "leave_null",
    "true_gap": "interpolate",
}

#: 判定为"制度下线"的最短尾部缺失年数
DISCONTINUED_MIN_YEARS: float = 3.0

#: 各类 period 形状
_RE_ANNUAL = re.compile(r"^(\d{4})$")
_RE_MONTHLY = re.compile(r"^(\d{4})-(\d{2})$")
_RE_QUARTERLY = re.compile(r"^(\d{4})-Q([1-4])$")


class MissingError(RuntimeError):
    """缺失分类层可预期的失败（period 形状不支持、混用多种粒度等）。"""


# --------------------------------------------------------------------------- #
# period 工具
# --------------------------------------------------------------------------- #

def _parse_period(p: str) -> tuple[str, tuple[int, ...]]:
    """把 period 解析成 (kind, 排序键)。支持 2015 / 2015-01 / 2015-Q1。"""
    m = _RE_ANNUAL.match(p)
    if m:
        return "annual", (int(m.group(1)),)
    m = _RE_MONTHLY.match(p)
    if m:
        return "monthly", (int(m.group(1)), int(m.group(2)))
    m = _RE_QUARTERLY.match(p)
    if m:
        return "quarterly", (int(m.group(1)), int(m.group(2)))
    raise MissingError(f"无法识别的 period 形状: {p!r}（支持 2015 / 2015-01 / 2015-Q1）")


def _periods_per_year(kind: str) -> int:
    return {"annual": 1, "monthly": 12, "quarterly": 4}[kind]


def _enumerate_periods(start: str, end: str, kind: str) -> list[str]:
    """枚举 [start, end] 内的全部 period（含端点）。"""
    out: list[str] = []
    if kind == "annual":
        for y in range(int(start), int(end) + 1):
            out.append(str(y))
    elif kind == "monthly":
        y, m = int(start[:4]), int(start[5:7])
        ey, em = int(end[:4]), int(end[5:7])
        while (y, m) <= (ey, em):
            out.append(f"{y:04d}-{m:02d}")
            m += 1
            if m > 12:
                y, m = y + 1, 1
    else:  # quarterly
        y, q = int(start[:4]), int(start[6])
        ey, eq = int(end[:4]), int(end[6])
        while (y, q) <= (ey, eq):
            out.append(f"{y:04d}-Q{q}")
            q += 1
            if q > 4:
                y, q = y + 1, 1
    return out


def _num(v: Any) -> Optional[float]:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    return None


def _auto_series_key(rows: Sequence[dict[str, Any]]) -> str:
    """从第一行推 series_key：source|region_code|indicator_id。"""
    if not rows:
        return "?"
    r = rows[0]
    bits = [str(r.get("source") or "?"), str(r.get("region_code") or ""),
            str(r.get("indicator_id") or "")]
    return "|".join(b for b in bits if b)


# --------------------------------------------------------------------------- #
# 主接口
# --------------------------------------------------------------------------- #

def _classify_run(bp: list[dict[str, Any]], i: int, j: int, kind: str) -> dict[str, Any]:
    """给一段连续缺失 [i, j] 分类（判定优先级见模块 docstring）。"""
    n_missing = j - i + 1
    span_years = n_missing / _periods_per_year(kind)
    is_head = (i == 0)
    is_tail = (j == len(bp) - 1)
    obs_idx = [k for k, x in enumerate(bp) if x["state"] == "ok"]
    first_obs = bp[obs_idx[0]]["period"] if obs_idx else None
    last_obs = bp[obs_idx[-1]]["period"] if obs_idx else None

    if is_tail:
        if (not obs_idx) or span_years >= DISCONTINUED_MIN_YEARS:
            cls = "discontinued"
            tail = f"；最后一个观测是 {last_obs}" if last_obs else "；整段网格内没有任何观测"
            thr = ("；达到 >=3 年的停更阈值" if span_years >= DISCONTINUED_MIN_YEARS
                   else "")
            evidence = (f"位于序列末尾，缺失 {n_missing} 期（约 {span_years:.2f} 年）"
                        f"{tail}{thr}")
        else:
            cls = "not_yet_published"
            evidence = (f"紧贴最后一个观测 {last_obs}，只缺 {n_missing} 期"
                        f"（约 {span_years:.2f} 年），未达 3 年停更阈值"
                        f" —— 更像新一期尚未发布")
    elif is_head:
        cls = "series_start"
        evidence = (f"位于序列开头，缺失 {n_missing} 期后才有第一个观测 {first_obs}"
                    f" —— 该序列本来就从这个时点才开始发布")
    else:
        cls = "true_gap"
        prev_obs = next(bp[k]["period"] for k in range(i - 1, -1, -1)
                        if bp[k]["state"] == "ok")
        next_obs = next(bp[k]["period"] for k in range(j + 1, len(bp))
                        if bp[k]["state"] == "ok")
        evidence = (f"两侧都有观测（{prev_obs} 与 {next_obs}），是中段空洞，"
                    f"缺失 {n_missing} 期（约 {span_years:.2f} 年）")

    return {
        "start": bp[i]["period"],
        "end": bp[j]["period"],
        "n_missing": n_missing,
        "span_years": round(span_years, 4),
        "classification": cls,
        "evidence": evidence,
        "recommended_action": RECOMMENDED_ACTIONS[cls],
    }


def classify_missing(rows: Iterable[dict[str, Any]],
                     series_meta: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """对一条长表序列做缺失检测与分类。

    :param rows: normalize 输出的长表行；只用到 `period` 与 `value` 两个键。
    :param series_meta: 可选，支持的键：

                        * `series_key` (str) —— 覆盖自动推导的键
                          （默认 `source|region_code|indicator_id`）
                        * `expected_periods` (list[str]) —— **期望网格**。
                          不给时退化为"从观测范围自动枚举"。注意：不给我就
                          **无法**判出 `series_start`（见模块 docstring）。

    :returns: `{"series_key", "by_period", "gaps", "summary"}`，见模块 docstring。
    :raises MissingError: 行不是 dict、period 形状不支持、或网格内混用粒度。
    """
    rows_list = list(rows)
    meta = dict(series_meta or {})

    values: dict[str, Any] = {}
    for idx, r in enumerate(rows_list):
        if not isinstance(r, dict):
            raise MissingError(f"第 {idx} 行不是 dict: {type(r).__name__} -> {r!r}")
        p = r.get("period")
        if p in (None, ""):
            continue
        values[str(p)] = r.get("value")

    series_key = str(meta.get("series_key") or _auto_series_key(rows_list))
    observed = sorted(values.keys(), key=lambda p: _parse_period(p)[1])

    explicit = meta.get("expected_periods")
    if explicit:
        expected = [str(p) for p in explicit]
    elif observed:
        expected = _enumerate_periods(observed[0], observed[-1], _parse_period(observed[0])[0])
    else:
        expected = []

    kinds = {_parse_period(p)[0] for p in expected}
    if len(kinds) > 1:
        raise MissingError(f"期望网格里混用了多种 period 粒度: {sorted(kinds)}")

    if not expected:
        return {
            "series_key": series_key,
            "by_period": [],
            "gaps": [],
            "summary": {
                "n_periods": 0, "n_ok": 0, "n_missing": 0, "n_gaps": 0,
                "by_classification": {}, "period_kind": None,
                "period_range": [None, None],
                "first_observation": None, "last_observation": None,
                "note": "既没有观测、也没有显式 expected_periods，无法判定缺失",
            },
        }

    kind = next(iter(kinds))
    expected = sorted(expected, key=lambda p: _parse_period(p)[1])

    by_period: list[dict[str, Any]] = []
    for p in expected:
        if p in values:
            v = values[p]
            by_period.append({"period": p,
                              "state": "ok" if _num(v) is not None else "missing",
                              "value": v})
        else:
            by_period.append({"period": p, "state": "absent", "value": None})

    gaps: list[dict[str, Any]] = []
    i = 0
    while i < len(by_period):
        if by_period[i]["state"] == "ok":
            i += 1
            continue
        j = i
        while j + 1 < len(by_period) and by_period[j + 1]["state"] != "ok":
            j += 1
        gaps.append(_classify_run(by_period, i, j, kind))
        i = j + 1

    n_ok = sum(1 for x in by_period if x["state"] == "ok")
    n_absent = sum(1 for x in by_period if x["state"] == "absent")
    by_cls: dict[str, int] = {}
    for g in gaps:
        by_cls[g["classification"]] = by_cls.get(g["classification"], 0) + 1

    return {
        "series_key": series_key,
        "by_period": by_period,
        "gaps": gaps,
        "summary": {
            "n_periods": len(by_period),
            "n_ok": n_ok,
            "n_missing": len(by_period) - n_ok,
            "n_explicit_null": len(by_period) - n_ok - n_absent,
            "n_absent_rows": n_absent,
            "n_gaps": len(gaps),
            "by_classification": by_cls,
            "period_kind": kind,
            "period_range": [expected[0], expected[-1]],
            "first_observation": next((x["period"] for x in by_period if x["state"] == "ok"), None),
            "last_observation": next((x["period"] for x in reversed(by_period) if x["state"] == "ok"), None),
        },
    }


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #

# 下面四组行数据都用**实测回来的真实数值**，只是把窗口/个别年份按要求摆放，
# 以便在离线、确定性的前提下覆盖四种分类：
#   * REGISTERED_ROWS  —— NBS 城镇登记失业率 (%)，2015-2021 有值、2022-2024 为空串（实测）
#   * SURVEYED_ROWS    —— NBS 全国城镇调查失业率 (%) 月度->年均值，实测只有 2018 起
#   * CPI_CITY_ROWS    —— NBS 居民消费价格指数 yData[1]（城市），2025 值为 null（实测）
#   * IMF_LUR_ROWS     —— IMF LUR / CHN，实测从 2017 开始（行本身缺失，非 null）
#   * MIDDLE_HOLE_ROWS —— **合成**：在 REGISTERED_ROWS 基础上把 2019 置空，用来覆盖
#                         true_gap 分支（当前数据集里没有真实的中段空洞）
_Y15_25 = [str(y) for y in range(2015, 2026)]
_Y15_24 = [str(y) for y in range(2015, 2025)]

REGISTERED_ROWS = [
    {"period": "2015", "value": 4.1}, {"period": "2016", "value": 4.0},
    {"period": "2017", "value": 3.9}, {"period": "2018", "value": 3.8},
    {"period": "2019", "value": 3.6}, {"period": "2020", "value": 4.2},
    {"period": "2021", "value": 4.0}, {"period": "2022", "value": None},
    {"period": "2023", "value": None}, {"period": "2024", "value": None},
]
SURVEYED_ROWS = [
    {"period": "2018", "value": 4.933}, {"period": "2019", "value": 5.150},
    {"period": "2020", "value": 5.617}, {"period": "2021", "value": 5.117},
    {"period": "2022", "value": 5.583}, {"period": "2023", "value": 5.217},
    {"period": "2024", "value": 5.117},
]
CPI_CITY_ROWS = [
    {"period": "2015", "value": 101.5}, {"period": "2016", "value": 102.1},
    {"period": "2017", "value": 101.7}, {"period": "2018", "value": 102.1},
    {"period": "2019", "value": 102.8}, {"period": "2020", "value": 102.3},
    {"period": "2021", "value": 101.0}, {"period": "2022", "value": 102.0},
    {"period": "2023", "value": 100.3}, {"period": "2024", "value": 100.2},
    {"period": "2025", "value": None},
]
IMF_LUR_ROWS = [
    {"period": "2017", "value": 5.0}, {"period": "2018", "value": 4.9},
    {"period": "2019", "value": 5.2}, {"period": "2020", "value": 5.6},
    {"period": "2021", "value": 5.1}, {"period": "2022", "value": 5.6},
    {"period": "2023", "value": 5.2}, {"period": "2024", "value": 5.1},
]
MIDDLE_HOLE_ROWS = [
    dict(r, value=None) if r["period"] == "2019" else dict(r)
    for r in REGISTERED_ROWS
]


def _selftest() -> int:
    print("=" * 92)
    print("self-test: missing.classify_missing（四种分类各至少一个案例）")
    print("=" * 92)

    cases: list[tuple[str, list[dict[str, Any]], dict[str, Any], str]] = [
        ("A 真缺失 true_gap（合成：真实登记失业率序列上把 2019 置空）",
         MIDDLE_HOLE_ROWS, {"series_key": "synthetic|nbs_registered_2019_blanked",
                            "expected_periods": _Y15_24}, "true_gap"),
        ("B 未发布 not_yet_published（真实：CPI 城市 2025=null）",
         CPI_CITY_ROWS, {"series_key": "nbscpi|city", "expected_periods": _Y15_25},
         "not_yet_published"),
        ("C 制度下线 discontinued（真实：登记失业率 2022-2024）",
         REGISTERED_ROWS, {"series_key": "nbs|registered", "expected_periods": _Y15_24},
         "discontinued"),
        ("D 起点晚 series_start（真实：调查失业率 2018 起，窗口 2015-2024）",
         SURVEYED_ROWS, {"series_key": "nbs|surveyed", "expected_periods": _Y15_24},
         "series_start"),
        ("E 起点晚 series_start（真实：IMF LUR 从 2017 起，且是缺行不是 null）",
         IMF_LUR_ROWS, {"series_key": "imf|lur", "expected_periods": _Y15_24},
         "series_start"),
    ]

    failures: list[str] = []
    for label, rows, meta, want in cases:
        res = classify_missing(rows, meta)
        s = res["summary"]
        got = [g["classification"] for g in res["gaps"]]
        print()
        print("-" * 92)
        print(f"CASE {label}")
        print(f"  series_key = {res['series_key']}")
        print(f"  网格 {s['period_range'][0]}..{s['period_range'][-1]}"
              f"（{s['period_kind']}，{s['n_periods']} 期）"
              f"  有值={s['n_ok']}  缺失={s['n_missing']}"
              f"（显式 null {s['n_explicit_null']} + 缺行 {s['n_absent_rows']}）")
        if not res["gaps"]:
            print("  （无缺口）")
        for g in res["gaps"]:
            print(f"  缺口 {g['start']}..{g['end']}  n={g['n_missing']}"
                  f"（约 {g['span_years']} 年）-> {g['classification']}"
                  f"  建议动作={g['recommended_action']}")
            print(f"      evidence: {g['evidence']}")
        ok = want in got
        print(f"  期望包含 {want!r}: {'PASS' if ok else 'FAIL'}（实际 {got}）")
        if not ok:
            failures.append(label)

    print()
    print("=" * 92)
    if failures:
        print(f"self-test 失败: {failures}")
        print("=" * 92)
        return 1
    print("self-test 完成 ✔（4 种分类全部覆盖）")
    print("=" * 92)
    return 0


def _main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    p = argparse.ArgumentParser(prog="missing", description="缺失检测与分类")
    p.add_argument("--test", action="store_true", help="跑自检")
    a = p.parse_args(argv)
    if a.test:
        return _selftest()
    p.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())


