#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""arbiter.py —— 口径判定仲裁（方向 A 第二轮）。

它解决什么问题
--------------
source_profiler 能回答「两条序列的口径像不像」（静态画像），
tools/compare-*.py 能回答「两条序列的数差多少」（实测差异），
但在本模块之前，这两件事没有汇合：画像说「口径一致」，实测却差 1.5 pp，没人把这句话说完整。

arbiter 把两者配成一条记录，给出四值判定：

    同口径   —— 口径一致，可直接拼接
    可桥接   —— 统计方法相同但覆盖/范围不同，需先桥接再拼
    不可拼接 —— 口径不同（典型：行政记录 vs 抽样调查），差异是口径造成的，不是数据错误
    人工复核 —— 知识库里有关键字段是 unknown，判定权交给人工（优先级最高）

判定优先级（本轮决策）
----------------------
1. unknown_fields 命中高影响字段（statistical_method / coverage / population_scope）
   -> 人工复核。unknown 表示「没有依据」，既不能算一致，也不能算冲突。
2. unknown_fields 只命中低影响字段（start_year / revision_policy 等）
   -> 保留原判定，把这些字段记进 knowledge_gaps（仅记录，不改变结论）。
3. 其余 -> 由 compare_profiles 的 comparable 映射：
   high->同口径 / medium->可桥接 / low|incompatible->不可拼接。

