#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""splicer —— 序列拼接器：把两条同指标序列接成一条长序列，并检查拼接断点。

为什么需要它
------------
PROJECT_STATE 长期挂着一条自我批评：**系统至今从未真正拼接**过序列。
方向 A 第四轮的「拼接断点检查」一直空转，因为没有真场景。

方向 C 第三轮接进 BIS 后，场景出现了：BIS 的 `WS_LONG_CPI` 是它**自己拼接**
出来的长序列（官方自述 *"constructed long consumer price indices, by joining the
series available for consecutive periods"*），而校验它拼得好不好、以及把 BIS 序列
与本项目 NBS 序列接起来，都需要一个可复用的拼接器。

本模块只做三件事，**不做**其它：

1. 按策略在重叠期选一条（`earlier_wins` / `later_wins`）
2. 报出重叠期的一致性（差异率）
3. 在拼接点附近做三类断点检测，给出三值 verdict

**不做**的事（刻意）：不做水平调整（ratio splice / rebasing）、不做插值、
不做频率转换。要把月度转年度，请在**调用方**先聚合好再送进来 —— 这样拼接器
只面对「同一频率、同一口径」的两条序列，语义边界清楚。

三类断点与阈值（都按「相对量」，所以无量纲、跨指标可比）
-------------------------------------------------------

``level_jump``（水平跳跃）
    拼接点两侧**相邻两期**的相对跳变::

        |v[first_after] - v[last_before]| / |v[last_before]|

    > 0.05 -> warn；> 0.10 -> reject。这是最要命的一类：两条序列基准年不同、
    口径不同，接上去会留下一个台阶，而台阶会被下游当成"真实变化"。

    **但它必须与「该序列自身的日常步长」对比才有意义** —— 只看绝对相对跳变会误判。
    实测踩到：中国 CPI 同比 2014 年 2.06% -> 2015 年 1.4% 是**数据本身的真实变化**
    （那年通胀确实掉下来了），相对 2.06 却是 32% 跳变，于是被误判 reject
    「不可拼接」，把正常年际波动说成了拼接台阶。

    所以本模块**同时**给出 ``excess_ratio`` = 绝对跳变 / 接缝前 ``BASELINE_WINDOW``
    期内**同一来源内部**逐步变化的中位数。判定规则：先按上面的阈值给等级，
    若等级非 ok **且** ``excess_ratio <= 1.0``（跳变没超过该序列的日常步长），
    降为 ``ok`` —— 那不是拼接伪影。这正好让"人工造的 5% 台阶"（序列本来平坦，
    步长约 0）照旧被判出来，而"真实的高波动指标跨年变化"不被冤枉。

``trend_break``（趋势断裂）
    拼接点前 3 期与后 3 期各拟合一条直线（最小二乘），比较斜率：:

        |slope_after - slope_before| / max(|slope_before|, eps)

    > 0.50 -> warn；> 1.50 -> reject。阈值比 level_jump 宽，因为 3 点斜率本身
    抖动大；它的作用是抓「方向反了」或「量级差一个数量级」这种明显断裂。

``variance_shift``（方差漂移）
    拼接点前 3 期与后 3 期的**样本标准差**之比::

        max(sd_before, sd_after) / max(min(sd_before, sd_after), eps)

    > 3.0 -> warn；> 10.0 -> reject。抓的是"接上之后波动结构换了"，
    例如从年度平滑序列接到月度噪声序列。

三值 verdict（与 arbiter / cross_validation 的措辞保持一致）
----------------------------------------------------------

* 全部 `ok` -> ``可直接拼接``
* 有 `warn` 无 `reject` -> ``需桥接``
* 有 `reject` -> ``不可拼接``
* **无重叠期** -> 额外记 ``no_overlap_check``：没有任何共同期做对照，
  只能纯接。此时 verdict 仍按断点检测给，但 ``overlap.check`` 字段会写明
  「未做重叠校验」，避免被误读成"验过了没问题"。

重叠期差异率的口径（``overlap.max_diff_rate`` / ``mean_diff_rate``）
------------------------------------------------------------------

    |v_a - v_b| / max(|v_a|, |v_b|)

**分母取两者绝对值的较大者**，这样分子分母同量纲、结果落在 [0, 1]，两侧都不会
除零。但数值贴着 0 时相对量会爆成假象（实测踩到：NBS 2025 年同比 0.0 vs BIS
0.051，相对量算出 100%「最大分歧」，其实绝对差只有 0.05 pp）。所以分母
``<= NEAR_ZERO_DENOM`` 的期**改用绝对差**，并把期号记进 ``near_zero_periods`` /
``near_zero_note`` —— 读的人必须知道那几期的口径不同。

用法
----

    $env:PYTHONPATH="python"; ./.venv/Scripts/python.exe -m econ_core.splicer --test

    from econ_core.splicer import splice
    res = splice(rows_a, rows_b, strategy="later_wins")
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

__all__ = [
    "SpliceError",
    "splice",
    "rebase",
    "rebase_series",
    "LEVEL_JUMP_WARN",
    "LEVEL_JUMP_REJECT",
    "TREND_WARN",
    "TREND_REJECT",
    "VARIANCE_WARN",
    "VARIANCE_REJECT",
    "VERDICT_OK",
    "VERDICT_BRIDGE",
    "VERDICT_REJECT",
    "NO_OVERLAP_CHECK",
]

# --------------------------------------------------------------------------- #
# 常量与阈值
# --------------------------------------------------------------------------- #

LEVEL_JUMP_WARN: float = 0.05
LEVEL_JUMP_REJECT: float = 0.10
TREND_WARN: float = 0.50
TREND_REJECT: float = 1.50
VARIANCE_WARN: float = 3.0
VARIANCE_REJECT: float = 10.0

VERDICT_OK: str = "可直接拼接"
VERDICT_BRIDGE: str = "需桥接"
VERDICT_REJECT: str = "不可拼接"

#: 无重叠期时的标记（不是 verdict，是 overlap.check 的取值）
NO_OVERLAP_CHECK: str = "no_overlap_check"

#: 断点检测窗口（拼接点前后各取几期做趋势/方差）
WINDOW: int = 3

#: 除零保护
_EPS: float = 1e-12

#: 相对量（``overlap.max_diff_rate`` / ``mean_diff_rate``）的**分母下限**。
#:
#: 相对量 t = |a-b| / max(|a|,|b|) 恒在 [0,1]（分母取了较大者），但它会在**分母贴近 0**
#: 时饱和到 1.0 —— 实测踩到：NBS 2025 年同比 0.0 vs BIS 0.051 -> t=1.0，看着像
#: "分歧 100%"，其实绝对差只有 0.051 pp，因为两条序列的数值都贴着 0。
#:
#: 判据不拿固定阈值去比分母本身（0.051 > 0.01 会漏判），而是：**两条序列的数值都小于
#: 这个下限时，该期的相对量记 0 并进 `saturated_periods`**，改看绝对量。
#: 换句话说这个常量不是"分母阈值"，是"这一期整体是不是贴在零点附近"的判据。
#:
#: 0.5 是**为百分点量纲的指标标定的**（中国 CPI 同比在 0~6 之间，0.5 以下算贴近 0）。
#: 它**不普适**：换成亿元量纲的序列（GDP）时，贴着 0 的判据要相应放大。
#: 之所以不做成自适应（用序列自身量级当尺），是因为实测那样会反过来漏判 ——
#: 一条一路降到 0 的序列，中位量级本身也小，判据就失效了。宁可少报也不要误报。
RELATIVE_METRIC_FLOOR: float = 0.5

#: level_jump 的「平庸基线」窗口：用拼接点前这么多个期内、**同一来源内部**的
#: 逐步变化中位数作基准。36 期对月度序列是 3 年，对年度序列是 36 年（取到多少算多少）。
BASELINE_WINDOW: int = 36

#: 合法的拼接策略
STRATEGIES: tuple[str, ...] = ("later_wins", "earlier_wins")

#: rebase 模式：none 不改，ratio 乘，difference 加
REBASE_MODES: tuple[str, ...] = ("none", "ratio", "difference")

#: ratio 模式的 sanity 区间：factor 必须落在 [MIN, MAX]，否则判 fail 并放弃调整。
#: 依据：比值调整只该修"同一口径的轻微系统性偏移"（几个百分点到几十个百分点）；
#: 乘数翻倍或腰斩说明两条序列**根本不是同一量级**，那是拼错了序列而不是需要桥接。
RATIO_FACTOR_MIN: float = 0.5
RATIO_FACTOR_MAX: float = 2.0


