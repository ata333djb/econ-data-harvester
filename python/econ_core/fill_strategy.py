#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""填补策略执行器 —— 对每条缺口给出**动作决策**，然后执行（本轮只执行 leave_null / wait）。

为什么本轮不执行任何插值
------------------------
实测：当前数据集里 `true_gap`（中段空洞）**0 个实例**。所有缺失都落在头部
（`series_start`，序列起点晚）或尾部（`not_yet_published` / `discontinued`）。
这四类里有三类**本来就不该补**：

* `discontinued` —— 制度下线，外推不是补数据，是**伪造数据**；
* `series_start` —— 序列本来就从这个时点才开始，往前补等于凭空发明历史；
* `not_yet_published` —— 官方还没发布，补了就是编。

所以本轮的正确行为是**空跑**：把决策逐条算出来、把结论写进数据，但不改任何数值。
出现 `interpolate` 会直接抛 NotImplementedError —— 因为那意味着数据集或策略变了，
必须先把真正的插值实现与回归补上，而不是让它悄悄生效。

职责边界
--------
* 分类（缺哪种缺失）在 `econ_core.missing`，本模块**不重复实现**，只消费它的 gap 结构。
* 本模块只做"缺口 -> 动作决策 -> 给处理后的长表打元数据"。
* `value` **永远保持原样**（缺失就是 null）。新增字段是**元数据**，不是值：
  `missing_classification` / `missing_evidence` / `missing_action`。

输出
----
    data/processed/<source>/<indicator>_processed.json
    {"rows": [...], "missing_report": {...}, "decisions": [...], "processed_at": "..."}

用法
----
    .\\.venv\\Scripts\\python.exe -m econ_core.fill_strategy --test
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Optional, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from econ_core import http_client, missing  # type: ignore[no-redef]
else:
    from . import http_client, missing

__all__ = [
    "FillStrategyError",
    "decide",
    "apply_strategy",
    "apply_to_validated_dir",
    "last_run",
    "POLICY",
    "PROCESSED_DIR",
]

PROJECT_ROOT: Path = http_client.PROJECT_ROOT
VALIDATED_DIR: Path = PROJECT_ROOT / "data" / "validated"
PROCESSED_DIR: Path = PROJECT_ROOT / "data" / "processed"

#: 分类 -> (动作, 是否允许填充, 理由)
#: 这张表就是"策略"本身。除 true_gap 之外没有可填充项，是有意为之。
POLICY: dict[str, tuple[str, bool, str]] = {
    "true_gap": (
        "interpolate", True,
        "两侧都有观测的中段空洞，理论上可安全插值",
    ),
    "not_yet_published": (
        "wait", False,
        "尾部缺口不足 3 年，更像新一期尚未发布：等官方发布，不要自己编",
    ),
    "discontinued": (
        "leave_null", False,
        "尾部缺口 >=3 年，判定制度下线：外推不是补数据，是伪造数据",
    ),
    "series_start": (
        "leave_null", False,
        "头部缺口：该序列本来就从首个观测才开始，往前补等于凭空发明历史",
    ),
}

#: 最近一次 apply_to_validated_dir 的汇总（供 CLI 打印）
_LAST_RUN: dict[str, Any] = {}


def last_run() -> dict[str, Any]:
    """返回最近一次 `:func:apply_to_validated_dir` 的汇总。"""
    return dict(_LAST_RUN)


class FillStrategyError(RuntimeError):
    """策略执行层可预期的失败。"""


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


#: Windows 文件名的非法字符（其余一律保留，**包括中文**）
_FORBIDDEN = r'[\\/:*?"<>|\x00-\x1f]+'


def _safe(name: str) -> str:
    """把 series_key 片段变成安全的文件名。

    只替换 Windows 非法字符，**保留中文**。早先的版本把所有非 ASCII 一律替换成下划线，
    结果 nbs|cpi|全国 / nbs|cpi|城市 / nbs|cpi|农村 三条序列被压成同一个文件名
    @@cpi_100_processed.json@@ 而互相覆盖（实测踩过，会静默丢数据）。
    """
    s = re.sub(_FORBIDDEN, "_", str(name)).strip(" ._")
    return s or "unknown"


# --------------------------------------------------------------------------- #
# 输入侧：缺失报告 + validated 长表
# --------------------------------------------------------------------------- #

def _load_missing_report() -> dict[str, Any]:
    """读 data/validated/missing_report.json（由 tools/scan-missing.py 产出）。"""
    p = VALIDATED_DIR / "missing_report.json"
    if not p.is_file():
        print(f"[warn] 没找到 {p}，将退化为对 validated 里的序列就地分类", file=sys.stderr)
        return {}
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[warn] 读取 {p} 失败: {exc}", file=sys.stderr)
        return {}
    return obj if isinstance(obj, dict) else {}


