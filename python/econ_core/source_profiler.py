#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""来源画像（source profiler）—— 回答"这条序列是谁、怎么算出来的、能不能和别的拼"。

为什么需要它
------------
系统现在能**发现**分歧（例如"城镇登记失业率比 IMF LUR 低 1.5 pp"），但说不出**为什么**。
差异可能来自：统计方法不同（行政记录 vs 抽样调查）、覆盖范围不同、修订机制不同，
或者干脆是舍入噪声。这四种情况的处理方式完全不同：前两种不能拼接，后一种可以直接拼。

要做到这一点，每条序列需要一份**来源画像**。而画像**不能全靠自动抽**：
数据层里只有 indicator_name / unit / raw_cache 这类表层字段，"统计方法""覆盖范围"
这些必须人工编纂 —— 这就是同目录 `source_profiles.yaml` 存在的原因。

本模块的职责
------------
* 把知识库（人工编纂）+ 数据血缘（自动抽取）**合起来**成一条序列的画像；
* 两条画像对比，给出可比性判定与差异清单；
* 已知差异幅度时，给出归因、解释与建议动作。

三个接口
--------
`profile_series(key)`              单序列画像（含 publisher / indicator / data_lineage / known_gaps）
`compare_profiles(a, b)`           两序列画像对比（comparable + differences + reasoning）
`explain_divergence(a, b, diff_pp)`  已知差异（百分点）时的归因与建议

数据血缘是**尽力而为**的
------------------------
`data/validated/` 是按需生成的（data/ 目录被 gitignore）。在干净检出上第一次跑门禁时，
画像里查不到落盘的序列文件是正常的 —— 此时 `data_lineage` 给出 null 与 note，
`known_gaps` 为空，但**知识库部分照常可用**。所以自检只断言知识库字段，不断言血缘。

用法
----
    .\\.venv\\Scripts\\python.exe -m econ_core.source_profiler --test
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from econ_core import http_client  # type: ignore[no-redef]
else:
    from . import http_client

__all__ = [
    "SourceProfileError",
    "profile_series",
    "compare_profiles",
    "explain_divergence",
    "load_knowledge",
    "PROFILES_PATH",
    "VALIDATED_DIR",
]

PROFILES_PATH: Path = Path(__file__).resolve().parent / "source_profiles.yaml"
VALIDATED_DIR: Path = http_client.PROJECT_ROOT / "data" / "validated"

#: 参与对比的指标字段 -> 差异影响权重
#: 「口径类」字段一旦不同就是 high：它们决定两条序列是不是在量同一件事。
_COMPARE_FIELDS: tuple[tuple[str, str], ...] = (
    ("statistical_method", "high"),
    ("coverage", "high"),
    ("population_scope", "high"),
    ("collecting_agency", "medium"),
    ("definition", "medium"),
    ("unit", "medium"),
    ("frequency", "low"),
    ("start_year", "low"),
    ("discontinued_after", "low"),
    ("revision_policy", "low"),
)

#: 需要"按家族比对"的字段 -> YAML families 段里的键
#: （自由文本措辞不同不代表口径不同，直接字符串比较会产生假差异）
_FAMILY_OF: dict[str, str] = {
    "statistical_method": "method",
    "coverage": "coverage",
    "population_scope": "scope",
}

#: 缺失值哨兵：知识库里查不到就写它，对比时视为"无依据"而不是"不同"
_UNKNOWN = {"unknown", "", None}

#: 口径一致 + 差异小于这个百分点数 -> 判为噪声（可拼接）
_NOISE_PP: float = 0.1

_KB_CACHE: dict[str, Any] = {}


class SourceProfileError(RuntimeError):
    """画像层可预期的失败（知识库缺失/结构不符）。"""


