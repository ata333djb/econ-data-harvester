#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""run-fill-strategy.py —— 一键把 validated 加工成 processed（本轮**零填充**）。

它做什么
--------
1. 用 tools/scan-missing.py 里**同一份**声明式清单构造行数据（importlib 按路径加载，
   不复制那份清单，避免两处漂移）。这些序列目前只存在于内存 / parsed 层，
   还没落盘到 data/validated/ —— 所以以 extra_series 形式送进执行器。
2. 调 `econ_core.fill_strategy.apply_to_validated_dir(extra_series=...)`：
   扫 data/validated/ 的长表 + 上述内存序列，逐条缺口决策，写出
   data/processed/<source>/<indicator>_processed.json。
3. 打印每条序列的缺口与决策、动作分布，并做两条端到端校验：
   * **没有任何决策允许填充**（当前数据集 true_gap=0，allowed_to_fill 全为 False）；
   * 每个 processed 文件里 `value=null` 的行**都带** missing_classification 元数据，
     且非空值的行数与原序列一致 —— 即"零填充"确实是零。

为什么"跑一次要联网"
--------------------
因为本轮要处理的序列来自 NBS / World Bank / IMF，行数据得现取（管线还没把它们落盘）。
这也是门禁里给它 timeout_s=420 的原因。

用法
----
    .\\.venv\\Scripts\\python.exe tools\\run-fill-strategy.py
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "python"))

from econ_core import fill_strategy  # noqa: E402

SCAN_MISSING = PROJECT_ROOT / "tools" / "scan-missing.py"


def load_declared_series() -> list[dict[str, Any]]:
    """按路径加载 tools/scan-missing.py 并复用它的声明式清单（含 expected_periods）。"""
    spec = importlib.util.spec_from_file_location("scan_missing_tool", SCAN_MISSING)
    if spec is None or spec.loader is None:
        raise fill_strategy.FillStrategyError(f"无法加载 {SCAN_MISSING}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    declared = mod.build_declared()
    out: list[dict[str, Any]] = []
    for item in declared:
        meta = item.get("meta") or {}
        out.append({
            "series_key": meta.get("series_key"),
            "rows": item["rows"],
            "expected_periods": meta.get("expected_periods"),
            "label": item.get("label"),
        })
    return out


def _section(title: str) -> None:
    print()
    print("=" * 100)
    print(title)
    print("=" * 100)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    _section("[1] 构造声明式清单（复用 scan-missing 的清单，不复制）")
    declared = load_declared_series()
    print(f"  声明式序列: {len(declared)} 条")
    for it in declared:
        print(f"    {it['series_key']}   rows={len(it['rows'])}"
              f"   window={it['expected_periods'][0] if it['expected_periods'] else '?'}"
              f"..{it['expected_periods'][-1] if it['expected_periods'] else '?'}")

    _section("[2] 执行策略（本轮只 leave_null / wait，零填充）")
    out_dir = fill_strategy.apply_to_validated_dir(extra_series=declared)
    s = fill_strategy.last_run()

    print(f"  missing_report.json 可用: {s['missing_report_found']}")
    print(f"  内存序列（未落盘到 validated）: {s['in_memory_series']} 条")
    print(f"  处理的序列数: {s['n_series']}")
    print(f"  缺口段总数  : {s['n_gaps']}")
    print(f"  决策数      : {s['n_decisions']}")
    print(f"  被打标的缺失行: {s['n_missing_rows']}")

    _section("[3] 逐序列：缺口与决策")
    for item in s["series"]:
        print(f"  {item['series_key']}")
        print(f"    label={item.get('label')}")
        print(f"    rows={item['n_rows']}  缺口={item['n_gaps']}  缺失行={item['n_missing_rows']}")
        print(f"    -> {item['out']}")
        for d in item["decisions"]:
            print(f"      {d['gap_id']}  classification={d['classification']}"
                  f"  action={d['action']}  allowed_to_fill={d['allowed_to_fill']}")
        if not item["decisions"]:
            print("      （无缺口）")

    _section("[4] 动作分布")
    for act in ("leave_null", "wait", "interpolate"):
        print(f"  {act:<12} {s['by_action'].get(act, 0)}")
    print(f"  分类分布: {s['by_classification']}")

    _section("[5] 端到端校验（零填充 + 元数据完整）")
    problems: list[str] = []
    n_fill_allowed = sum(1 for item in s["series"] for d in item["decisions"]
                         if d["allowed_to_fill"])
    print(f"  允许填充的决策数: {n_fill_allowed}（当前数据集应为 0）")
    if n_fill_allowed:
        problems.append(f"有 {n_fill_allowed} 条决策允许填充，但本轮不应执行任何填充")

    n_checked = 0
    n_null = 0
    for item in s["series"]:
        f = PROJECT_ROOT / item["out"]
        try:
            obj = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            problems.append(f"{item['out']}: 读取失败 {exc}")
            continue
        rows = obj.get("rows") or []
        for r in rows:
            n_checked += 1
            if r.get("value") is None:
                n_null += 1
                if not r.get("missing_classification"):
                    problems.append(f"{item['out']} period={r.get('period')}: null 行缺 missing_classification")
        if len(obj.get("decisions") or []) != item["n_gaps"]:
            problems.append(f"{item['out']}: decisions 数与缺口数不一致")
    print(f"  复读 processed 行数: {n_checked}，其中 value=None {n_null} 行（均带分类元数据）")

    if s["skipped_no_rows"]:
        print(f"  ⚠ 报告里存在但没有行数据的序列（本轮跳过）: {len(s['skipped_no_rows'])} 条")
        for k in s["skipped_no_rows"]:
            print(f"      {k}")

    _section("[6] 输出目录")
    print(f"  {out_dir}")

    if problems:
        print()
        for p in problems:
            print(f"  [FAIL] {p}", file=sys.stderr)
        return 1
    print("  [PASS] validated -> processed 已打通：零填充，元数据完整")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