def _report_gaps(report: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """把 missing_report.json 拉成 {series_key: [gap, ...]}。"""
    out: dict[str, list[dict[str, Any]]] = {}
    for s in report.get("series") or []:
        if isinstance(s, dict) and s.get("series_key"):
            out[str(s["series_key"])] = list(s.get("gaps") or [])
    return out


def _scan_validated_series() -> dict[str, dict[str, Any]]:
    """扫 data/validated/ 下的长表 JSON，按 series_key 聚合（保留原始行字段）。

    识别规则与 tools/scan-missing.py 一致：顶层要有 `rows`，且行里
    `period` 与 `value` 同时存在（否则会把对比脚本的结果行误当序列）。
    """
    grouped: dict[str, dict[str, Any]] = {}
    if not VALIDATED_DIR.is_dir():
        return grouped
    for f in sorted(VALIDATED_DIR.rglob("*.json")):
        if f.name == "missing_report.json":
            continue
        try:
            obj = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(obj, dict) or not isinstance(obj.get("rows"), list):
            continue
        rows = [r for r in obj["rows"]
                if isinstance(r, dict) and "period" in r and "value" in r]
        if not rows:
            continue
        key = str(obj.get("series_key") or missing._auto_series_key(rows))
        entry = grouped.setdefault(key, {
            "file": str(f.relative_to(PROJECT_ROOT)), "rows": [],
            "expected_periods": obj.get("expected_periods"),
            "label": obj.get("name"),
        })
        entry["rows"].extend(rows)
    return grouped


# --------------------------------------------------------------------------- #
# 决策
# --------------------------------------------------------------------------- #

def decide(gap: dict[str, Any], series_key: Optional[str] = None) -> dict[str, Any]:
    """对一条缺口给出动作决策。

    :param gap: missing.classify_missing 产出的 gap 结构
                （至少含 start / end / classification；evidence 可选）。
    :param series_key: 覆盖 gap 里的 series_key（用于拼 gap_id）。
    :returns: `{"gap_id", "series_key", "start", "end", "n_missing",
              "classification", "action", "reason", "allowed_to_fill", "evidence"}`。

              注意：spec 列的 5 个字段之外，这里额外带上 start/end/n_missing/evidence，
              因为 apply_strategy 必须知道缺口覆盖哪些 period 才能给缺失行打标。
    """
    cls = str(gap.get("classification") or "")
    action, allowed, why = POLICY.get(
        cls, ("leave_null", False, f"未知分类 {cls!r}：保守不动，先人工确认"))
    sk = str(series_key or gap.get("series_key") or "?")
    start = str(gap.get("start"))
    end = str(gap.get("end"))
    evidence = str(gap.get("evidence") or "")
    reason = why + (f"；证据：{evidence}" if evidence else "")
    return {
        "gap_id": f"{sk}:{start}-{end}",
        "series_key": sk,
        "start": start,
        "end": end,
        "n_missing": gap.get("n_missing"),
        "classification": cls,
        "action": action,
        "reason": reason,
        "allowed_to_fill": allowed,
        "evidence": evidence,
    }


def apply_strategy(rows: list[dict[str, Any]],
                   decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把决策**作为元数据**贴到缺失行上，返回新的行列表。

    * `value` 永远保持原样（本轮缺失就是 null，不填）；
    * 缺失行新增 `missing_classification` / `missing_action` / `missing_evidence`；
    * 缺口网格内但 `rows` 里没有的 period，会补出 `value=None` 的行（打标，不是插值）；
    * 出现 `interpolate` 直接抛 NotImplementedError（见模块 docstring）。

    :raises NotImplementedError: decisions 里出现了 interpolate。
    """
    for d in decisions:
        if str(d.get("action")) == "interpolate":
            raise NotImplementedError(
                "本轮不执行插值：当前数据集里 true_gap 为 0 个实例。"
                f"出现 interpolate（gap_id={d.get('gap_id')}）说明数据或策略变了，"
                "必须先把插值实现与回归补上，不能让它悄悄生效。"
            )

    # 展开每个决策覆盖的 period（复用 missing 的网格工具，保证口径一致）
    covered: dict[str, dict[str, Any]] = {}
    for d in decisions:
        start = str(d.get("start"))
        kind = missing._parse_period(start)[0]
        for p in missing._enumerate_periods(start, str(d.get("end")), kind):
            covered[p] = d

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in rows:
        r2 = dict(r)
        p = str(r2.get("period"))
        seen.add(p)
        if r2.get("value") is None:
            d = covered.get(p)
            if d is None:
                r2["missing_classification"] = "undecided"
                r2["missing_action"] = "manual_review"
                r2["missing_evidence"] = "该缺失期不落在任何已决策缺口内（窗口可能不一致）"
            else:
                r2["missing_classification"] = d["classification"]
                r2["missing_action"] = d["action"]
                r2["missing_evidence"] = d.get("evidence")
        out.append(r2)

    for p, d in covered.items():
        if p in seen:
            continue
        out.append({
            "period": p,
            "value": None,
            "missing_classification": d["classification"],
            "missing_action": d["action"],
            "missing_evidence": d.get("evidence"),
        })

    out.sort(key=lambda r: missing._parse_period(str(r.get("period")))[1])
    return out


def apply_to_validated_dir(extra_series: Optional[list[dict[str, Any]]] = None) -> Path:
    """扫 validated 长表（可附带内存序列），逐序列决策并写出 processed。

    :param extra_series: `[{"series_key", "rows", "expected_periods", "label"}]`。
                         用于"已经 normalize、但当前管线还没落盘到 data/validated/"的序列
                         —— 这是管线缺口，不是本模块偷懒：本模块不抓数据，只加工。
    :returns: 输出根目录 `data/processed/`（明细见 `:func:last_run`）。
    """
    report = _load_missing_report()
    report_gaps = _report_gaps(report)

    series = _scan_validated_series()
    n_extra = 0
    for item in extra_series or []:
        key = str(item.get("series_key") or "?")
        if key in series:
            continue
        series[key] = {"file": "(in-memory)", "rows": list(item.get("rows") or []),
                       "expected_periods": item.get("expected_periods"),
                       "label": item.get("label")}
        n_extra += 1

    summary: dict[str, Any] = {
        "n_series": 0, "n_gaps": 0, "n_decisions": 0, "n_missing_rows": 0,
        "by_action": {}, "by_classification": {}, "series": [],
        "skipped_no_rows": [], "in_memory_series": n_extra,
        "missing_report_found": bool(report), "processed_dir": str(PROCESSED_DIR),
    }

    for key, item in series.items():
        rows = item["rows"]
        if not rows:
            summary["skipped_no_rows"].append(key)
            continue

        gaps = list(report_gaps.get(key) or [])
        if not gaps:
            meta: dict[str, Any] = {"series_key": key}
            if item.get("expected_periods"):
                meta["expected_periods"] = item["expected_periods"]
            gaps = missing.classify_missing(rows, meta)["gaps"]

        decisions = [decide(g, series_key=key) for g in gaps]
        processed = apply_strategy(rows, decisions)

        source, _, indicator = key.partition("|")
        # source 目录名统一小写：声明式清单的 key 是小写（nbs|...），而 normalize 行
        # 自动推出来的 key 是大写（NBS|...）。Windows 文件系统大小写不敏感看不出来，
        # 但在 Linux 上会变成两个目录，所以这里统一。
        sub = PROCESSED_DIR / _safe((source or "unknown").lower())
        sub.mkdir(parents=True, exist_ok=True)
        out = sub / f"{_safe(indicator or key)}_processed.json"
        envelope = {
            "rows": processed,
            "missing_report": {"series_key": key, "label": item.get("label"),
                               "source_file": item.get("file"), "gaps": gaps},
            "decisions": decisions,
            "processed_at": _utc_now(),
        }
        out.write_text(json.dumps(envelope, ensure_ascii=False, indent=2), encoding="utf-8")

        n_missing_rows = sum(1 for r in processed if r.get("value") is None)
        summary["n_series"] += 1
        summary["n_gaps"] += len(gaps)
        summary["n_decisions"] += len(decisions)
        summary["n_missing_rows"] += n_missing_rows
        for d in decisions:
            summary["by_action"][d["action"]] = summary["by_action"].get(d["action"], 0) + 1
            summary["by_classification"][d["classification"]] = (
                summary["by_classification"].get(d["classification"], 0) + 1)
        summary["series"].append({
            "series_key": key, "label": item.get("label"), "file": item.get("file"),
            "n_rows": len(processed), "n_gaps": len(gaps), "n_missing_rows": n_missing_rows,
            "out": str(out.relative_to(PROJECT_ROOT)),
            "decisions": [{"gap_id": d["gap_id"], "classification": d["classification"],
                           "action": d["action"], "allowed_to_fill": d["allowed_to_fill"]}
                          for d in decisions],
        })

    for key in report_gaps:
        if key not in series and key not in summary["skipped_no_rows"]:
            summary["skipped_no_rows"].append(key)

    _LAST_RUN.clear()
    _LAST_RUN.update(summary)
    return PROCESSED_DIR


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #

def _selftest() -> int:
    print("=" * 94)
    print("self-test: fill_strategy（用**当前真实数据集**的缺口做决策）")
    print("=" * 94)

    report = _load_missing_report()
    if not report:
        print("[FAIL] 找不到 data/validated/missing_report.json；请先跑 tools/scan-missing.py")
        return 1

    gaps_by_series = _report_gaps(report)
    real = [(k, g) for k, gs in gaps_by_series.items() for g in gs]
    failures: list[str] = []

    print(f"\n[1] 真实缺口 {len(real)} 条 -> 逐条决策")
    dist: dict[str, int] = {}
    for key, g in real:
        d = decide(g, series_key=key)
        dist[d["classification"]] = dist.get(d["classification"], 0) + 1
        print(f"  {d['gap_id']}")
        print(f"    classification={d['classification']}  action={d['action']}"
              f"  allowed_to_fill={d['allowed_to_fill']}")
        print(f"    reason: {d['reason'][:150]}")
        if d["classification"] in ("discontinued", "series_start"):
            if d["action"] != "leave_null" or d["allowed_to_fill"]:
                failures.append(f"{d['gap_id']}: {d['classification']} 应为 leave_null/False")
        elif d["classification"] == "not_yet_published":
            if d["action"] != "wait" or d["allowed_to_fill"]:
                failures.append(f"{d['gap_id']}: not_yet_published 应为 wait/False")
    print(f"\n  分类分布: {dist}")

    print("\n[2] apply_strategy：真实登记失业率行 + 真实决策（核对 value 一律不被改）")
    reg_key = "nbs|registered_unemployment"
    reg_decs = [decide(g, series_key=reg_key) for g in gaps_by_series.get(reg_key, [])]
    src_rows = [dict(r) for r in missing.REGISTERED_ROWS]
    out_rows = apply_strategy(src_rows, reg_decs)
    src_by = {str(r["period"]): r.get("value") for r in src_rows}
    for r in out_rows:
        p = str(r["period"])
        mark = "已打标" if r.get("value") is None and r.get("missing_classification") else "-"
        print(f"    {p}  value={r['value']!r}  missing_classification={r.get('missing_classification')!r}"
              f"  action={r.get('missing_action')!r}  {mark}")
        if p in src_by and r.get("value") != src_by[p]:
            failures.append(f"{reg_key} {p}: value 被改动了（{src_by[p]!r} -> {r.get('value')!r}）")
        if r.get("value") is None and not r.get("missing_classification"):
            failures.append(f"{reg_key} {p}: 缺失行没有 missing_classification 元数据")
    n_missing = sum(1 for r in out_rows if r.get("value") is None)
    print(f"    行数 {len(out_rows)}，其中 value=None 的 {n_missing} 行（原始 null 全部保留）")

    print("\n[3] interpolate 守卫：合成一条 true_gap 决策，应抛 NotImplementedError")
    fake = decide({"start": "2019", "end": "2019", "n_missing": 1,
                   "classification": "true_gap", "evidence": "(合成，仅为覆盖该分支)"},
                  series_key="synthetic|x")
    print(f"    decide -> action={fake['action']}  allowed_to_fill={fake['allowed_to_fill']}")
    if fake["action"] != "interpolate" or not fake["allowed_to_fill"]:
        failures.append("true_gap 的决策应为 interpolate/True")
    try:
        apply_strategy([{"period": "2019", "value": None}], [fake])
        failures.append("apply_strategy 对 interpolate 没有抛 NotImplementedError")
    except NotImplementedError as exc:
        print(f"    OK 抛出 NotImplementedError: {str(exc)[:110]}")

    print()
    print("=" * 94)
    if failures:
        for f in failures:
            print(f"  [FAIL] {f}")
        print("=" * 94)
        return 1
    print("self-test 完成 ✔（真实缺口决策正确；value 未被改动；interpolate 被拦住）")
    print("=" * 94)
    return 0


def _main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    p = argparse.ArgumentParser(prog="fill_strategy",
                                description="填补策略执行器（本轮只执行 leave_null / wait）")
    p.add_argument("--test", action="store_true", help="跑自检（用真实缺口）")
    a = p.parse_args(argv)
    if a.test:
        return _selftest()
    p.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())


