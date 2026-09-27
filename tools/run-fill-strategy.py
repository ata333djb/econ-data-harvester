#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""run-fill-strategy.py —— 一键把 validated 加工成 processed（本轮**零填充**）。

它做什么
--------
1. 记录 data/raw/_http_cache/ 的**快照**（文件名+大小+mtime）。
2. 调 `econ_core.fill_strategy.apply_to_validated_dir()`：纯扫 data/validated/ 下的长表，
   逐条缺口决策，写出 data/processed/<source>/<indicator>_processed.json。
3. 再取一次快照，**对比证明本次运行没有发出任何 HTTP 请求**（http_client 每次请求都会
   在 raw 缓存里落一个新文件或改写已有文件；快照零变化 == 零请求）。
4. 打印逐序列缺口与决策、动作分布，并做两条端到端校验：
   * 没有任何决策允许填充（当前数据集 true_gap=0，allowed_to_fill 全为 False）；
   * 每个 processed 文件里 `value=null` 的行都带 missing_classification 元数据。

为什么现在能脱网
----------------
上一轮它得自己拿 tools/scan-missing.py 的清单现取数据（所以是网络密集型、给了 420s）。
现在 tools/materialize-validated.py 已经把 9 条声明式序列落盘到 data/validated/，
本脚本只需要读盘。

用法
----
    .\\.venv\\Scripts\\python.exe tools\\run-fill-strategy.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "python"))

from econ_core import fill_strategy  # noqa: E402

RAW_CACHE = PROJECT_ROOT / "data" / "raw" / "_http_cache"


def snapshot_raw_cache() -> dict[str, tuple[int, int]]:
    """raw 缓存快照：{文件名: (大小, mtime_ns)}。用于证明"本次没联网"。"""
    out: dict[str, tuple[int, int]] = {}
    if RAW_CACHE.is_dir():
        for p in RAW_CACHE.iterdir():
            if p.is_file():
                st = p.stat()
                out[p.name] = (st.st_size, st.st_mtime_ns)
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

    _section("[1] 执行前：raw 缓存快照")
    before = snapshot_raw_cache()
    print(f"  {RAW_CACHE}")
    print(f"  既有存档文件数: {len(before)}")

    _section("[2] 执行策略（只扫 data/validated/，本轮 leave_null / wait 零填充）")
    out_dir = fill_strategy.apply_to_validated_dir()
    s = fill_strategy.last_run()

    print(f"  missing_report.json 可用: {s['missing_report_found']}")
    print(f"  处理的序列数: {s['n_series']}")
    print(f"  缺口段总数  : {s['n_gaps']}")
    print(f"  决策数      : {s['n_decisions']}")
    print(f"  被打标的缺失行: {s['n_missing_rows']}")

    _section("[3] 逐序列：缺口与决策")
    for item in s["series"]:
        print(f"  {item['series_key']}")
        print(f"    file={item.get('file')}")
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

    _section("[5] 脱网自证：执行前后 raw 缓存快照对比")
    after = snapshot_raw_cache()
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(k for k in (set(before) & set(after)) if before[k] != after[k])
    print(f"  新增存档: {len(added)}  改写存档: {len(changed)}  删除: {len(removed)}")
    for k in added[:5]:
        print(f"      + {k}")
    for k in changed[:5]:
        print(f"      ~ {k}")
    offline = not added and not changed and not removed
    print(f"  结论: {'本次运行零 HTTP 请求（脱网可用）' if offline else '发生了网络访问！'}")

    _section("[6] 端到端校验（零填充 + 元数据完整）")
    problems: list[str] = []
    if not offline:
        problems.append("本次运行改动了 raw 缓存，说明仍在联网")

    n_fill_allowed = sum(1 for item in s["series"] for d in item["decisions"]
                         if d["allowed_to_fill"])
    print(f"  允许填充的决策数: {n_fill_allowed}（当前数据集应为 0）")
    if n_fill_allowed:
        problems.append(f"有 {n_fill_allowed} 条决策允许填充，但本轮不应执行任何填充")

    n_rows = 0
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
            n_rows += 1
            if r.get("value") is None:
                n_null += 1
                if not r.get("missing_classification"):
                    problems.append(
                        f"{item['out']} period={r.get('period')}: null 行缺 missing_classification")
        if len(obj.get("decisions") or []) != item["n_gaps"]:
            problems.append(f"{item['out']}: decisions 数与缺口数不一致")
    print(f"  复读 processed 行数: {n_rows}，其中 value=None {n_null} 行（均带分类元数据）")

    if s["skipped_no_rows"]:
        print(f"  ⚠ 报告里存在但没有行数据的序列（本轮跳过）: {len(s['skipped_no_rows'])} 条")
        for k in s["skipped_no_rows"]:
            print(f"      {k}")

    _section("[7] 输出目录")
    print(f"  {out_dir}")

    if problems:
        print()
        for p in problems:
            print(f"  [FAIL] {p}", file=sys.stderr)
        return 1
    print("  [PASS] validated -> processed 已打通：零填充、元数据完整、本次未联网")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