class SpliceError(RuntimeError):
    """拼接层的可预期失败（入参形状不对、策略未知等）。"""


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #

def _as_float(v: Any) -> Optional[float]:
    """把 value 规整成 float；null / 空串 / 非数字都返回 None（缺失不参与计算）。"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        f = float(v)
        return f if math.isfinite(f) else None
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        try:
            f = float(s)
        except ValueError:
            return None
        return f if math.isfinite(f) else None
    return None


def _index(rows: Sequence[dict[str, Any]], label: str) -> dict[str, dict[str, Any]]:
    """把长表行列表变成 {period: row}，要求有 period / value / series_key。

    :raises SpliceError: 缺字段、period 重复、或一条有效观测都没有。
    """
    if not isinstance(rows, (list, tuple)) or not rows:
        raise SpliceError(f"{label}: 必须是至少含一条记录的行列表（收到 {type(rows).__name__}）")
    out: dict[str, dict[str, Any]] = {}
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            raise SpliceError(f"{label}[{i}]: 不是 dict，而是 {type(r).__name__}")
        for k in ("period", "value", "series_key"):
            if k not in r:
                raise SpliceError(f"{label}[{i}]: 缺字段 {k!r}（长表行必须有 period / value / series_key）")
        period = str(r["period"])
        if not period:
            raise SpliceError(f"{label}[{i}]: period 是空串")
        if period in out:
            raise SpliceError(
                f"{label}: period 重复（{period!r}）—— 拼接器要求每条序列内部 period 唯一，"
                f"否则无法判断每一期是谁的")
        out[period] = r
    if not out:
        raise SpliceError(f"{label}: 没有任何有效期")
    return out


def _sorted_periods(*mappings: dict[str, dict[str, Any]]) -> list[str]:
    """把所有序列的 period 并集排序。

    排序规则：长度优先，再字典序。理由是本项目只出现 ``YYYY`` / ``YYYY-MM`` /
    ``YYYY-Qn`` 三种粒度；同粒度下字典序等于时间序，跨粒度时长度优先能让
    ``1995`` 排在 ``1995-01`` 前面（虽然同一条拼接里不该混粒度，但并集时不至于乱）。
    """
    union: set[str] = set()
    for m in mappings:
        union.update(m.keys())
    return sorted(union, key=lambda p: (len(p), p))


def _slope(periods: Sequence[str], values: Sequence[float]) -> Optional[float]:
    """最小二乘斜率（x 用下标 0..n-1，不解析日期 —— 等间距假设由调用方保证）。"""
    n = len(values)
    if n < 2:
        return None
    mean_x = (n - 1) / 2.0
    mean_y = sum(values) / n
    num = sum((i - mean_x) * (v - mean_y) for i, v in enumerate(values))
    den = sum((i - mean_x) ** 2 for i in range(n))
    if abs(den) < _EPS:
        return None
    return num / den


def _stdev(values: Sequence[float]) -> Optional[float]:
    """样本标准差（n-1）；不足 2 个点返回 None。"""
    n = len(values)
    if n < 2:
        return None
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    return math.sqrt(var)


def _median(values: Sequence[float]) -> Optional[float]:
    """中位数；空列表返回 None。"""
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def _verdict_of(breaks: Sequence[dict[str, Any]]) -> str:
    """三值 verdict：有 reject -> 不可拼接；有 warn -> 需桥接；否则可直接拼接。"""
    types = {str(b.get("verdict")) for b in breaks}
    if "reject" in types:
        return VERDICT_REJECT
    if "warn" in types:
        return VERDICT_BRIDGE
    return VERDICT_OK


def _grade(value: Optional[float], warn: float, reject: float) -> str:
    """按阈值给出 ok / warn / reject；无法计算时返回 unknown（不参与 verdict）。

    **边界语义是严格的 `>`**（严格大于），与需求里写的 ``> 0.05 -> warn`` 一致：

        value <= warn   -> ok
        warn < value <= reject -> warn
        value > reject  -> reject

    实测提醒：人工构造的台阶如果正好落在阈值上（例如 ``|105-100|/100`` 想造 5%，
    实际浮点结果是 0.04999999999999999）会判 **ok**，很容易让人以为"检测失灵"。
    要造明确落在 warn 带的夹具，取 6%~9%（如 100 -> 106）更稳。
    """
    if value is None:
        return "unknown"
    if value > reject:
        return "reject"
    if value > warn:
        return "warn"
    return "ok"


# --------------------------------------------------------------------------- #
# 公开接口
# --------------------------------------------------------------------------- #

def _detect_breaks(spliced: Sequence[dict[str, Any]],
                   first_b_period: Optional[str]) -> list[dict[str, Any]]:
    """在拼接点附近做三类断点检测，返回 breaks 列表。

    **抽成独立函数的理由**：rebase 前、rebase 后各要跑一次（`rebase.before_breaks` /
    `after_breaks`），两处必须是**同一套实现**，否则"调整前后对比"就失去可比性。

    :param spliced: 已定源的拼接结果行（``[{period, value, source, series_key, splice_point}]``）。
    :param first_b_period: 拼接点（来源切换的第一期）；None 表示没有切换点 -> 不做检测。
    :returns: breaks 列表，每项含 ``at`` / ``type`` / ``magnitude`` / ``verdict`` / ``evidence``。
    """
    breaks: list[dict[str, Any]] = []
    if first_b_period is None:
        return breaks

    i = next(k for k, r in enumerate(spliced) if r["period"] == first_b_period)
    before = spliced[max(0, i - WINDOW):i]
    after = spliced[i:i + WINDOW]

    # 3a) level_jump：紧邻两期
    #
    # 关键设计：**同时**报「绝对相对跳变」与「相对该序列自身正常步长的超额倍数」。
    # 只看前者会误判 —— 实测踩到：中国 CPI 同比 2014 年 2.06% -> 2015 年 1.4%
    # 是**数据本身的真实变化**（那年通胀确实掉下来了），但相对 2.06 是 32% 跳变，
    # 于是被判 reject「不可拼接」——把正常年际波动说成了拼接台阶。
    # 拼接器要抓的是「接缝处的**额外**台阶」，所以拿拼接点**前 BASELINE_WINDOW 期内、
    # 同一来源内部**的逐步变化中位数当基准：超额倍数 <= 1 就说明这个跳变在该序列的
    # 日常波动范围内，不该算拼接伪影。
    if i > 0:
        v_prev = spliced[i - 1]["value"]
        v_first = spliced[i]["value"]
        if v_prev is not None and v_first is not None and abs(v_prev) > _EPS:
            mag = abs(v_first - v_prev) / abs(v_prev)
            # 基准：拼接点之前、同一来源内部的逐步变化（绝对值）
            base_vals = [r["value"] for r in spliced[max(0, i - BASELINE_WINDOW):i]]
            base_vals.append(v_prev)
            steps = [abs(base_vals[k] - base_vals[k - 1])
                     for k in range(1, len(base_vals))
                     if base_vals[k] is not None and base_vals[k - 1] is not None]
            base = _median(steps)
            abs_jump = abs(v_first - v_prev)
            if base is not None and base > _EPS:
                excess: Optional[float] = abs_jump / base
                verdict = _grade(mag, LEVEL_JUMP_WARN, LEVEL_JUMP_REJECT)
                if verdict != "ok" and excess <= 1.0:
                    verdict = "ok"      # 跳变没超过该序列的日常步长 -> 不是拼接伪影
                evidence = (
                    f"{spliced[i - 1]['period']}={v_prev:g} ({spliced[i - 1]['source']}) -> "
                    f"{first_b_period}={v_first:g} ({spliced[i]['source']})，"
                    f"相对跳变 {mag:.4f}（阈值 warn>{LEVEL_JUMP_WARN} / reject>{LEVEL_JUMP_REJECT}）；"
                    f"该序列接缝前 {len(steps)} 步的中位步长={base:.6g}，"
                    f"超额倍数 {excess:.4f}（<=1 视为日常波动，判 ok）")
            elif base is not None:
                # 基线中位步长为 0：接缝前该序列**一步没动**（典型是水平量被两条完全平坦的
                # 序列拼起来）。这时"超额倍数"是无定义的（除以 0），**不能**把它当成
                # "跳变在正常范围内"而豁免 —— 恰恰相反，平坦序列里的 5% 台阶就是纯粹的拼接伪影。
                # 实测踩过：这里若豁免，本模块自检 [9] 的 5% 人工台阶会被判 ok。
                excess = None
                verdict = _grade(mag, LEVEL_JUMP_WARN, LEVEL_JUMP_REJECT)
                evidence = (
                    f"{spliced[i - 1]['period']}={v_prev:g} ({spliced[i - 1]['source']}) -> "
                    f"{first_b_period}={v_first:g} ({spliced[i]['source']})，"
                    f"相对跳变 {mag:.4f}（阈值 warn>{LEVEL_JUMP_WARN} / reject>{LEVEL_JUMP_REJECT}）；"
                    f"接缝前 {len(steps)} 步的中位步长=0（该段一步没动），"
                    f"超额倍数无定义 -> **不豁免**，按阈值判定")
            else:
                excess = None
                verdict = _grade(mag, LEVEL_JUMP_WARN, LEVEL_JUMP_REJECT)
                evidence = (
                    f"{spliced[i - 1]['period']}={v_prev:g} -> {first_b_period}={v_first:g}，"
                    f"相对跳变 {mag:.4f}（阈值 warn>{LEVEL_JUMP_WARN} / reject>{LEVEL_JUMP_REJECT}）；"
                    f"接缝前可用步数不足，无法算日常步长基准")
            breaks.append({
                "at": first_b_period,
                "type": "level_jump",
                "magnitude": mag,
                "abs_jump": abs_jump,
                "baseline_median_step": base,
                "excess_ratio": excess,
                "verdict": verdict,
                "evidence": evidence,
            })
        else:
            breaks.append({
                "at": first_b_period, "type": "level_jump", "magnitude": None,
                "verdict": "unknown",
                "evidence": f"紧邻期含缺失值（{spliced[i - 1]['period']} / {first_b_period}），无法计算",
            })

    # 3b) trend_break：前后各 WINDOW 期斜率
    vb = [v for v in (r["value"] for r in before) if v is not None]
    va = [v for v in (r["value"] for r in after) if v is not None]
    s_before = _slope([r["period"] for r in before], vb)
    s_after = _slope([r["period"] for r in after], va)
    if s_before is None or s_after is None:
        breaks.append({
            "at": first_b_period, "type": "trend_break", "magnitude": None,
            "verdict": "unknown",
            "evidence": (f"前后可用期数不足（before={len(vb)} / after={len(va)}，"
                         f"各需 >=2），未做趋势检测"),
        })
    elif abs(s_before) <= _EPS and abs(s_after) <= _EPS:
        # 两侧都平：斜率一致地等于 0，相对变化是 0/0 -> 明确记 0、判 ok。
        breaks.append({
            "at": first_b_period, "type": "trend_break", "magnitude": 0.0,
            "verdict": "ok",
            "evidence": (f"前 {len(vb)} 期斜率=0，后 {len(va)} 期斜率=0 —— 两侧都平，无趋势变化"),
        })
    elif abs(s_before) <= _EPS or abs(s_after) <= _EPS:
        # 一侧平、一侧不平：**相对变化无定义**（分母为 0）。
        # 实测踩过：旧代码用 max(|s_before|, _EPS) 当分母，0/eps 造出 magnitude=3e12
        # -> 假 reject「不可拼接」。相对量在这里失效，只能报**绝对**斜率变化，
        # 并如实记 unknown（既不判 ok 也不判 reject）。
        flat_side = "前" if abs(s_before) <= _EPS else "后"
        breaks.append({
            "at": first_b_period, "type": "trend_break", "magnitude": None,
            "abs_slope_change": abs(s_after - s_before),
            "verdict": "unknown",
            "evidence": (f"前 {len(vb)} 期斜率={s_before:.6g}，后 {len(va)} 期斜率={s_after:.6g}；"
                         f"{flat_side}段斜率为 0 -> 相对变化无定义（分母为 0），"
                         f"改报绝对斜率变化 {abs(s_after - s_before):.6g}，记 unknown"),
        })
    else:
        denom = max(abs(s_before), _EPS)
        mag = abs(s_after - s_before) / denom
        breaks.append({
            "at": first_b_period,
            "type": "trend_break",
            "magnitude": mag,
            "verdict": _grade(mag, TREND_WARN, TREND_REJECT),
            "evidence": (f"前 {len(vb)} 期斜率={s_before:.6g}，后 {len(va)} 期斜率={s_after:.6g}，"
                         f"相对变化 {mag:.4f}（阈值 warn>{TREND_WARN} / reject>{TREND_REJECT}）"),
        })

    # 3c) variance_shift：前后各 WINDOW 期标准差之比
    sd_before, sd_after = _stdev(vb), _stdev(va)
    if sd_before is None or sd_after is None:
        breaks.append({
            "at": first_b_period, "type": "variance_shift", "magnitude": None,
            "verdict": "unknown",
            "evidence": (f"前后可用期数不足（before={len(vb)} / after={len(va)}，"
                         f"各需 >=2），未做方差检测"),
        })
    else:
        lo, hi = min(sd_before, sd_after), max(sd_before, sd_after)
        if lo < _EPS:
            # 一侧完全无波动：比值无界，用 hi/eps 会爆表。记 unknown 更诚实。
            mag_v: Optional[float] = None
            g = "unknown"
            ev = (f"前 {len(vb)} 期 sd={sd_before:.6g}，后 {len(va)} 期 sd={sd_after:.6g}；"
                  f"一侧标准差为 0，比值无界，记 unknown")
        else:
            mag_v = hi / lo
            g = _grade(mag_v, VARIANCE_WARN, VARIANCE_REJECT)
            ev = (f"前 {len(vb)} 期 sd={sd_before:.6g}，后 {len(va)} 期 sd={sd_after:.6g}，"
                  f"比值 {mag_v:.4f}（阈值 warn>{VARIANCE_WARN} / reject>{VARIANCE_REJECT}）")
        breaks.append({
            "at": first_b_period, "type": "variance_shift", "magnitude": mag_v,
            "verdict": g, "evidence": ev,
        })

    return breaks



def rebase_series(series: Sequence[dict[str, Any]], target_mean: float,
           mode: str = "ratio") -> dict[str, Any]:
    """把一条序列的水平调整到 ``target_mean``（对齐两条序列的水平基准）。

    两种模式**不可混用**，选错的后果不是"精度差一点"，而是**引入二次误差**：

    ``ratio``（比值调整）: ``adjusted = value * (target_mean / source_mean)``
        适用**水平量**（GDP / 人口 / 指数 / 价格水平）。
        **不适用比率量**（同比增速 / 失业率 / 利率）：比率量本身就是比值，
        再乘一个比值等于对分母也做了一次缩放，结果是错的。

    ``difference``（差值调整）: ``adjusted = value + (target_mean - source_mean)``
        适用**比率量**。**不适用水平量**：10 亿元与 100 亿元的"差"没有跨序列意义，
        平移一个水平量会把整条曲线抬高/压低，而水平量该对齐的是**尺度**不是零点。

    sanity_check（防止把"拼错序列"当成"需要调整"）
        两种模式的守恒式是 ``mean(adjusted) == target_mean``，所以只要
        ``factor`` / ``offset`` 没被拦截就一定成立；sanity 拦的是**荒谬的调整幅度**：

        * ratio：``factor`` 必须落在 ``[RATIO_FACTOR_MIN, RATIO_FACTOR_MAX]``（默认 [0.5, 2.0]）。
          乘数翻倍或腰斩说明两条序列根本不是一个量级 —— 那是拼错了，不是要桥接。
        * difference：``|offset| < |target_mean|``。防止对已经接近 0 的量做荒谬平移
          （把 0.5 平移到 2.5 不是"对齐水平"，是造数）。
        * 另外 ``source_mean == 0`` 时 ratio 无定义 -> fail。

    **sanity_check == "fail" 时 adjusted 返回未调整的原序列**，并记 warning。
    调用方必须读 ``sanity_check`` 再决定用不用（``splice()`` 会据此置 ``applied=False``）。

    :param series: 一条序列的 ``[{period, value}, ...]``（``value`` 为 None 的期保留原值）。
    :param target_mean: 目标均值（拼接场景下 = 另一条序列在**重叠期**的均值）。
    :param mode: ``"ratio"``（默认）或 ``"difference"``。
    :returns: ``{"adjusted", "mode", "factor", "offset", "source_mean", "target_mean",
              "sanity_check", "sanity_note"}``。
              ``factor`` 仅 ratio 模式有值（difference 模式为 None），
              ``offset`` 仅 difference 模式有值（ratio 模式为 None）。
    :raises SpliceError: series 为空 / mode 未知 / 无任何有效数值。
    """
    if mode not in ("ratio", "difference"):
        raise SpliceError(f"未知 rebase 模式 {mode!r}；可选 ['ratio', 'difference']")
    if not isinstance(series, (list, tuple)) or not series:
        raise SpliceError(f"rebase: series 必须是非空列表（收到 {type(series).__name__}）")

    vals: list[float] = []
    for i, r in enumerate(series):
        if not isinstance(r, dict) or "period" not in r or "value" not in r:
            raise SpliceError(f"rebase: series[{i}] 必须是含 period / value 的 dict")
        v = _as_float(r.get("value"))
        if v is not None:
            vals.append(v)
    if not vals:
        raise SpliceError("rebase: series 里没有任何有效数值（全为 None）")

    tgt = float(target_mean)
    source_mean = sum(vals) / len(vals)

    # ---- 先算调整量，再判 sanity（判 fail 就不落地） -------------------- #
    factor: Optional[float] = None
    offset: Optional[float] = None
    if mode == "ratio":
        if abs(source_mean) <= _EPS:
            reason = f"ratio 模式要求源均值非 0，实际 {source_mean:g}（比值为无定义）"
        else:
            factor = tgt / source_mean
            reason = "" if RATIO_FACTOR_MIN <= factor <= RATIO_FACTOR_MAX else (
                f"factor={factor:.6g} 超出允许区间 [{RATIO_FACTOR_MIN}, {RATIO_FACTOR_MAX}] —— "
                f"两条序列量级不匹配，更像拼错了序列而不是需要水平调整")
    else:
        offset = tgt - source_mean
        reason = "" if abs(offset) < abs(tgt) else (
            f"|offset|={abs(offset):.6g} >= |target_mean|={abs(tgt):.6g} —— "
            f"对已接近 0 的量做这么大的平移属于造数")

    ok = (reason == "")
    adjusted: list[dict[str, Any]] = []
    for r in series:
        v = _as_float(r.get("value"))
        if v is None or not ok:
            out_v: Optional[float] = v
        elif mode == "ratio":
            out_v = v * factor                      # type: ignore[operator]
        else:
            out_v = v + offset                      # type: ignore[operator]
        adjusted.append({"period": str(r["period"]), "value": out_v})

    if not ok:
        import logging
        logging.getLogger(__name__).warning(
            "rebase(%s) sanity_check=fail，已返回**未调整**的原序列：%s", mode, reason)

    return {
        "adjusted": adjusted,
        "mode": mode,
        "factor": factor,
        "offset": offset,
        "source_mean": source_mean,
        "target_mean": tgt,
        "sanity_check": "pass" if ok else "fail",
        "sanity_note": reason,
    }


#: 公开别名。实现体叫 ``rebase_series`` 是为了**避开** ``splice()`` 的同名参数
#: ``rebase`` 的遮蔽 —— 在 ``splice()`` 内部 ``rebase`` 是字符串，直接 ``rebase(...)``
#: 会抛 ``TypeError: str object is not callable``（实测踩到）。
rebase = rebase_series


def splice(series_a: Sequence[dict[str, Any]],
           series_b: Sequence[dict[str, Any]],
           strategy: str = "later_wins",
           rebase: str = "none") -> dict[str, Any]:
    """把 series_a 与 series_b 拼成一条长序列，可选先做水平调整，并检查拼接点断点。

    :param series_a: 早源（较长历史的那条）。长表行列表，每行必须有
                     ``period`` / ``value`` / ``series_key``。
    :param series_b: 晚源（提供较新期间、且用来覆盖重叠期的那条）。要求同上。
    :param strategy: 重叠期取谁 —— ``"later_wins"``（默认，重叠期用 B）或
                     ``"earlier_wins"``（重叠期用 A）。**注意**：本参数只决定
                     **重叠期**取谁；没有重叠的期只能取唯一可用的那条，
                     策略对它无效。
    :param rebase: 水平调整模式 —— ``"none"``（默认，只检查不调整）、
                   ``"ratio"``、``"difference"``。见 :func:`rebase` 的模式说明。
                   **选哪个由数据的量纲决定，不由效果决定**：水平量用 ratio，
                   比率量用 difference。选错模式会引入二次误差。

                   调整的落点：**只调整 B 的非重叠期**。理由：重叠期本来就是
                   "拿 A 的值当基准"（``strategy="later_wins"`` 时取 A），
                   再对 B 的重叠期做调整属于**二次调整** —— B 在重叠期的值根本
                   不会被采进结果，调它只会在 ``overlap`` 对比里制造假一致。
                   非重叠期是 B 唯一真正"贡献数据"的地方，调整必须落在那里。
    :returns: 在原字段（``spliced`` / ``overlap`` / ``splice_point`` / ``breaks`` /
              ``summary``）之外，rebase != "none" 时额外给 ``rebase`` 字段：
              ``{mode, factor, offset, sanity_check, applied, source_mean, target_mean,
              before_breaks, after_breaks, verdict_effect, note}``。
    :raises SpliceError: 入参形状不对 / 策略未知 / rebase 模式未知 / 序列内部 period 重复。
    """
    if strategy not in STRATEGIES:
        raise SpliceError(f"未知策略 {strategy!r}；可选 {list(STRATEGIES)}")
    if rebase not in REBASE_MODES:
        raise SpliceError(f"未知 rebase 模式 {rebase!r}；可选 {list(REBASE_MODES)}")

    idx_a = _index(series_a, "series_a")
    idx_b = _index(series_b, "series_b")
    key_a = str(next(iter(idx_a.values()))["series_key"])
    key_b = str(next(iter(idx_b.values()))["series_key"])
    if key_a == key_b:
        raise SpliceError(
            f"series_a 与 series_b 的 series_key 相同（{key_a!r}）—— 拼接两条**同一个**序列没有意义，"
            f"请检查调用方是否把同一条序列传了两次")

    periods = _sorted_periods(idx_a, idx_b)
    common = [p for p in periods if p in idx_a and p in idx_b]

    def _build(idx_b_eff: dict[str, dict[str, Any]]) -> tuple[list[dict[str, Any]], Optional[str]]:
        """按 strategy 逐期定源，返回 (行列表, 拼接点)。"""
        rows: list[dict[str, Any]] = []
        first_b: Optional[str] = None
        for p in periods:
            in_a, in_b = p in idx_a, p in idx_b_eff
            take_b = (strategy == "later_wins") if (in_a and in_b) else in_b
            row = idx_b_eff[p] if take_b else idx_a[p]
            if take_b and first_b is None:
                first_b = p
            rows.append({
                "period": p,
                "value": _as_float(row.get("value")),
                "source": str(row.get("source") or "?"),
                "series_key": str(row["series_key"]),
                "splice_point": False,
            })
        if first_b is not None:
            for r in rows:
                if r["period"] == first_b:
                    r["splice_point"] = True
                    break
        return rows, first_b

    # ---- 1) 基线（未调整） ---------------------------------------------- #
    spliced, first_b_period = _build(idx_b)
    before_breaks = _detect_breaks(spliced, first_b_period)
    baseline_verdict = _verdict_of(before_breaks)

    # ---- 2) 重叠期一致性（始终基于**原始**值，调整不污染对照） ---------- #
    diffs: list[float] = []
    abs_diffs: list[float] = []
    saturated: list[str] = []
    for p in common:
        va, vb = _as_float(idx_a[p].get("value")), _as_float(idx_b[p].get("value"))
        if va is None or vb is None:
            continue
        ad = abs(va - vb)
        abs_diffs.append(ad)
        if max(abs(va), abs(vb)) <= RELATIVE_METRIC_FLOOR:
            # 两条序列在这一期都贴在 0 附近：相对量会饱和，改用绝对量（记 0 并记账）
            diffs.append(0.0)
            saturated.append(p)
        else:
            diffs.append(ad / max(abs(va), abs(vb)))
    if common:
        overlap: dict[str, Any] = {
            "periods": common,
            "n_overlap": len(common),
            "max_diff_rate": max(diffs) if diffs else None,
            "mean_diff_rate": (sum(diffs) / len(diffs)) if diffs else None,
            "max_abs_diff": max(abs_diffs) if abs_diffs else None,
            "mean_abs_diff": (sum(abs_diffs) / len(abs_diffs)) if abs_diffs else None,
            "check": "overlap_compared" if diffs else "overlap_no_numeric_value",
            "metric": ("相对量 = |v_a - v_b| / max(|v_a|, |v_b|)（恒在 [0,1]）；"
                       "绝对量 = |v_a - v_b|，量纲与输入一致。"
                       f"两条序列都 <= RELATIVE_METRIC_FLOOR({RELATIVE_METRIC_FLOOR}) 的期"
                       "相对量记 0 并列入 saturated_periods —— 那时只有绝对量有意义"),
            "relative_metric_floor": RELATIVE_METRIC_FLOOR,
            "saturated_periods": saturated,
        }
        if saturated:
            overlap["saturation_note"] = (
                f"{len(saturated)} 期两条序列都贴近 0（{saturated}），相对量已记 0 —— "
                f"**不要**把这些期读成'无分歧'，它们的真实差异要看绝对量："
                f"max_abs_diff={max(abs_diffs):.6g}、mean_abs_diff={sum(abs_diffs) / len(abs_diffs):.6g}")
    else:
        overlap = {
            "periods": [],
            "n_overlap": 0,
            "max_diff_rate": None,
            "mean_diff_rate": None,
            "max_abs_diff": None,
            "mean_abs_diff": None,
            "check": NO_OVERLAP_CHECK,
            "note": ("两条序列没有任何共同期，**无法做重叠校验**；本结果的 verdict 只反映"
                     "拼接点附近的断点检测，不代表重叠期一致"),
        }

    # ---- 3) 可选的水平调整 ---------------------------------------------- #
    rebase_info: Optional[dict[str, Any]] = None
    spliced_eff, first_b_eff = spliced, first_b_period
    after_breaks: list[dict[str, Any]] = list(before_breaks)

    if rebase != "none":
        if not common:
            rebase_info = {
                "mode": rebase, "factor": None, "offset": None,
                "sanity_check": "fail", "applied": False,
                "source_mean": None, "target_mean": None,
                "before_breaks": before_breaks, "after_breaks": before_breaks,
                "verdict_effect": "not_attempted",
                "note": ("没有重叠期 -> 无法确定 target_mean，rebase 未执行"
                         "（没有共同期就没有「把谁对齐到谁」的依据）"),
            }
        else:
            # ---- rebase 的校准窗口 = **B 的重叠期**，不是 B 的非重叠期 ----
            #
            # 这里有个反直觉但关键的点（第一版实现踩了）：
            # 调整的**落点**是 B 的非重叠期（重叠期结果取 A，调它等于二次调整），
            # 但校准的**窗口**必须是 B 的重叠期 —— 因为重叠期是"两条序列都有值、
            # 可以直接比"的唯一天然参照。
            #
            # 第一版拿 `b_nonover` 去调 `target_mean`，于是 source_mean 正好就是
            # 被调整的那一段的均值，调完自然精确等于 target_mean —— 但**接缝一点没变**：
            # 接缝两侧仍是「A 重叠期前一期」与「B 重叠期的值」，而后者根本没被改。
            # 结果就是四个断言全过、台阶依旧（实测 [9]/[10] 全 FAIL 才暴露）。
            #
            # 正确做法：用 B 的重叠期算出 factor/offset，把这个**同一个调整**应用到
            # B 的非重叠期上。
            b_common_rows = [{"period": p, "value": _as_float(idx_b[p].get("value"))}
                             for p in common]
            b_nonover = [{"period": p, "value": _as_float(r.get("value"))}
                         for p, r in idx_b.items() if p not in set(common)]
            a_over = [v for v in (_as_float(idx_a[p].get("value")) for p in common)
                      if v is not None]
            target_mean = (sum(a_over) / len(a_over)) if a_over else float("nan")

            # factor/offset 由「B 的重叠期」对标「A 的重叠期均值」得出，
            # 然后这个**同一个调整应用到 B 的全段**（含重叠期）。
            #
            # 为什么连重叠期也要调（第二版实现踩了）：拼接点那一期本身就在重叠区
            # （B 从 2019 起、A 到 2019 止，2019 是共同期）。如果只调非重叠期，
            # 接缝两侧就是「A 的 2018」与「B 的 2019（未调）」—— 台阶原封不动，
            # 结果四个断言全绿而 level_jump 依旧 warn（实测 [9]/[10] 就是这么挂的）。
            # 全段统一调整才能保证 B 内部自洽、接缝处与 A 同尺度。
            #
            # 重叠期仍需遵守 strategy（later_wins 取 A），所以调好的 B 重叠期只是
            # "备选值"（earlier_wins 时会用到）；结果里选谁由 `_build` 决定。
            #
            # ⚠️ `rebase_series()` 返回的 `adjusted` **已经是调整后的值** ——
            # 不要再乘一遍 factor（第三版实现踩了：导致 factor 被二次作用，
            # 105.5 变成 94.79 而不是 100，台阶反而变大）。
            rb = rebase_series(
                [{"period": p, "value": _as_float(idx_b[p].get("value"))} for p in periods
                 if p in idx_b], target_mean, mode=rebase)
            applied = (rb["sanity_check"] == "pass") and bool(b_nonover)
            adjusted_b: list[dict[str, Any]] = [
                {"period": r["period"], "value": r["value"]} for r in rb["adjusted"]]

            nonover_set = set(common)
            n_adjusted = sum(1 for r in adjusted_b if r["period"] not in nonover_set)

            adjusted_by_period = {r["period"]: r["value"] for r in adjusted_b}
            idx_b_eff: dict[str, dict[str, Any]] = {}
            for p, r in idx_b.items():
                if p in adjusted_by_period:
                    r2 = dict(r)
                    r2["value"] = adjusted_by_period[p]
                    idx_b_eff[p] = r2
                else:
                    idx_b_eff[p] = r

            spliced_eff, first_b_eff = _build(idx_b_eff)
            after_breaks = _detect_breaks(spliced_eff, first_b_eff)
            after_verdict = _verdict_of(after_breaks)

            if not applied:
                effect = "not_applied"
            elif baseline_verdict == VERDICT_OK:
                effect = "not_needed"          # 本来就 ok，调整没带来改善
            elif after_verdict == VERDICT_OK:
                effect = "improved"            # 调整把 warn/reject 变成了 ok
            elif after_verdict == baseline_verdict:
                effect = "unchanged"           # 调整没改变结论
            else:
                effect = "changed"

            # 说清"为什么没生效"。两种原因完全不同，混在一起会误导：
            #   no_non_overlap —— B 完全被 A 的时间跨度包住（B 的每一期都是共同期），
            #                     于是没有任何一期需要调整。**rebase 根本没被执行**，
            #                     不能读成"执行了但没用"。
            #   sanity_failed  —— 算出的 factor/offset 被 sanity 拦下，返回了原序列。
            if applied:
                why = ""
            elif not b_nonover:
                why = ("no_non_overlap: B 的每一期都落在重叠区（B 被 A 的时间跨度完全包住），"
                       "没有非重叠期可调 —— rebase 未执行，"
                       "**不要**把它读成「执行了但没能改善」")
            else:
                why = "sanity_failed: " + (rb.get("sanity_note") or "")

            rebase_info = {
                "mode": rebase,
                "factor": rb["factor"],
                "offset": rb["offset"],
                "sanity_check": rb["sanity_check"],
                "applied": applied,
                "source_mean": rb["source_mean"],
                "target_mean": rb["target_mean"],
                "before_breaks": before_breaks,
                "after_breaks": after_breaks,
                "verdict_effect": effect,
                "n_adjusted": len(rb["adjusted"]),
                "not_applied_reason": why,
                "note": (rb.get("sanity_note") or (
                    "已对 B 全段做水平调整（含重叠期，保证接缝与 A 同尺度）；"
                    "重叠期结果仍按 strategy 选值")),
            }

    n_from_b = sum(1 for r in spliced_eff if r["series_key"] == key_b)
    n_from_a = sum(1 for r in spliced_eff if r["series_key"] == key_a)
    final_verdict = _verdict_of(after_breaks)
    if rebase_info is not None and rebase_info["applied"] and final_verdict == VERDICT_OK:
        # 明确区分「调整前就 ok」和「调整后才 ok」—— 不能让"经过调整"看起来像"本来就一致"
        final_verdict = "可直接拼接（已调整）"
    return {
        "spliced": spliced_eff,
        "overlap": overlap,
        "splice_point": first_b_eff,
        "breaks": after_breaks,
        "rebase": rebase_info,
        "summary": {
            "n_total": len(spliced_eff),
            "n_from_a": n_from_a,
            "n_from_b": n_from_b,
            "verdict": final_verdict,
            "verdict_before_rebase": baseline_verdict,
            "strategy": strategy,
            "series_a": key_a,
            "series_b": key_b,
        },
    }



# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #

def _rows(key: str, pairs: Sequence[tuple[str, Optional[float]]],
          source: str = "test") -> list[dict[str, Any]]:
    """造长表行（自检用）。"""
    return [{"period": p, "value": v, "series_key": key, "source": source} for p, v in pairs]


def _flat(key: str, years: Sequence[int], value: float, source: str = "test") -> list[dict[str, Any]]:
    """造一条完全水平（无波动）的序列。"""
    return _rows(key, [(str(y), value) for y in years], source=source)


def _selftest() -> int:
    print("=" * 78)
    print("self-test: splicer（纯离线，四个必测场景 + 边界）")
    print("=" * 78)

    checks: list[tuple[str, bool]] = []

    # ---- 场景 1：完美重叠（同值）-> 可直接拼接 ---------------------------- #
    print("\n[1] 完美重叠（重叠期同值）")
    a = _rows("x|A", [("2018", 100.0), ("2019", 101.0), ("2020", 102.0)])
    b = _rows("x|B", [("2020", 102.0), ("2021", 103.0), ("2022", 104.0)])
    r1 = splice(a, b)
    print(f"  n_total={r1['summary']['n_total']} n_from_a={r1['summary']['n_from_a']} "
          f"n_from_b={r1['summary']['n_from_b']}")
    print(f"  重叠期={r1['overlap']['periods']} max_diff_rate={r1['overlap']['max_diff_rate']} "
          f"check={r1['overlap']['check']}")
    print(f"  splice_point={r1['splice_point']} verdict={r1['summary']['verdict']}")
    for br in r1["breaks"]:
        print(f"    break {br['type']:<15} magnitude={br['magnitude']} verdict={br['verdict']}")
    checks += [
        ("[1] verdict = 可直接拼接", r1["summary"]["verdict"] == VERDICT_OK),
        ("[1] max_diff_rate == 0.0", r1["overlap"]["max_diff_rate"] == 0.0),
        ("[1] n_total == 5（2018-2022 并集）", r1["summary"]["n_total"] == 5),
        ("[1] 重叠期 2020 取 B（later_wins）",
         next(r for r in r1["spliced"] if r["period"] == "2020")["series_key"] == "x|B"),
        ("[1] splice_point=2020 且该期 splice_point=True",
         r1["splice_point"] == "2020"
         and next(r for r in r1["spliced"] if r["period"] == "2020")["splice_point"] is True),
    ]

    # ---- 场景 2：5% 水平跳跃 -> 需桥接 ----------------------------------- #
    print("\n[2] 5.x% 水平跳跃（重叠期差 0，紧邻期跳 105/100）")
    # 前段平稳 100，B 段起 105 -> level_jump = 0.05 略超阈值
    a2 = _flat("x|A", [2018, 2019, 2020], 100.0)
    b2 = _flat("x|B", [2021, 2022, 2023], 105.5)
    r2 = splice(a2, b2)
    lj2 = next(br for br in r2["breaks"] if br["type"] == "level_jump")
    print(f"  splice_point={r2['splice_point']} verdict={r2['summary']['verdict']}")
    print(f"  level_jump magnitude={lj2['magnitude']:.4f} verdict={lj2['verdict']}")
    print(f"  {lj2['evidence']}")
    print(f"  breaks 条数={len(r2['breaks'])}")
    checks += [
        ("[2] verdict = 需桥接", r2["summary"]["verdict"] == VERDICT_BRIDGE),
        ("[2] level_jump verdict = warn", lj2["verdict"] == "warn"),
        ("[2] breaks 非空", len(r2["breaks"]) > 0),
        ("[2] 无重叠期 -> check = no_overlap_check",
         r2["overlap"]["check"] == NO_OVERLAP_CHECK),
    ]

    # ---- 场景 3：15% 水平跳跃 -> 不可拼接 -------------------------------- #
    print("\n[3] 15% 水平跳跃（100 -> 115）")
    a3 = _flat("x|A", [2018, 2019, 2020], 100.0)
    b3 = _flat("x|B", [2021, 2022, 2023], 115.0)
    r3 = splice(a3, b3)
    lj3 = next(br for br in r3["breaks"] if br["type"] == "level_jump")
    print(f"  verdict={r3['summary']['verdict']}")
    print(f"  level_jump magnitude={lj3['magnitude']:.4f} verdict={lj3['verdict']}")
    checks += [
        ("[3] verdict = 不可拼接", r3["summary"]["verdict"] == VERDICT_REJECT),
        ("[3] level_jump verdict = reject", lj3["verdict"] == "reject"),
    ]

    # ---- 场景 4：无重叠期（纯拼接）-> no_overlap_check -------------------- #
    print("\n[4] 无重叠期（2015-2019 + 2020-2024，无共同期）")
    a4 = _flat("x|A", [2015, 2016, 2017, 2018, 2019], 50.0)
    b4 = _flat("x|B", [2020, 2021, 2022, 2023, 2024], 50.0)
    r4 = splice(a4, b4)
    print(f"  n_overlap={r4['overlap']['n_overlap']} check={r4['overlap']['check']}")
    print(f"  max_diff_rate={r4['overlap']['max_diff_rate']} splice_point={r4['splice_point']}")
    print(f"  verdict={r4['summary']['verdict']}  n_total={r4['summary']['n_total']}")
    checks += [
        ("[4] overlap.check = no_overlap_check", r4["overlap"]["check"] == NO_OVERLAP_CHECK),
        ("[4] n_overlap == 0", r4["overlap"]["n_overlap"] == 0),
        ("[4] 差异率为 None（不是 0）",
         r4["overlap"]["max_diff_rate"] is None and r4["overlap"]["mean_diff_rate"] is None),
        ("[4] spliced 合并两段 = 10 期且无缺口", r4["summary"]["n_total"] == 10),
        ("[4] 同值平接 -> verdict = 可直接拼接", r4["summary"]["verdict"] == VERDICT_OK),
        ("[4] splice_point = 2020（第一条 B）", r4["splice_point"] == "2020"),
    ]

    # ---- 边界：earlier_wins ---------------------------------------------- #
    print("\n[5] 边界：earlier_wins（重叠期取 A）")
    r5 = splice(a, b, strategy="earlier_wins")
    print(f"  重叠期 2020 取 -> {next(r for r in r5['spliced'] if r['period'] == '2020')['series_key']}")
    print(f"  splice_point={r5['splice_point']}")
    checks += [
        ("[5] 重叠期取 A",
         next(r for r in r5["spliced"] if r["period"] == "2020")["series_key"] == "x|A"),
        ("[5] 2021 才是第一条 B（splice_point=2021）", r5["splice_point"] == "2021"),
    ]

    # ---- 边界：负值 / 缺失值 / 同键 -------------------------------- #
    print("\n[6] 边界：负值处理 + 缺失值不参与 + 同键拒绝")
    # 分母取 |v_prev|：-100 -> -105 的相对跳变是 0.05，不能因为负号变成 -0.05 或符号翻转
    neg = splice(_rows("x|A", [("2020", -100.0), ("2021", -100.0)]),
                 _rows("x|B", [("2021", -105.0), ("2022", -105.0)]))
    ljn = next(br for br in neg["breaks"] if br["type"] == "level_jump")
    print(f"  负值 -100 -> -105  level_jump magnitude={ljn['magnitude']:.4f}（用 |v_prev| 作分母）")
    miss = splice(
        _rows("x|A", [("2018", 1.0), ("2019", None), ("2020", 3.0), ("2021", 4.0)]),
        _rows("x|B", [("2021", 4.0), ("2022", 5.0)]))
    print(f"  含 None 时 n_total={miss['summary']['n_total']}（缺失值保留为行，不参与计算）")
    try:
        splice(a, a)
        same_key_ok = False
    except SpliceError as exc:
        same_key_ok = True
        print(f"  同键 -> SpliceError: {str(exc)[:70]}...")
    checks += [
        ("[6] 负值序列 level_jump 用 |v_prev| 作分母（不因负号翻转）",
         abs(ljn["magnitude"] - 0.05) < 1e-9),
        ("[6] 缺失值保留为行且不参与断点计算", miss["summary"]["n_total"] == 5),
        ("[6] 两条序列 series_key 相同 -> 抛 SpliceError", same_key_ok),
    ]

    # ---- 场景 7：真实数据的高波动不能被误判（本轮实测踩到的假阳性） -------- #
    print("\n[7] 高波动序列的正常年际变化（2014=2.06 -> 2015=1.40）不应判 reject")
    # 复刻真实序列的形态：同比在 -1% ~ 6% 之间大幅波动，接缝处 2.06 -> 1.40
    hi = _rows("x|A", [("2008", 5.97), ("2009", -0.72), ("2010", 3.18), ("2011", 5.56),
                       ("2012", 2.62), ("2013", 2.57), ("2014", 2.06)])
    hb = _rows("x|B", [("2015", 1.40), ("2016", 2.00), ("2017", 1.60), ("2018", 2.10)])
    r7 = splice(hi, hb)
    lj7 = next(br for br in r7["breaks"] if br["type"] == "level_jump")
    print(f"  level_jump magnitude={lj7['magnitude']:.4f}（>0.10 本该 reject）")
    print(f"  基准中位步长={lj7['baseline_median_step']:.6g}  超额倍数={lj7['excess_ratio']:.4f}")
    print(f"  -> verdict={lj7['verdict']}，整体 verdict={r7['summary']['verdict']}")
    print(f"  {lj7['evidence']}")
    # 对照组：同样 32% 的跳变，但序列本身平坦（步长≈0）-> 必须判出来
    flat_a = _flat("x|A", [2010, 2011, 2012, 2013, 2014], 10.0)
    flat_b = _flat("x|B", [2015, 2016, 2017], 13.2)
    r7b = splice(flat_a, flat_b)
    lj7b = next(br for br in r7b["breaks"] if br["type"] == "level_jump")
    print(f"  对照：平坦序列 10 -> 13.2（同样 +32%）-> magnitude={lj7b['magnitude']:.4f} "
          f"verdict={lj7b['verdict']}")
    checks += [
        ("[7] 高波动序列的接缝判 ok（超额倍数 <= 1）", lj7["verdict"] == "ok"),
        ("[7] 超额倍数字段存在且有值", lj7["excess_ratio"] is not None
         and lj7["excess_ratio"] <= 1.0),
        ("[7] 对照组（平坦序列同样的相对跳变）仍判 reject", lj7b["verdict"] == "reject"),
    ]

    # ---- 场景 8：重叠期贴零值的相对量饱和要记账 --------------------------- #
    print("\n[8] 贴零值的相对量饱和（序列量级 1~2，某一期 0.0 vs 0.051）")
    z_a = _rows("x|A", [("2023", 1.50), ("2024", 1.20), ("2025", 0.051)])
    z_b = _rows("x|B", [("2025", 0.0), ("2026", 1.0)])
    r8 = splice(z_a, z_b)
    ov8 = r8["overlap"]
    print(f"  重叠期={ov8['periods']}（只有 2025 是共同期，两条都贴近 0）")
    print(f"  max_diff_rate (相对) = {ov8['max_diff_rate']:.6f}  <- 记 0，不虚报 100%")
    print(f"  max_abs_diff  (绝对) = {ov8['max_abs_diff']:.6f} pp  <- 真实差异在这里")
    print(f"  saturated_periods={ov8['saturated_periods']}")
    print(f"  {ov8.get('saturation_note', '')}")
    # 对照组：量级正常的期，相对量照常算（不能被 floor 吞掉）
    n_a = _rows("x|A", [("2024", 2.06)])
    n_b = _rows("x|B", [("2024", 1.40)])
    ov8b = splice(n_a, n_b)["overlap"]
    print(f"  对照（2.06 vs 1.40，量级正常）-> max_diff_rate={ov8b['max_diff_rate']:.4f} "
          f"saturated={ov8b['saturated_periods']}")
    checks += [
        ("[8] 贴零期的相对量记 0（不虚报饱和的 1.0）",
         ov8["max_diff_rate"] is not None and ov8["max_diff_rate"] == 0.0),
        ("[8] 绝对量被并列报出且 = 0.051", ov8["max_abs_diff"] is not None
         and abs(ov8["max_abs_diff"] - 0.051) < 1e-9),
        ("[8] 贴零期被记账 + 有解释", ov8["saturated_periods"] == ["2025"]
         and "贴近 0" in str(ov8.get("saturation_note", ""))),
        ("[8] 对照：量级正常的期相对量照常算（=0.3204，未被 floor 吞掉）",
         ov8b["max_diff_rate"] is not None and abs(ov8b["max_diff_rate"] - 0.3204) < 1e-3
         and ov8b["saturated_periods"] == []),
        ("[8] metric 字段说明了两个口径",
         "绝对量" in str(ov8.get("metric", "")) and "saturated_periods" in str(ov8.get("metric", ""))),
    ]

    # ---- 场景 9：rebase="ratio" 修好水平量的人工台阶 --------------------- #
    print("\n[9] rebase='ratio'：水平量的人工台阶（全段 A=100 / B=105.5，接缝跳 5.5%）")
    # 夹具设计（刻意让**重叠期均值可手算**）：
    #   A 全段 = 100；B 的**重叠期 2019** 与**非重叠期 2020/2021** 都是 105.5。
    #   于是 target_mean = mean(A 的重叠期) = 100，source_mean = mean(B 的非重叠期) = 105.5，
    #   factor = 100/105.5 —— 三个数都能手算核对。
    #   接缝跳变 = |105.5-100|/100 = 5.5% -> 落在 warn 带（>5% 且 <=10%）。
    #   两侧都平（斜率 0）-> trend_break 判 ok，variance_shift 记 unknown，都不影响 verdict。
    a9 = _flat("x|A", [2017, 2018, 2019], 100.0)
    b9 = _flat("x|B", [2019, 2020, 2021], 105.5)
    r9_none = splice(a9, b9)
    r9 = splice(a9, b9, rebase="ratio")
    rb9 = r9["rebase"]
    print(f"  none   : verdict={r9_none['summary']['verdict']}")
    print(f"  ratio  : before={r9['summary']['verdict_before_rebase']} -> after={r9['summary']['verdict']}")
    print(f"           factor={rb9['factor']:.6f} sanity={rb9['sanity_check']} "
          f"applied={rb9['applied']} n_adjusted={rb9['n_adjusted']}")
    print(f"           source_mean={rb9['source_mean']:.4f} target_mean={rb9['target_mean']:.4f} "
          f"effect={rb9['verdict_effect']}")
    lj9_before = next(b for b in rb9["before_breaks"] if b["type"] == "level_jump")
    lj9_after = next(b for b in rb9["after_breaks"] if b["type"] == "level_jump")
    print(f"           level_jump: {lj9_before['verdict']}({lj9_before['magnitude']:.4f}) "
          f"-> {lj9_after['verdict']}({lj9_after['magnitude']:.4f})")
    bvals9 = [(r["period"], r["value"]) for r in r9["spliced"] if r["series_key"] == "x|B"]
    print(f"           B 段调整后: {[(p, round(v, 4)) for p, v in bvals9]}")
    checks += [
        ("[9] rebase 前 level_jump 是 warn", lj9_before["verdict"] == "warn"),
        ("[9] rebase 后 level_jump 变 ok", lj9_after["verdict"] == "ok"),
        ("[9] verdict = 可直接拼接（已调整）—— 明确标注经过调整",
         r9["summary"]["verdict"] == "可直接拼接（已调整）"),
        ("[9] verdict_before_rebase 保留原判（=需桥接）",
         r9["summary"]["verdict_before_rebase"] == VERDICT_BRIDGE),
        ("[9] factor = 100/105.5", rb9["factor"] is not None
         and abs(rb9["factor"] - 100.0 / 105.5) < 1e-9),
        ("[9] n_adjusted = 3（B 全段：重叠期 2019 + 非重叠 2020/2021）",
         rb9["n_adjusted"] == 3),
        ("[9] B 非重叠期 105.5 -> 100（调完与 A 齐平）", all(
            abs(v - 100.0) < 1e-9 for p, v in bvals9 if p != "2019")),
        ("[9] effect=improved", rb9["verdict_effect"] == "improved"),
    ]

    # ---- 场景 10：rebase="difference" 修好比率量的人工台阶 -------------- #
    print("\n[10] rebase='difference'：比率量的人工台阶（全段 A=2.0 / B=2.13）")
    # 同上设计，但换成比率量：全段 A=2.0、B=2.13。接缝跳 6.5% -> warn
    # target_mean = mean(A 重叠期 2019) = 2.0，offset = 2.0 - 2.13 = -0.13
    a10 = _flat("x|A", [2017, 2018, 2019], 2.0)
    b10 = _flat("x|B", [2019, 2020, 2021], 2.13)
    r10 = splice(a10, b10, rebase="difference")
    rb10 = r10["rebase"]
    print(f"  difference: before={r10['summary']['verdict_before_rebase']} -> after={r10['summary']['verdict']}")
    print(f"           offset={rb10['offset']:.6f} sanity={rb10['sanity_check']} "
          f"applied={rb10['applied']} n_adjusted={rb10['n_adjusted']}")
    print(f"           source_mean={rb10['source_mean']:.4f} target_mean={rb10['target_mean']:.4f}")
    lj10_before = next(b for b in rb10["before_breaks"] if b["type"] == "level_jump")
    lj10_after = next(b for b in rb10["after_breaks"] if b["type"] == "level_jump")
    print(f"           level_jump: {lj10_before['verdict']}({lj10_before['magnitude']:.4f}) "
          f"-> {lj10_after['verdict']}({lj10_after['magnitude']:.4f})")
    bvals10 = [(r["period"], r["value"]) for r in r10["spliced"] if r["series_key"] == "x|B"]
    print(f"           B 段调整后: {[(p, round(v, 4)) for p, v in bvals10]}")
    checks += [
        ("[10] rebase 前 level_jump 是 warn", lj10_before["verdict"] == "warn"),
        ("[10] rebase 后 level_jump 变 ok", lj10_after["verdict"] == "ok"),
        ("[10] verdict = 可直接拼接（已调整）",
         r10["summary"]["verdict"] == "可直接拼接（已调整）"),
        ("[10] offset = 2.0 - 2.13 = -0.13", rb10["offset"] is not None
         and abs(rb10["offset"] + 0.13) < 1e-9),
        ("[10] B 非重叠期 2.13 -> 2.0", all(
            abs(v - 2.0) < 1e-9 for p, v in bvals10 if p != "2019")),
        ("[10] factor 为 None（difference 模式不产出乘数）", rb10["factor"] is None),
    ]

    # ---- 场景 11：sanity_check 拦截 ratio（factor=10） ------------------- #
    print("\n[11] sanity_check 拦截 ratio：target_mean=100, source_mean=10 -> factor=10")
    rb11 = rebase_series([{"period": "2020", "value": 10.0}, {"period": "2021", "value": 10.0}],
                  target_mean=100.0, mode="ratio")
    print(f"  factor={rb11['factor']} sanity={rb11['sanity_check']}")
    print(f"  source_mean={rb11['source_mean']} target_mean={rb11['target_mean']}")
    print(f"  返回的 adjusted 值: {[r['value'] for r in rb11['adjusted']]}（应为未调整的 10.0）")
    print(f"  note: {rb11['sanity_note']}")
    r11 = splice(_flat("x|A", [2017, 2018, 2019], 100.0),
                 _flat("x|B", [2020, 2021, 2022], 10.0), rebase="ratio")
    print(f"  splice 侧 applied={r11['rebase']['applied']} "
          f"effect={r11['rebase']['verdict_effect']} verdict={r11['summary']['verdict']}")
    checks += [
        ("[11] sanity_check = fail", rb11["sanity_check"] == "fail"),
        ("[11] factor = 10.0（如实报出，不掩盖）", abs(rb11["factor"] - 10.0) < 1e-9),
        ("[11] adjusted 返回未调整原值（10.0）",
         all(abs(r["value"] - 10.0) < 1e-9 for r in rb11["adjusted"])),
        ("[11] sanity_note 指出量级不匹配", "超出允许区间" in rb11["sanity_note"]),
        ("[11] splice 侧 applied=False 且不谎称已调整",
         r11["rebase"]["applied"] is False
         and r11["summary"]["verdict"] != "可直接拼接（已调整）"),
    ]

    # ---- 场景 12：sanity_check 拦截 difference（target=0.5, offset=2.0） - #
    print("\n[12] sanity_check 拦截 difference：target_mean=0.5, source=-1.5 -> offset=2.0")
    rb12 = rebase_series([{"period": "2020", "value": -1.5}, {"period": "2021", "value": -1.5}],
                  target_mean=0.5, mode="difference")
    print(f"  offset={rb12['offset']} |target_mean|={abs(rb12['target_mean'])} "
          f"sanity={rb12['sanity_check']}")
    print(f"  返回的 adjusted 值: {[r['value'] for r in rb12['adjusted']]}（应为未调整的 -1.5）")
    print(f"  note: {rb12['sanity_note']}")
    checks += [
        ("[12] sanity_check = fail", rb12["sanity_check"] == "fail"),
        ("[12] offset = 2.0 且 |offset| >= |target_mean|", abs(rb12["offset"] - 2.0) < 1e-9
         and abs(rb12["offset"]) >= abs(rb12["target_mean"])),
        ("[12] adjusted 返回未调整原值（-1.5）",
         all(abs(r["value"] + 1.5) < 1e-9 for r in rb12["adjusted"])),
        ("[12] sanity_note 指出属于造数", "造数" in rb12["sanity_note"]),
    ]

    # ---- 场景 13：rebase ≠ none 但没有重叠期 -> 不执行 ------------------- #
    print("\n[13] rebase 无重叠期：无法确定 target_mean -> not_attempted")
    r13 = splice(_flat("x|A", [2015, 2016, 2017], 100.0),
                 _flat("x|B", [2020, 2021, 2022], 105.0), rebase="ratio")
    print(f"  applied={r13['rebase']['applied']} effect={r13['rebase']['verdict_effect']}")
    print(f"  note={r13['rebase']['note']}")
    checks += [
        ("[13] 无重叠期时 applied=False", r13["rebase"]["applied"] is False),
        ("[13] effect=not_attempted", r13["rebase"]["verdict_effect"] == "not_attempted"),
    ]

    # ---- 场景 14：rebase="none" 时 rebase 字段为 None 且行为不变 --------- #
    print("\n[14] rebase='none'（默认）：rebase 字段为 None，行为与旧版一致")
    # 用 100 -> 107（7% 台阶）：明确落在 warn 带内。
    # 别用 105 —— |105-100|/100 浮点上是 0.04999999999999999，压不到 warn；
    # 也别用 102（2%），它确实该判 ok。阈值边界语义见 _grade 的 docstring。
    r14 = splice(_flat("x|A", [2017, 2018, 2019], 100.0),
                 _flat("x|B", [2020, 2021, 2022], 107.0))
    print(f"  rebase={r14['rebase']} verdict={r14['summary']['verdict']} "
          f"verdict_before={r14['summary']['verdict_before_rebase']}")
    for br in r14["breaks"]:
        print(f"    {br['type']:<15} {br['verdict']:<8} mag={br['magnitude']}")
    checks += [
        ("[14] rebase 字段为 None", r14["rebase"] is None),
        ("[14] verdict 沿用现有判定（需桥接，不含'已调整'）",
         r14["summary"]["verdict"] == VERDICT_BRIDGE),
        ("[14] verdict_before_rebase == verdict",
         r14["summary"]["verdict_before_rebase"] == r14["summary"]["verdict"]),
    ]

    print()
    for label, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")

    bad = [label for label, ok in checks if not ok]
    print()
    print("=" * 78)
    print("self-test 完成 ✔" if not bad else f"self-test 失败: {bad}")
    print("=" * 78)
    return 0 if not bad else 1


def _main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    p = argparse.ArgumentParser(prog="splicer", description="序列拼接器（拼接点 + 断点检测）")
    p.add_argument("--test", action="store_true", help="跑自检（纯离线）")
    a = p.parse_args(argv)
    if a.test:
        try:
            return _selftest()
        except SpliceError as exc:
            print(f"[FAIL] {exc}", file=sys.stderr)
            return 2
    p.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