数据来源
--------
实测差异不重跑取数，直接读 data/validated/cross_check/*.json 的产物。已知四种形状：

    gdp_nbs_vs_worldbank.json  result.summary.max_diff_rate      (percent)
    gdp_3way_*.json            nbs_vs_imf / wb_vs_imf.max_diff_rate (percent)
    gdp_real_*.json            max_abs_diff_pp                    (pp)
    unemployment_3way_*.json   rows[].registered_vs_imf_pp / surveyed_vs_imf_pp (pp)

识别不出来的形状跳过并记 warning，不抛异常、不中断整轮。

两条容易误读的约定
------------------
* verdict 来自口径画像（comparable），attribution / recommended_action 来自实测归因
  （explain_divergence）。两者是独立的两句话，本模块不强行让它们一致；
  当 verdict=人工复核 时，explanation 会显式声明「归因仅供参考」。
* diff_type=percent 时 measured.diff_pp 里装的是**比率**（0.0114 就是 1.14%），
  只有 diff_type=pp 才真的是百分点。字段名沿用调用方约定，语义看 diff_type。

用法
----
    ./.venv/Scripts/python.exe -m econ_core.arbiter --test    # 自检（7 个场景，纯离线）
    ./.venv/Scripts/python.exe -m econ_core.arbiter           # 打印全部判定
    ./.venv/Scripts/python.exe -m econ_core.arbiter --write   # 落盘报告
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from econ_core import http_client, source_profiler  # type: ignore[no-redef]
else:
    from . import http_client, source_profiler

__all__ = [
    "ArbiterError",
    "arbitrate_pair",
    "arbitrate_all",
    "write_arbiter_report",
    "last_warnings",
    "CROSS_CHECK_DIR",
    "DEFAULT_OUT_DIR",
    "VERDICT_BY_COMPARABLE",
    "VERDICT_MANUAL",
    "HIGH_IMPACT_FIELDS",
]

PROJECT_ROOT: Path = http_client.PROJECT_ROOT
CROSS_CHECK_DIR: Path = PROJECT_ROOT / "data" / "validated" / "cross_check"
DEFAULT_OUT_DIR: Path = PROJECT_ROOT / "data" / "validated" / "arbiter"

#: 四值判定（同口径 / 可桥接 / 不可拼接 / 人工复核）
VERDICT_SAME = "同口径"
VERDICT_BRIDGE = "可桥接"
VERDICT_SEPARATE = "不可拼接"
VERDICT_MANUAL = "人工复核"

VERDICT_BY_COMPARABLE: dict[str, str] = {
    "high": VERDICT_SAME,
    "medium": VERDICT_BRIDGE,
    "low": VERDICT_SEPARATE,
    "incompatible": VERDICT_SEPARATE,
}

#: 高影响字段：这些字段 unknown 时判定权交给人工。
#: 取值与 source_profiler._COMPARE_FIELDS 里 impact == "high" 的集合一致，
#: 自检会断言两者不漂移（一处改了另一处没改就会被门禁抓到）。
HIGH_IMPACT_FIELDS: frozenset[str] = frozenset(
    {"statistical_method", "coverage", "population_scope"})

#: cross_check 产物里只有数据源代码，没有 series_key；这里做一次显式映射。
GDP_NBS = "nbs|gdp|cny_100m"
GDP_WB = "worldbank|NY.GDP.MKTP.CN"
GDP_IMF = "imf|NGDPD"
UNEMP_REG = "nbs|registered_unemployment"
UNEMP_SUR = "nbs|surveyed_unemployment"
UNEMP_IMF = "imf|LUR"
#: 下面两条对比涉及 GDP 指数 / 实际增速，这两个序列尚未进知识库：
#: 期望行为是「跳过 + warning」，本模块故意不为它们造知识库条目。
GDP_INDEX = "nbs|gdp|index_prev_year_100"
IMF_REAL = "imf|NGDP_RPCH"

#: Windows 非法文件名字符（chr(92) 是反斜杠，避免源码里出现字面反斜杠）
_ILLEGAL_CHARS = '/:*?"<>|' + chr(92)

_LAST_WARNINGS: list[str] = []


class ArbiterError(RuntimeError):
    """仲裁层可预期的失败（cross_check 目录缺失、参数非法等）。"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rel(p: Path) -> str:
    """项目内相对路径，统一正斜杠（跨平台、可直接复制）。"""
    try:
        rel = Path(p).resolve().relative_to(PROJECT_ROOT)
    except ValueError:
        return str(p)
    return rel.as_posix()


def _safe_stem(text: str) -> str:
    """pair_key -> 文件名。保留非 ASCII，只替换路径非法字符。"""
    out = text.replace(" × ", "__vs__")
    out = "".join(("_" if ch in _ILLEGAL_CHARS else ch) for ch in out)
    out = "".join(ch for ch in out if ord(ch) >= 32)
    return out.strip().replace(" ", "_")


def last_warnings() -> list[str]:
    """最近一次 arbitrate_all 的 warnings（跳过/降级的原因）。"""
    return list(_LAST_WARNINGS)


def arbitrate_pair(series_a: str, series_b: str, diff_pp: float, diff_source: str,
                   diff_type: str = "pp") -> dict[str, Any]:
    """对一对序列做口径判定：静态画像 × 实测差异。

    :param series_a: 序列键 A（可用别名键，内部会解析到规范键）。
    :param series_b: 序列键 B。
    :param diff_pp: 实测差异的**绝对值**（diff_type=pp 时单位是百分点）。
    :param diff_source: 差异来自哪个文件（原样记进 measured.source）。
    :param diff_type: "pp" 或 "percent"（后者表示这是个比率，如 0.0114 = 1.14%）。
    :returns: 判定记录（见模块 docstring 的字段说明）。
    :raises KeyError: 序列键不在知识库。
    :raises ArbiterError: diff_type 非法。
    """
    if diff_type not in ("pp", "percent"):
        raise ArbiterError(f"diff_type 只能是 pp/percent，实际 {diff_type!r}")
    measured = abs(float(diff_pp))

    cmp_ = source_profiler.compare_profiles(series_a, series_b)
    comparable = str(cmp_.get("comparable") or "unknown")
    unknown_fields = [str(f) for f in (cmp_.get("unknown_fields") or [])]
    high_unknowns = [f for f in unknown_fields if f in HIGH_IMPACT_FIELDS]
    knowledge_gaps = [f for f in unknown_fields if f not in HIGH_IMPACT_FIELDS]

    if high_unknowns:
        verdict = VERDICT_MANUAL
    else:
        # 认不出的 comparable 值一律保守降级为人工复核，不猜
        verdict = VERDICT_BY_COMPARABLE.get(comparable, VERDICT_MANUAL)

    div = source_profiler.explain_divergence(series_a, series_b, measured)
    explanation = str(div.get("explanation") or "")
    if verdict == VERDICT_MANUAL:
        explanation = ("知识库缺口（" + "、".join(high_unknowns)
                       + "）——判定降级为人工复核；下面的归因仅供参考：" + explanation)

    key_a = str(cmp_.get("series_key_a") or series_a)
    key_b = str(cmp_.get("series_key_b") or series_b)
    return {
        "pair_key": key_a + " × " + key_b,
        "series_a": key_a,
        "series_b": key_b,
        "verdict": verdict,
        "comparable": comparable,
        "decision_rule": str(cmp_.get("decision_rule") or ""),
        "differences": list(cmp_.get("differences") or []),
        "unknown_fields": unknown_fields,
        "high_impact_unknowns": high_unknowns,
        "knowledge_gaps": knowledge_gaps,
        "measured": {
            "diff_pp": measured,
            "diff_type": diff_type,
            "source": str(diff_source),
        },
        "attribution": str(div.get("attribution") or "unknown"),
        "explanation": explanation,
        "recommended_action": str(div.get("recommended_action") or "keep_separate"),
        "confidence": str(div.get("confidence") or "low"),
        "arbitrated_at": _utc_now(),
    }


# --------------------------------------------------------------------------- #
# cross_check 产物 -> 对比对（形状适配）
# --------------------------------------------------------------------------- #

def _rows_max_abs(rows: Any, field: str) -> Optional[float]:
    """rows[].<field> 里所有数值的绝对值最大值（无则 None）。"""
    vals: list[float] = []
    if isinstance(rows, list):
        for r in rows:
            if isinstance(r, dict) and isinstance(r.get(field), (int, float)):
                vals.append(abs(float(r[field])))
    return max(vals) if vals else None


def _adapt(path: Path, obj: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """把一个 cross_check JSON 适配成对比对列表；返回 (specs, warnings)。

    spec = {a, b, diff, diff_type, label}。识别不出来的形状不猜，记 warning。
    """
    specs: list[dict[str, Any]] = []
    warnings: list[str] = []
    src = _rel(path)
    rows = obj.get("rows")

    def add(a: str, b: str, diff: Any, diff_type: str, label: str) -> None:
        if isinstance(diff, (int, float)):
            specs.append({"a": a, "b": b, "diff": float(diff),
                          "diff_type": diff_type, "label": label})
        else:
            warnings.append(f"{src}: {label} 的差异值不是数字（{diff!r}），已跳过")

    # 形状 1: gdp_nbs_vs_worldbank —— result.summary.max_diff_rate
    result = obj.get("result")
    summary = result.get("summary") if isinstance(result, dict) else None
    if isinstance(summary, dict) and "max_diff_rate" in summary:
        add(GDP_NBS, GDP_WB, summary.get("max_diff_rate"), "percent", "NBS GDP vs World Bank GDP")

    # 形状 2: gdp_3way —— nbs_vs_imf / wb_vs_imf 两条 max_diff_rate
    for block_key, series_a in (("nbs_vs_imf", GDP_NBS), ("wb_vs_imf", GDP_WB)):
        block = obj.get(block_key)
        if isinstance(block, dict) and "max_diff_rate" in block:
            add(series_a, GDP_IMF, block.get("max_diff_rate"), "percent", block_key)

    # 形状 3: unemployment_3way —— 顶层 *_vs_imf_pp，没有就退到 rows[] 列的最大绝对值
    for field, series_a in (("registered_vs_imf_pp", UNEMP_REG),
                            ("surveyed_vs_imf_pp", UNEMP_SUR)):
        if isinstance(obj.get(field), (int, float)):
            add(series_a, UNEMP_IMF, obj[field], "pp", field)
        else:
            v = _rows_max_abs(rows, field)
            if v is not None:
                add(series_a, UNEMP_IMF, v, "pp", field + " (rows max abs)")

    # 形状 4: gdp_real —— max_abs_diff_pp
    if isinstance(obj.get("max_abs_diff_pp"), (int, float)):
        add(GDP_INDEX, IMF_REAL, obj["max_abs_diff_pp"], "pp",
            "NBS GDP index vs IMF NGDP_RPCH")

    # 兜底：顶层任何 *_vs_* 数字都当一对（series 名是占位，交给知识库判不在册）
    if not specs:
        for key, value in obj.items():
            if "_vs_" in key and isinstance(value, (int, float)):
                a, _, b = key.partition("_vs_")
                warnings.append(f"{src}: 兜底识别出未知形状的对比对 {key}")
                add("unknown|" + a, "unknown|" + b, value,
                    "percent" if "rate" in key else "pp", key)

    if not specs and not warnings:
        warnings.append(f"{src}: 未识别的 cross_check 形状（顶层键 {sorted(obj)[:8]}），已跳过")
    return specs, warnings


def arbitrate_all(cross_check_dir: Optional[Path] = None) -> list[dict[str, Any]]:
    """扫描 cross_check/*.json，对自动发现的每个对比对调用 arbitrate_pair。

    单对的失败（序列不在知识库 / 文件坏了 / 形状不认识）只记 warning 并跳过，
    不影响其它对——这批数据是历史产物，形状不完全统一。
    """
    global _LAST_WARNINGS
    d = Path(cross_check_dir) if cross_check_dir else CROSS_CHECK_DIR
    if not d.is_dir():
        raise ArbiterError(f"cross_check 目录不存在: {d}")

    pairs: list[dict[str, Any]] = []
    warnings: list[str] = []
    for path in sorted(d.glob("*.json")):
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            warnings.append(f"{_rel(path)}: 读取/解析失败，已跳过（{exc}）")
            continue
        if not isinstance(obj, dict):
            warnings.append(f"{_rel(path)}: 顶层不是 JSON 对象，已跳过")
            continue
        specs, warns = _adapt(path, obj)
        warnings.extend(warns)
        for spec in specs:
            try:
                rec = arbitrate_pair(spec["a"], spec["b"], spec["diff"],
                                    _rel(path), spec["diff_type"])
            except KeyError as exc:
                warnings.append(f"{_rel(path)}: {spec['a']} × {spec['b']} "
                                f"不在知识库，已跳过（{str(exc)[:70]}）")
                continue
            rec["label"] = spec["label"]
            pairs.append(rec)

    _LAST_WARNINGS = warnings
    for w in warnings:
        print(f"[arbiter][warning] {w}", file=sys.stderr)
    return pairs


def write_arbiter_report(out_dir: Optional[Path] = None) -> Path:
    """跑 arbitrate_all 并落盘：每个对比对一个 JSON + 一个 _index.json。

    :returns: 落盘目录。
    """
    out = Path(out_dir) if out_dir else DEFAULT_OUT_DIR
    pairs = arbitrate_all()
    out.mkdir(parents=True, exist_ok=True)

    index: dict[str, Any] = {"generated_at": _utc_now(), "n_pairs": len(pairs),
                             "by_verdict": {}, "warnings": last_warnings(),
                             "pairs": []}
    for rec in pairs:
        verdict = rec["verdict"]
        index["by_verdict"][verdict] = index["by_verdict"].get(verdict, 0) + 1
        fname = _safe_stem(rec["pair_key"]) + ".json"
        fp = out / fname
        fp.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
        index["pairs"].append({
            "pair_key": rec["pair_key"],
            "verdict": verdict,
            "comparable": rec["comparable"],
            "diff_pp": rec["measured"]["diff_pp"],
            "diff_type": rec["measured"]["diff_type"],
            "source": rec["measured"]["source"],
            "file": fname,
        })
    (out / "_index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #

FAKE_SOURCE = "data/validated/cross_check/_selftest_fake.json"


def _show(title: str, obj: Any) -> None:
    print()
    print("-" * 92)
    print(title)
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _brief(rec: dict[str, Any]) -> dict[str, Any]:
    """自检输出只留判定相关的字段，避免刷屏。"""
    return {
        "pair_key": rec["pair_key"],
        "verdict": rec["verdict"],
        "comparable": rec["comparable"],
        "attribution": rec["attribution"],
        "recommended_action": rec["recommended_action"],
        "confidence": rec["confidence"],
        "unknown_fields": rec["unknown_fields"],
        "high_impact_unknowns": rec["high_impact_unknowns"],
        "knowledge_gaps": rec["knowledge_gaps"],
        "measured": rec["measured"],
    }


def _with_fake_compare(base: dict[str, Any], unknown_fields: Sequence[str],
                       fn: Callable[[], Any]) -> Any:
    """临时把 compare_profiles 换成「返回指定 unknown_fields」的版本。

    只在自检里用：真实知识库不该为了测试塞假条目。
    """
    real = source_profiler.compare_profiles

    def fake(key_a: str, key_b: str, kb: Any = None) -> dict[str, Any]:
        out = dict(base)
        out["unknown_fields"] = [str(f) for f in unknown_fields]
        return out

    source_profiler.compare_profiles = fake  # type: ignore[assignment]
    try:
        return fn()
    finally:
        source_profiler.compare_profiles = real  # type: ignore[assignment]


def _selftest() -> int:
    failures: list[str] = []
    print("=" * 92)
    print("self-test: arbiter（口径判定：画像 × 实测差异 -> 四值判定）")
    print("=" * 92)

    # 0) 漂移守卫：高影响字段集合必须与 source_profiler 的 high 集合一致
    declared = {f for f, impact in source_profiler._COMPARE_FIELDS if impact == "high"}
    print()
    print("-" * 92)
    print("[0] 漂移守卫：HIGH_IMPACT_FIELDS 与 source_profiler 的 high 影响字段一致")
    print(f"  arbiter: {sorted(HIGH_IMPACT_FIELDS)}")
    print(f"  profiler high: {sorted(declared)}")
    if set(HIGH_IMPACT_FIELDS) != declared:
        failures.append(f"0: 高影响字段漂移 arbiter={sorted(HIGH_IMPACT_FIELDS)} profiler={sorted(declared)}")

    # 1) 登记失业率 vs IMF LUR —— 行政记录 vs 抽样调查，不可拼接
    r1 = arbitrate_pair(UNEMP_REG, UNEMP_IMF, 1.5, FAKE_SOURCE)
    _show(f"[1] arbitrate_pair({UNEMP_REG!r}, {UNEMP_IMF!r}, 1.5, pp)", _brief(r1))
    if r1["verdict"] != VERDICT_SEPARATE:
        failures.append(f"1: verdict 期望 {VERDICT_SEPARATE}，实际 {r1['verdict']}")
    if r1["comparable"] != "low":
        failures.append(f"1: comparable 期望 low，实际 {r1['comparable']}")
    if r1["recommended_action"] != "keep_separate":
        failures.append(f"1: recommended_action 期望 keep_separate，实际 {r1['recommended_action']}")

    # 2) 调查失业率 vs IMF LUR —— 同口径
    r2 = arbitrate_pair(UNEMP_SUR, UNEMP_IMF, 0.02, FAKE_SOURCE)
    _show(f"[2] arbitrate_pair({UNEMP_SUR!r}, {UNEMP_IMF!r}, 0.02, pp)", _brief(r2))
    if r2["verdict"] != VERDICT_SAME:
        failures.append(f"2: verdict 期望 {VERDICT_SAME}，实际 {r2['verdict']}")
    if r2["recommended_action"] != "splice":
        failures.append(f"2: recommended_action 期望 splice，实际 {r2['recommended_action']}")

    # 3) NBS GDP vs World Bank GDP —— 同源复述，差异是浮点噪声
    r3 = arbitrate_pair(GDP_NBS, GDP_WB, 0.0, FAKE_SOURCE, "percent")
    _show(f"[3] arbitrate_pair({GDP_NBS!r}, {GDP_WB!r}, 0.0, percent)", _brief(r3))
    if r3["verdict"] != VERDICT_SAME:
        failures.append(f"3: verdict 期望 {VERDICT_SAME}，实际 {r3['verdict']}")

    # 4) NBS GDP vs IMF NGDPD —— 现价美元换算，可桥接
    r4 = arbitrate_pair(GDP_NBS, GDP_IMF, 0.0114, FAKE_SOURCE, "percent")
    _show(f"[4] arbitrate_pair({GDP_NBS!r}, {GDP_IMF!r}, 0.0114, percent)", _brief(r4))
    if r4["verdict"] != VERDICT_BRIDGE:
        failures.append(f"4: verdict 期望 {VERDICT_BRIDGE}，实际 {r4['verdict']}")
    if r4["comparable"] != "medium":
        failures.append(f"4: comparable 期望 medium，实际 {r4['comparable']}")

    # 5) unknown_fields 命中高影响字段 -> 人工复核（优先级最高）
    base5 = source_profiler.compare_profiles(GDP_NBS, GDP_WB)
    r5 = _with_fake_compare(base5, ["statistical_method"],
                            lambda: arbitrate_pair(GDP_NBS, GDP_WB, 0.0, FAKE_SOURCE, "percent"))
    _show("[5] 假 unknown_fields=[statistical_method]（其它字段与场景 3 相同）", _brief(r5))
    if r5["verdict"] != VERDICT_MANUAL:
        failures.append(f"5: verdict 期望 {VERDICT_MANUAL}，实际 {r5['verdict']}")
    if r5["high_impact_unknowns"] != ["statistical_method"]:
        failures.append(f"5: high_impact_unknowns 期望 [statistical_method]，实际 {r5['high_impact_unknowns']}")
    if r5["knowledge_gaps"]:
        failures.append(f"5: knowledge_gaps 应为空，实际 {r5['knowledge_gaps']}")
    if "人工复核" not in r5["explanation"]:
        failures.append("5: explanation 未声明降级为人工复核")

    # 6) unknown_fields 只命中低影响字段 -> 保留原判定，记 knowledge_gaps
    r6 = _with_fake_compare(base5, ["start_year"],
                            lambda: arbitrate_pair(GDP_NBS, GDP_WB, 0.0, FAKE_SOURCE, "percent"))
    _show("[6] 假 unknown_fields=[start_year]（其它字段与场景 3 相同）", _brief(r6))
    if r6["verdict"] != r3["verdict"]:
        failures.append(f"6: verdict 应与场景 3 相同（{r3['verdict']}），实际 {r6['verdict']}")
    if r6["knowledge_gaps"] != ["start_year"]:
        failures.append(f"6: knowledge_gaps 期望 [start_year]，实际 {r6['knowledge_gaps']}")
    if r6["high_impact_unknowns"]:
        failures.append(f"6: high_impact_unknowns 应为空，实际 {r6['high_impact_unknowns']}")

    # 7) arbitrate_all：扫 cross_check 目录，不崩
    files = sorted(CROSS_CHECK_DIR.glob("*.json"))
    print()
    print("-" * 92)
    print(f"[7] arbitrate_all() 扫描 {_rel(CROSS_CHECK_DIR)}（{len(files)} 个 JSON）")
    for f in files:
        print(f"      - {_rel(f)}")
    try:
        pairs = arbitrate_all()
    except Exception as exc:  # noqa: BLE001 - 自检要看到任何异常
        pairs = []
        failures.append(f"7: arbitrate_all 抛异常 {type(exc).__name__}: {exc}")
    for rec in pairs:
        print(f"      {rec['verdict']:<6} {rec['pair_key']}"
              f"  (diff={rec['measured']['diff_pp']:.6g} {rec['measured']['diff_type']})")
    if len(files) < 3:
        failures.append(f"7: cross_check 文件数期望 >=3，实际 {len(files)}")
    if len(pairs) < 3:
        failures.append(f"7: arbiter 记录数期望 >=3，实际 {len(pairs)}")
    warns = last_warnings()
    print(f"      warnings: {len(warns)}")
    for w in warns:
        print(f"        * {w}")

    print()
    print("=" * 92)
    if failures:
        for f in failures:
            print(f"  [FAIL] {f}")
        print("=" * 92)
        return 1
    print("self-test 完成（7 个场景全部通过）")
    print("=" * 92)
    return 0


def _main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    p = argparse.ArgumentParser(prog="arbiter",
                                description="口径判定仲裁：画像 × 实测差异 -> 四值判定")
    p.add_argument("--test", action="store_true", help="跑自检（7 个场景，纯离线）")
    p.add_argument("--write", action="store_true",
                   help="把判定落盘到 data/validated/arbiter/")
    p.add_argument("--out-dir", default=None, help="落盘目录（默认 data/validated/arbiter/）")
    a = p.parse_args(argv)

    if a.test:
        return _selftest()
    try:
        if a.write:
            out = write_arbiter_report(a.out_dir)
            print(f"落盘目录: {_rel(out)}")
            return 0
        pairs = arbitrate_all()
        print(json.dumps(pairs, ensure_ascii=False, indent=2))
        return 0
    except ArbiterError as exc:
        print(f"[arbiter] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(_main())