def load_knowledge(path: Optional[Path] = None) -> dict[str, Any]:
    """加载 `source_profiles.yaml`（带进程内缓存）。

    :raises SourceProfileError: 缺 PyYAML、文件不存在、或三段结构不完整。
    """
    p = Path(path) if path else PROFILES_PATH
    cache_key = str(p)
    if cache_key in _KB_CACHE:
        return _KB_CACHE[cache_key]

    try:
        import yaml  # 局部导入：缺依赖时报错更清楚
    except ImportError as exc:  # pragma: no cover - 环境问题
        raise SourceProfileError(
            "加载来源画像知识库需要 PyYAML。请先安装："
            ".\\.venv\\Scripts\\python.exe pip_sandbox_install.py install pyyaml"
        ) from exc

    if not p.is_file():
        raise SourceProfileError(f"知识库文件不存在: {p}")
    try:
        obj = yaml.safe_load(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - yaml 的异常类型多变
        raise SourceProfileError(f"知识库 YAML 解析失败 {p}: {exc}") from exc

    if not isinstance(obj, dict):
        raise SourceProfileError(f"知识库顶层不是 mapping: {type(obj).__name__}")
    for sec in ("meta", "publishers", "indicators", "fields"):
        if not isinstance(obj.get(sec), (dict,)):
            raise SourceProfileError(f"知识库缺少 {sec!r} 段或类型不对")
    _KB_CACHE[cache_key] = obj
    return obj


def _canonical(key: str, kb: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """解析 series_key：跟随 alias_of 直到规范键。

    :returns: (规范 key, 该 key 的 indicator 条目)
    :raises KeyError: 知识库里没有这个 key（错误信息里给可用键）。
    """
    indicators = kb["indicators"]
    seen: list[str] = []
    cur = key
    while True:
        if cur not in indicators:
            avail = ", ".join(sorted(indicators)[:12])
            raise KeyError(
                f"知识库里没有 series_key={key!r}"
                + (f"（alias 链: {' -> '.join(seen + [cur])}）" if seen else "")
                + f"。可用键（前 12 个）: {avail}"
            )
        entry = indicators[cur]
        if not isinstance(entry, dict):
            raise SourceProfileError(f"知识库条目 {cur!r} 不是 mapping")
        nxt = entry.get("alias_of")
        if not nxt:
            return cur, entry
        seen.append(cur)
        if nxt in seen:
            raise SourceProfileError(f"alias 成环: {' -> '.join(seen + [str(nxt)])}")
        cur = str(nxt)


def _find_series_file(key: str) -> tuple[Optional[Path], Optional[dict[str, Any]]]:
    """在 data/validated/ 下找 series_key 匹配的长表文件（尽力而为）。"""
    if not VALIDATED_DIR.is_dir():
        return None, None
    for f in sorted(VALIDATED_DIR.rglob("*.json")):
        if f.name == "missing_report.json":
            continue
        try:
            obj = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(obj, dict) and obj.get("series_key") == key and isinstance(obj.get("rows"), list):
            return f, obj
    return None, None


def _report_gaps(key: str) -> list[dict[str, Any]]:
    """从 data/validated/missing_report.json 读该序列的缺口（尽力而为）。"""
    p = VALIDATED_DIR / "missing_report.json"
    if not p.is_file():
        return []
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    for s in (obj.get("series") or []):
        if isinstance(s, dict) and str(s.get("series_key")) == key:
            return list(s.get("gaps") or [])
    return []


# --------------------------------------------------------------------------- #
# 单序列画像
# --------------------------------------------------------------------------- #

def _lineage(key: str, f: Optional[Path], obj: Optional[dict[str, Any]]) -> dict[str, Any]:
    """数据血缘：从落盘的长表文件里抽（尽力而为，没有就给 null + note）。"""
    if not obj:
        return {
            "raw_cache": None, "row_count": 0, "n_missing": 0, "fetched_at": None,
            "series_file": None, "expected_periods": None,
            "note": "data/validated/ 下没有该序列的落盘文件"
                    "（跑 tools/materialize-validated.py 后即可生成）",
        }
    rows = [r for r in (obj.get("rows") or []) if isinstance(r, dict)]
    return {
        "raw_cache": next((str(r["raw_cache"]) for r in rows if r.get("raw_cache")), None),
        "row_count": len(rows),
        "n_missing": sum(1 for r in rows if r.get("value") is None),
        "fetched_at": next((str(r["fetched_at"]) for r in rows if r.get("fetched_at")), None),
        "series_file": str(f.relative_to(http_client.PROJECT_ROOT)) if f else None,
        "expected_periods": obj.get("expected_periods"),
        "note": "",
    }


def profile_series(series_key: str, kb: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """返回一条序列的完整画像（知识库 + 数据血缘 + 已知缺口）。

    :param series_key: 序列键。知识库里带 `alias_of` 的键会被解析到规范键
                       （例如 `NBS|000000000000|db8e...` -> `nbs|gdp|cny_100m`）。
    :returns: `{"series_key", "requested_key", "is_alias", "publisher", "indicator",
              "data_lineage", "profile_version", "known_gaps"}`
    :raises KeyError: series_key 不在知识库（消息里列出可用键）。
    :raises SourceProfileError: 知识库缺失/结构不符/alias 成环。
    """
    k = kb or load_knowledge()
    canon, ind = _canonical(series_key, k)

    pub_key = canon.split("|")[0].lower()
    publisher = dict(k["publishers"].get(pub_key) or {})
    if not publisher:
        publisher = {"key": pub_key, "name": "unknown",
                     "notes": "知识库 publishers 段里没有这个机构"}

    f, obj = _find_series_file(canon)
    if obj is None and canon != series_key:
        f, obj = _find_series_file(series_key)

    return {
        "series_key": canon,
        "requested_key": series_key,
        "is_alias": canon != series_key,
        "publisher": publisher,
        "indicator": dict(ind),
        "data_lineage": _lineage(canon, f, obj),
        "profile_version": str((k.get("meta") or {}).get("updated") or "unknown"),
        "known_gaps": _report_gaps(canon),
    }


# --------------------------------------------------------------------------- #
# 两序列对比
# --------------------------------------------------------------------------- #

def _reasoning(comparable: str, rule: str, differences: list[dict[str, Any]],
               name_a: str, name_b: str) -> str:
    """把可比性判定与差异清单写成一句人话。"""
    head = {
        "high": "两序列口径一致，可直接拼接比对",
        "medium": "两序列统计方法相同但覆盖范围不同，需要桥接后才能拼接",
        "low": "两序列统计方法或覆盖范围不同，属口径差异",
        "incompatible": "两序列口径互不相容",
    }.get(comparable, "可比性未知")
    keys = [d["field"] for d in differences if d["impact"] == "high"]
    if keys and comparable in ("high", "medium"):
        # 自由文本不同但家族一致（或知识库已显式判定可比），要说清楚"这不是阻碍"
        tail = f"以下字段自由文本不同但口径家族一致、已被可比性判定覆盖: {', '.join(keys)}"
    elif keys:
        tail = f"关键差异字段: {', '.join(keys)}"
    else:
        tail = "没有高影响差异字段"
    return f"{name_a} vs {name_b}：{head}（判定依据：{rule}）。{tail}。"


def compare_profiles(key_a: str, key_b: str,
                     kb: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """对比两条序列的画像，给出可比性与差异清单。

    可比性判定规则（前者优先）：
      1. 知识库 `comparability` 表里显式标注（`with_<对方 series_key>`）-> 直接采用；
      2. 同 `indicator_id`（同源复述）-> `high`；
      3. `statistical_method` 相同且 `coverage` 相同 -> `high`；
      4. `statistical_method` 相同、覆盖不同 -> `medium`（需桥接）；
      5. 方法不同，或知识库标 `low` -> `low`；
      6. 知识库标 `incompatible` -> `incompatible`。

    「unknown」字段既不算相同也不算不同，单独放进 `unknown_fields` —— 那是**知识缺口**，
    不是口径分歧，别混为一谈。

    :returns: `{"key_a", "key_b", "series_key_a", "series_key_b", "comparable",
              "decision_rule", "differences", "shared_attributes", "unknown_fields",
              "reasoning"}`
    """
    k = kb or load_knowledge()
    pa = profile_series(key_a, k)
    pb = profile_series(key_b, k)
    ia, ib = pa["indicator"], pb["indicator"]
    ka, kbk = pa["series_key"], pb["series_key"]

    explicit: Optional[str] = None
    for src, other in ((ia.get("comparability") or {}, kbk),
                       (ib.get("comparability") or {}, ka)):
        if isinstance(src, dict):
            v = src.get(f"with_{other}")
            if v not in (None, ""):
                explicit = str(v)
                break

    families = k.get("families") or {}
    fam_a = families.get(ka) or {}
    fam_b = families.get(kbk) or {}

    differences: list[dict[str, Any]] = []
    shared: list[dict[str, Any]] = []
    unknown_fields: list[str] = []
    for field, impact in _COMPARE_FIELDS:
        va, vb = ia.get(field), ib.get(field)
        if va in _UNKNOWN or vb in _UNKNOWN:
            unknown_fields.append(field)
            continue
        # statistical_method / coverage 是自由文本，措辞不同 != 口径不同。
        # 有家族值时先用家族判断（见 YAML 的 families 段说明），否则退回字符串比较。
        fam_field = _FAMILY_OF.get(field)
        fa = fam_a.get(fam_field) if fam_field else None
        fb = fam_b.get(fam_field) if fam_field else None
        if fam_field and fa not in _UNKNOWN and fb not in _UNKNOWN:
            same = fa == fb
        else:
            same = va == vb
        if same:
            shared.append({"field": field, "value": va})
        else:
            differences.append({"field": field, "a": va, "b": vb, "impact": impact})

    if explicit in ("high", "medium", "low", "incompatible"):
        comparable, rule = explicit, f"知识库 comparability 显式标注 {explicit}"
    else:
        same_iid = bool(ia.get("indicator_id")) and ia.get("indicator_id") == ib.get("indicator_id")
        same_method = str(ia.get("statistical_method")) == str(ib.get("statistical_method"))
        same_cov = str(ia.get("coverage")) == str(ib.get("coverage"))
        if same_iid:
            comparable, rule = "high", "同 indicator_id（同源复述）"
        elif same_method and same_cov:
            comparable, rule = "high", "statistical_method 与 coverage 都相同"
        elif same_method:
            comparable, rule = "medium", "statistical_method 相同、coverage 不同"
        else:
            comparable, rule = "low", "statistical_method 不同"

    name_a = str(ia.get("display_name") or ka)
    name_b = str(ib.get("display_name") or kbk)
    return {
        "key_a": key_a,
        "key_b": key_b,
        "series_key_a": ka,
        "series_key_b": kbk,
        "comparable": comparable,
        "decision_rule": rule,
        "differences": differences,
        "shared_attributes": shared,
        "unknown_fields": unknown_fields,
        "reasoning": _reasoning(comparable, rule, differences, name_a, name_b),
    }


# --------------------------------------------------------------------------- #
# 差异归因
# --------------------------------------------------------------------------- #

def explain_divergence(key_a: str, key_b: str, diff_pp: float,
                       kb: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """已知两条序列的差异（**百分点**）时，给出归因、人话解释与建议动作。

    归因优先级：
      1. 口径一致且差异小于 :data:_NOISE_PP -> `noise`（可拼接）；
      2. `statistical_method` 不同 -> `statistical_method`（不可拼接）；
      3. `coverage` / `population_scope` 不同 -> `coverage`（需桥接）；
      4. `revision_policy` 不同 -> `revision`（需桥接）；
      5. 其余 -> `unknown`（保守：不拼）。

    :param diff_pp: 已知差异，**单位是百分点**（如登记失业率与 IMF LUR 差 1.5 pp）。
    :returns: `{"divergence_pp", "attribution", "explanation", "recommended_action",
              "confidence", "comparable", "decision_rule"}`
    """
    k = kb or load_knowledge()
    cmp_ = compare_profiles(key_a, key_b, k)
    pa = profile_series(key_a, k)
    pb = profile_series(key_b, k)
    ia, ib = pa["indicator"], pb["indicator"]
    name_a = str(ia.get("display_name") or pa["series_key"])
    name_b = str(ib.get("display_name") or pb["series_key"])
    key_a_full = pa["series_key"]
    key_b_full = pb["series_key"]
    d = abs(float(diff_pp))
    diffs = {x["field"]: x for x in cmp_["differences"]}
    hint = str(ia.get("divergence_hint") or ib.get("divergence_hint") or "")

    if d <= _NOISE_PP and cmp_["comparable"] in ("high", "medium"):
        attribution, action, confidence = "noise", "splice", "high"
        explanation = (
            f"{name_a} 与 {name_b} 的口径画像一致（{cmp_['decision_rule']}）。"
            f"实测差异仅 {d:.2f} pp，属舍入/修订级噪声"
            + (f"：{hint}" if hint else "")
            + "。可直接拼接为一条序列。"
        )
    elif "statistical_method" in diffs:
        attribution, action, confidence = "statistical_method", "keep_separate", "high"
        da = diffs["statistical_method"]
        explanation = (
            f"{name_a} 采用「{da['a']}」，{name_b} 采用「{da['b']}」。"
            + (f"{hint}。" if hint else "")
            + f"差异 {d:.2f} pp 属统计方法造成的**口径差异，不是数据错误**；"
              f"建议作为两条独立序列，不拼接。"
        )
    elif "coverage" in diffs or "population_scope" in diffs:
        attribution, action, confidence = "coverage", "bridge", "medium"
        fld = "coverage" if "coverage" in diffs else "population_scope"
        dc = diffs[fld]
        explanation = (
            f"两序列统计方法相同，但 {fld} 不同：{name_a} 为「{dc['a']}」，"
            f"{name_b} 为「{dc['b']}」。差异 {d:.2f} pp 来自覆盖范围；"
            f"若要合并需先按覆盖范围做桥接（权重或子集对齐），不建议直接拼接。"
        )
    elif "revision_policy" in diffs:
        attribution, action, confidence = "revision", "bridge", "medium"
        dr = diffs["revision_policy"]
        explanation = (
            f"两序列修订机制不同（{name_a}：「{dr['a']}」；{name_b}：「{dr['b']}」）。"
            f"差异 {d:.2f} pp 可能是同一年份被不同批次修订所致；"
            f"建议用同一修订批次的数据拼接，或先做版本对齐。"
        )
    elif cmp_["comparable"] == "high":
        attribution, action, confidence = "unknown", "splice", "medium"
        explanation = (
            f"两序列画像一致但差异 {d:.2f} pp 超出噪声阈值 {_NOISE_PP} pp；"
            f"知识库不足以解释，建议先核对是否为同一修订批次再决定。"
        )
    else:
        attribution, action, confidence = "unknown", "keep_separate", "low"
        explanation = (
            f"知识库里没有足够的字段解释这 {d:.2f} pp 的差异"
            f"（可比性判定 {cmp_['comparable']}）；保守处理：保持两条独立序列。"
        )

    return {
        "divergence_pp": diff_pp,
        "attribution": attribution,
        "explanation": explanation,
        "recommended_action": action,
        "confidence": confidence,
        "comparable": cmp_["comparable"],
        "decision_rule": cmp_["decision_rule"],
        "key_a": key_a_full,
        "key_b": key_b_full,
    }


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #

GDP_ALIAS = "NBS|000000000000|db8e5a86c08246e79b1b11251927e740"
GDP_CANON = "nbs|gdp|cny_100m"
REG = "nbs|registered_unemployment"
SUR = "nbs|surveyed_unemployment"
LUR = "imf|LUR"


def _show(title: str, obj: Any) -> None:
    print()
    print("-" * 92)
    print(title)
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _selftest() -> int:
    failures: list[str] = []
    print("=" * 92)
    print("self-test: source_profiler（知识库 + 血缘 + 对比 + 归因）")
    print("=" * 92)

    # 1) 登记失业率
    p_reg = profile_series(REG)
    _show(f"[1] profile_series({REG!r})", p_reg)
    if p_reg["indicator"].get("statistical_method") != "行政记录（失业登记台账逐级汇总）":
        failures.append("1: statistical_method 不是行政记录")
    if p_reg["indicator"].get("discontinued_after") != 2021:
        failures.append(f"1: discontinued_after 期望 2021，实际 {p_reg['indicator'].get('discontinued_after')!r}")

    # 2) 调查失业率
    p_sur = profile_series(SUR)
    _show(f"[2] profile_series({SUR!r})", p_sur)
    if not str(p_sur["indicator"].get("statistical_method", "")).startswith("抽样调查"):
        failures.append("2: statistical_method 不是抽样调查")
    if p_sur["indicator"].get("start_year") != 2018:
        failures.append(f"2: start_year 期望 2018，实际 {p_sur['indicator'].get('start_year')!r}")

    # 3) alias 解析
    p_alias = profile_series(GDP_ALIAS)
    p_canon = profile_series(GDP_CANON)
    _show(f"[3] profile_series({GDP_ALIAS!r})  <- alias", p_alias)
    if p_alias["series_key"] != GDP_CANON:
        failures.append(f"3: 规范键期望 {GDP_CANON}，实际 {p_alias['series_key']}")
    if not p_alias.get("is_alias"):
        failures.append("3: is_alias 应为 True")
    if p_alias["indicator"] != p_canon["indicator"]:
        failures.append("3: alias 解析后的 indicator 与规范键不一致")
    if p_alias["publisher"].get("key") != "nbs":
        failures.append("3: publisher 应为 nbs")

    # 4) 登记 vs 调查
    c4 = compare_profiles(REG, SUR)
    _show(f"[4] compare_profiles({REG!r}, {SUR!r})", c4)
    if c4["comparable"] != "low":
        failures.append(f"4: comparable 期望 low，实际 {c4['comparable']}")
    fields4 = {d["field"] for d in c4["differences"]}
    for need in ("statistical_method", "coverage"):
        if need not in fields4:
            failures.append(f"4: differences 缺少 {need}（实际 {sorted(fields4)}）")

    # 5) 调查 vs IMF LUR
    c5 = compare_profiles(SUR, LUR)
    _show(f"[5] compare_profiles({SUR!r}, {LUR!r})", c5)
    if c5["comparable"] != "high":
        failures.append(f"5: comparable 期望 high，实际 {c5['comparable']}")

    # 6) 归因：行政记录 vs 抽样调查
    e6 = explain_divergence(REG, LUR, 1.5)
    _show(f"[6] explain_divergence({REG!r}, {LUR!r}, 1.5)", e6)
    if e6["attribution"] != "statistical_method":
        failures.append(f"6: attribution 期望 statistical_method，实际 {e6['attribution']}")
    if "行政记录只统计主动登记者" not in e6["explanation"]:
        failures.append("6: explanation 未说明「行政记录只统计主动登记者」")
    if e6["recommended_action"] != "keep_separate":
        failures.append(f"6: recommended_action 期望 keep_separate，实际 {e6['recommended_action']}")

    # 7) 归因：同口径的微小差异
    e7 = explain_divergence(SUR, LUR, 0.02)
    _show(f"[7] explain_divergence({SUR!r}, {LUR!r}, 0.02)", e7)
    if e7["attribution"] not in ("unknown", "noise"):
        failures.append(f"7: attribution 期望 unknown/noise，实际 {e7['attribution']}")
    if e7["recommended_action"] != "splice":
        failures.append(f"7: recommended_action 期望 splice，实际 {e7['recommended_action']}")

    # 8) 未知 key
    print()
    print("-" * 92)
    print("[8] 未知 series_key 应抛 KeyError")
    try:
        profile_series("nbs|does_not_exist")
        failures.append("8: 未抛 KeyError")
    except KeyError as exc:
        print(f"  OK KeyError: {str(exc)[:120]}")
    try:
        compare_profiles(REG, "bogus|key")
        failures.append("8: compare_profiles 未抛 KeyError")
    except KeyError:
        print("  OK compare_profiles 同样抛 KeyError")

    print()
    print("=" * 92)
    if failures:
        for f in failures:
            print(f"  [FAIL] {f}")
        print("=" * 92)
        return 1
    print("self-test 完成 ✔（8 个场景全部通过）")
    print("=" * 92)
    return 0


def _main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    p = argparse.ArgumentParser(prog="source_profiler",
                                description="来源画像：知识库 + 数据血缘 + 可比性判定")
    p.add_argument("--test", action="store_true", help="跑自检")
    a = p.parse_args(argv)
    if a.test:
        return _selftest()
    p.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())


