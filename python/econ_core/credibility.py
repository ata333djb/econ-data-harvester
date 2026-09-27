#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""credibility.py —— 序列可信度评分（方向 A 第三轮）。

它解决什么问题
--------------
arbiter 能回答「两条序列的差异能不能拼」，但用户还需要一眼看出「这条序列本身有多可信」。
本模块把知识库（source_profiles.yaml）、数据血缘（data/validated/ 的落盘长表）
和 arbiter 的判定汇总成一个 0-100 分与 high / medium / low 等级。

五个维度（权重合计 1.00）
------------------------
    Expertise    0.25  发布机构权威性（publishers.authority_level）
    Provenance   0.20  可追溯性（raw_cache + row_sha16 + 落盘文件 + 缺失元数据完整度）
    Timeliness   0.10  时效性（update_frequency；停更 -40）
    Transparency 0.15  方法透明度（definition / revision_policy / references）
    Coherence    0.30  一致性（该序列参与的 arbiter 对比对判定）

分档：score >= 80 -> high；60-79 -> medium；< 60 -> low

两点说明（都写进 evidence 里，不藏着）
-------------------------------------
* Transparency 的三种规范情形来自本轮需求（definition 未知=30 / 有定义但修订政策未知=70 /
  都齐=100）。需求没覆盖的组合按保守原则补：有定义+有修订策略但无 references=80；
  有定义、无修订策略、无 references=50。分支表见 _transparency。
* Provenance 的 ±5 看「缺失行有没有 missing_evidence」。缺失元数据是 fill_strategy 写在
  data/processed/ 的，所以本模块会顺带找同名 processed 文件，找不到才退回 validated 层；
  只有 validated 有缺失行而 processed 未生成时给 -5 —— 那是「证据还没走完流水线」，不是数据错。

用法
----
    ./.venv/Scripts/python.exe -m econ_core.credibility --test
    ./.venv/Scripts/python.exe -m econ_core.credibility           # 打印全部评分
    ./.venv/Scripts/python.exe -m econ_core.credibility --write   # 落盘报告
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from econ_core import arbiter, http_client, source_profiler  # type: ignore[no-redef]
else:
    from . import arbiter, http_client, source_profiler

__all__ = [
    "CredibilityError",
    "score_series",
    "score_all",
    "write_credibility_report",
    "WEIGHTS",
    "DIMENSION_ORDER",
    "DEFAULT_OUT_DIR",
]

PROJECT_ROOT: Path = http_client.PROJECT_ROOT
PROCESSED_DIR: Path = PROJECT_ROOT / "data" / "processed"
DEFAULT_OUT_DIR: Path = PROJECT_ROOT / "data" / "validated" / "credibility"

#: 维度权重（合计必须为 1.0，自检会断言）
WEIGHTS: dict[str, float] = {
    "expertise": 0.25,
    "provenance": 0.20,
    "timeliness": 0.10,
    "transparency": 0.15,
    "coherence": 0.30,
}
DIMENSION_ORDER: tuple[str, ...] = ("expertise", "provenance", "timeliness",
                                   "transparency", "coherence")

#: 与 source_profiler._UNKNOWN 同义（自检断言两者一致），这里本地定义避免跨模块依赖私有名
_UNKNOWN = {"unknown", "", None}

#: Expertise：publishers.authority_level -> 分
_AUTHORITY_SCORE: dict[str, int] = {
    "official": 100, "international": 85, "academic": 75, "commercial": 50,
}
#: Timeliness：更新频率 -> 分（先看 publisher.update_frequency，再退回 indicator.frequency）
_FREQUENCY_SCORE: dict[str, int] = {
    "monthly": 100, "quarterly": 85, "annual": 70, "irregular": 50,
}

_ARBITER_CACHE: Optional[list[dict[str, Any]]] = None


class CredibilityError(RuntimeError):
    """评分层可预期的失败。"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rel(p: Path) -> str:
    """项目内相对路径，统一正斜杠。"""
    try:
        return Path(p).resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(p)


def _is_unknown(value: Any) -> bool:
    return value in _UNKNOWN


def _join(items: Sequence[str]) -> str:
    return "、".join(items)


# --------------------------------------------------------------------------- #
# 数据读取（血缘 + 缺失元数据）
# --------------------------------------------------------------------------- #

def _read_json_rows(rel_path: Optional[str]) -> Optional[list[dict[str, Any]]]:
    """读长表文件的 rows（没有 / 坏了就 None）。"""
    if not rel_path:
        return None
    p = PROJECT_ROOT / str(rel_path)
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    rows = obj.get("rows") if isinstance(obj, dict) else None
    return rows if isinstance(rows, list) else None


def _processed_rows(series_file: Optional[str], source: str) -> Optional[list[dict[str, Any]]]:
    """找 validated 序列对应的 processed 文件（fill_strategy 把 missing_evidence 写在那里）。

    命名规则：validated 文件名去掉 <source>_ 前缀，加 _processed.json，落在 data/processed/<source>/。
    """
    if not series_file:
        return None
    stem = Path(str(series_file)).stem
    prefix = source + "_"
    if source and stem.startswith(prefix):
        stem = stem[len(prefix):]
    target = stem + "_processed.json"
    for cand in sorted(PROCESSED_DIR.glob("*/" + target)):
        return _read_json_rows(_rel(cand))
    return None


# --------------------------------------------------------------------------- #
# 五个维度
# --------------------------------------------------------------------------- #

def _expertise(publisher: dict[str, Any]) -> tuple[int, str]:
    """Expertise：发布机构权威性。"""
    level = publisher.get("authority_level")
    if _is_unknown(level):
        return 60, "publisher.authority_level 未标注（未知，取中性分 60）"
    score = _AUTHORITY_SCORE.get(str(level).lower())
    if score is None:
        return 60, f"authority_level={level!r} 不在映射表里（未知，取 60）"
    return score, f"authority_level={level} -> {score}"


def _provenance(lineage: dict[str, Any], rows: Optional[list[dict[str, Any]]],
                processed: Optional[list[dict[str, Any]]]) -> tuple[int, str]:
    """Provenance：可追溯性（raw_cache / row_sha16 / 落盘文件 + 缺失元数据完整度）。"""
    has_raw = not _is_unknown(lineage.get("raw_cache"))
    has_file = not _is_unknown(lineage.get("series_file")) and bool(rows)
    has_sha = bool(rows) and all(not _is_unknown(r.get("row_sha16")) for r in (rows or []))
    if has_raw and has_sha and has_file:
        base = 100
    elif has_raw and has_sha:
        base = 80
    elif has_raw:
        base = 60
    else:
        base = 30

    blank = "有" if has_raw else "无"
    shat = "齐全" if has_sha else "缺失"
    filet = "有" if has_file else "无"
    note = f"raw_cache={blank} / row_sha16={shat} / series_file={filet} -> {base}"

    adj = 0
    nulls = [r for r in (rows or []) if r.get("value") is None]
    if nulls:
        pool = processed if processed else rows
        ev_nulls = [r for r in (pool or []) if r.get("value") is None] or nulls
        complete = all(not _is_unknown(r.get("missing_evidence")) for r in ev_nulls)
        where = "processed" if processed else "validated"
        adj = 5 if complete else -5
        state = "完整" if complete else "缺失"
        note += f"；缺失行 {len(nulls)} 行，{where} 层的 missing_evidence {state} -> {adj:+d}"
    else:
        note += "；无缺失行，证据完整度不加减"
    return max(0, min(100, base + adj)), note


def _timeliness(indicator: dict[str, Any], publisher: dict[str, Any]) -> tuple[int, str]:
    """Timeliness：时效性（更新频率 + 是否停更）。"""
    freq = publisher.get("update_frequency")
    src = "publisher.update_frequency"
    if _is_unknown(freq):
        freq = indicator.get("frequency")
        src = "indicator.frequency（publisher 没写频率）"
    if _is_unknown(freq):
        base, note = 50, "更新频率未知 -> 50"
    else:
        base = _FREQUENCY_SCORE.get(str(freq).lower(), 50)
        note = f"{src}={freq} -> {base}"
    stopped = indicator.get("discontinued_after")
    if not _is_unknown(stopped):
        note += f"；discontinued_after={stopped}（停更）-> -40"
        base -= 40
    return max(0, base), note


def _transparency(indicator: dict[str, Any]) -> tuple[int, str]:
    """Transparency：方法透明度。

    规范情形（本轮需求）：definition 未知 -> 30；有定义但 revision_policy 未知、references
    非空 -> 70；三者齐全 -> 100。需求未覆盖的组合按保守原则补 80 / 50（见下面分支）。
    """
    has_def = not _is_unknown(indicator.get("definition"))
    has_rev = not _is_unknown(indicator.get("revision_policy"))
    refs = indicator.get("references") or []
    has_refs = bool(refs)
    if not has_def:
        return 30, "definition 未知（方法说不清）-> 30"
    if has_rev and has_refs:
        return 100, f"definition + revision_policy 都有、references {len(refs)} 条 -> 100"
    if has_rev and not has_refs:
        return 80, "definition + revision_policy 都有、但没有 references（需求未覆盖，保守取 80）"
    if not has_rev and has_refs:
        return 70, f"有 definition、revision_policy 未知、references {len(refs)} 条 -> 70"
    return 50, "有 definition，但 revision_policy 未知且无 references（需求未覆盖，保守取 50）"


def _coherence(verdicts: Sequence[str]) -> tuple[int, str]:
    """Coherence：该序列参与的 arbiter 判定（口径差异不是数据错误，不重罚）。"""
    uniq = sorted(set(verdicts))
    if not uniq:
        return 70, "未参与任何 arbiter 对比对（无信息，默认 70）"
    if arbiter.VERDICT_MANUAL in uniq:
        return 40, f"判定含「人工复核」（{_join(uniq)}）-> 40"
    if arbiter.VERDICT_SEPARATE in uniq:
        tail = "（口径差异，不是数据错误，故不重罚）"
        return 60, f"判定含「不可拼接」（{_join(uniq)}）-> 60" + tail
    if arbiter.VERDICT_BRIDGE in uniq:
        return 80, f"判定含「可桥接」且无冲突（{_join(uniq)}）-> 80"
    return 100, f"判定全部「同口径」（{_join(uniq)}）-> 100"


# --------------------------------------------------------------------------- #
# 评分
# --------------------------------------------------------------------------- #

def _arbiter_pairs(refresh: bool = False) -> list[dict[str, Any]]:
    """arbiter 的全部判定（进程内缓存；跑 11 次 score_series 也只扫一遍）。"""
    global _ARBITER_CACHE
    if _ARBITER_CACHE is None or refresh:
        try:
            _ARBITER_CACHE = arbiter.arbitrate_all()
        except arbiter.ArbiterError:
            _ARBITER_CACHE = []
    return _ARBITER_CACHE


def _arbiter_ref(pair: dict[str, Any]) -> str:
    """该判定对应的报告文件；报告还没落盘时退回 cross_check 原始产物。"""
    f = arbiter.pair_report_path(pair["pair_key"])
    return _rel(f) if f.is_file() else str(pair["measured"]["source"])


def score_series(series_key: str) -> dict[str, Any]:
    """给一条序列打分。

    :param series_key: 序列键（可用别名键，内部解析到规范键）。
    :returns: series_key / display_name / score / grade / dimensions / arbiter_refs / scored_at。
    :raises KeyError: series_key 不在知识库。
    """
    profile = source_profiler.profile_series(series_key)
    canon = str(profile["series_key"])
    publisher = profile.get("publisher") or {}
    indicator = profile.get("indicator") or {}
    lineage = profile.get("data_lineage") or {}
    series_file = lineage.get("series_file")

    rows = _read_json_rows(series_file)
    processed = _processed_rows(series_file, str(publisher.get("key") or ""))

    pairs = [q for q in _arbiter_pairs()
             if canon in (q.get("series_a"), q.get("series_b"))]

    e = _expertise(publisher)
    p = _provenance(lineage, rows, processed)
    t = _timeliness(indicator, publisher)
    x = _transparency(indicator)
    c = _coherence([str(q.get("verdict")) for q in pairs])

    dimensions = {
        "expertise": {"score": e[0], "weight": WEIGHTS["expertise"], "evidence": e[1]},
        "provenance": {"score": p[0], "weight": WEIGHTS["provenance"], "evidence": p[1]},
        "timeliness": {"score": t[0], "weight": WEIGHTS["timeliness"], "evidence": t[1]},
        "transparency": {"score": x[0], "weight": WEIGHTS["transparency"], "evidence": x[1]},
        "coherence": {"score": c[0], "weight": WEIGHTS["coherence"], "evidence": c[1]},
    }
    total = round(sum(d["score"] * d["weight"] for d in dimensions.values()), 2)
    grade = "high" if total >= 80 else ("medium" if total >= 60 else "low")

    return {
        "series_key": canon,
        "display_name": str(indicator.get("display_name") or canon),
        "score": total,
        "grade": grade,
        "dimensions": dimensions,
        "arbiter_refs": [_arbiter_ref(q) for q in pairs],
        "scored_at": _utc_now(),
    }


def _scorable_keys() -> list[str]:
    """知识库里所有规范键（排除 alias 条目——那是同一条序列的另一种键形）。"""
    kb = source_profiler.load_knowledge()
    indicators = kb.get("indicators") or {}
    keys: list[str] = []
    for key, item in indicators.items():
        if isinstance(item, dict) and item.get("alias_of"):
            continue
        keys.append(str(key))
    return keys


def score_all() -> list[dict[str, Any]]:
    """给知识库里所有序列打分（按分数从高到低）。"""
    return sorted((score_series(k) for k in _scorable_keys()),
                  key=lambda r: (-r["score"], r["series_key"]))


def write_credibility_report(out_dir: Optional[Path] = None) -> Path:
    """落盘：每条序列一个 JSON + 一个 _index.json（按分数排序）。

    :returns: 落盘目录。
    """
    out = Path(out_dir) if out_dir else DEFAULT_OUT_DIR
    records = score_all()
    out.mkdir(parents=True, exist_ok=True)

    index: dict[str, Any] = {"scored_at": _utc_now(), "n_series": len(records),
                             "weights": dict(WEIGHTS), "by_grade": {},
                             "dimension_order": list(DIMENSION_ORDER), "series": []}
    for rank, rec in enumerate(records, 1):
        fname = arbiter.safe_stem(rec["series_key"]) + ".json"
        (out / fname).write_text(
            json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
        index["by_grade"][rec["grade"]] = index["by_grade"].get(rec["grade"], 0) + 1
        index["series"].append({
            "rank": rank,
            "series_key": rec["series_key"],
            "display_name": rec["display_name"],
            "score": rec["score"],
            "grade": rec["grade"],
            "dimensions": {k: rec["dimensions"][k]["score"] for k in DIMENSION_ORDER},
            "file": fname,
        })
    (out / "_index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #

GDP = "nbs|gdp|cny_100m"
REG = "nbs|registered_unemployment"
LUR = "imf|LUR"


def _print_record(rec: dict[str, Any]) -> None:
    print(f"  {rec['score']:.2f}  {rec['grade']:<6}  {rec['series_key']}")
    print(f"          display_name: {rec['display_name']}")
    for k in DIMENSION_ORDER:
        d = rec["dimensions"][k]
        print(f"          {k:<12} {d['score']:>3} x {d['weight']:.2f}  {d['evidence']}")
    for ref in rec["arbiter_refs"]:
        print(f"          ref: {ref}")


def _selftest() -> int:
    failures: list[str] = []
    print("=" * 92)
    print("self-test: credibility（五维可信度评分：Expertise / Provenance / Timeliness / Transparency / Coherence）")
    print("=" * 92)

    # 0) 漂移守卫
    print()
    print("-" * 92)
    print("[0] 漂移守卫：权重合计 1.0；_UNKNOWN 与 source_profiler 一致")
    total_w = sum(WEIGHTS.values())
    print(f"  weights={WEIGHTS}  合计={total_w}")
    if abs(total_w - 1.0) > 1e-9:
        failures.append(f"0: 权重合计应为 1.0，实际 {total_w}")
    if set(WEIGHTS) != set(DIMENSION_ORDER):
        failures.append(f"0: WEIGHTS 与 DIMENSION_ORDER 的键不一致")
    if _UNKNOWN != set(source_profiler._UNKNOWN):
        failures.append("0: _UNKNOWN 与 source_profiler._UNKNOWN 漂移")
    print(f"  _UNKNOWN={sorted(str(x) for x in _UNKNOWN)}")

    # 1) 官方 + 可追溯 + 定义清晰 + 与 WB 同口径
    r1 = score_series(GDP)
    print()
    print("-" * 92)
    print(f"[1] score_series({GDP!r})  期望 score >= 85 且 grade=high")
    _print_record(r1)
    if r1["score"] < 85:
        failures.append(f"1: score 期望 >= 85，实际 {r1['score']}")
    if r1["grade"] != "high":
        failures.append(f"1: grade 期望 high，实际 {r1['grade']}")

    # 2) 停更 + 与 imf|LUR 不可拼接
    r2 = score_series(REG)
    print()
    print("-" * 92)
    print(f"[2] score_series({REG!r})  期望 60 <= score <= 80")
    _print_record(r2)
    if not (60 <= r2["score"] <= 80):
        failures.append(f"2: score 期望 60-80，实际 {r2['score']}")
    if r2["dimensions"]["timeliness"]["score"] > 60:
        failures.append("2: 停更序列的 timeliness 应被 -40")

    # 3) IMF LUR
    r3 = score_series(LUR)
    print()
    print("-" * 92)
    print(f"[3] score_series({LUR!r})  期望 score >= 75 且 grade in (high, medium)")
    _print_record(r3)
    if r3["score"] < 75:
        failures.append(f"3: score 期望 >= 75，实际 {r3['score']}")
    if r3["grade"] not in ("high", "medium"):
        failures.append(f"3: grade 期望 high/medium，实际 {r3['grade']}")

    # 4) 未知 key
    print()
    print("-" * 92)
    print("[4] score_series('unknown|key') 应抛 KeyError")
    try:
        score_series("unknown|key")
        failures.append("4: 未抛 KeyError")
    except KeyError as exc:
        print(f"  OK KeyError: {str(exc)[:100]}")

    # 5) score_all
    print()
    print("-" * 92)
    print("[5] score_all() 期望 9-11 条（知识库规范键，不含 alias）")
    records = score_all()
    for i, rec in enumerate(records, 1):
        dims = " ".join(f"{k[:4]}={rec['dimensions'][k]['score']}" for k in DIMENSION_ORDER)
        print(f"  {i:>2}. {rec['score']:>6.2f}  {rec['grade']:<6}  {rec['series_key']}")
        print(f"      {dims}")
    print(f"  合计 {len(records)} 条")
    expected_n = len(_scorable_keys())
    if len(records) != expected_n or expected_n < 9:
        failures.append(f"5: score_all 应覆盖知识库全部规范键（{expected_n} 条），实际 {len(records)}")

    print()
    print("=" * 92)
    if failures:
        for f in failures:
            print(f"  [FAIL] {f}")
        print("=" * 92)
        return 1
    print("self-test 完成（5 个场景全部通过）")
    print("=" * 92)
    return 0


def _main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    p = argparse.ArgumentParser(prog="credibility",
                                description="序列可信度评分：五维加权 0-100 + high/medium/low")
    p.add_argument("--test", action="store_true", help="跑自检（5 个场景，纯离线）")
    p.add_argument("--write", action="store_true",
                   help="把评分落盘到 data/validated/credibility/")
    p.add_argument("--out-dir", default=None, help="落盘目录（默认 data/validated/credibility/）")
    a = p.parse_args(argv)

    if a.test:
        return _selftest()
    try:
        if a.write:
            out = write_credibility_report(a.out_dir)
            print(f"落盘目录: {_rel(out)}")
            return 0
        for rec in score_all():
            _print_record(rec)
            print()
        return 0
    except CredibilityError as exc:
        print(f"[credibility] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(_main())

