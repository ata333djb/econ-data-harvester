#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""materialize-validated.py —— 把声明式清单落盘到 data/validated/，让 processed 能脱网。

为什么需要它
------------
此前 `fill_strategy.apply_to_validated_dir()` 只能加工 data/validated/ 里**已落盘**的长表，
而那里只有一份 normalize 自检产物；其余 9 条序列虽然已 normalize 过，却从没写进 validated。
于是 tools/run-fill-strategy.py 只能自己在内存里再造一遍清单（还得联网现取）。
本脚本把这一环补上：**拉数 -> normalize -> 落盘 validated**，此后 processed 纯离线可算。

做法
----
1. 按路径 importlib 加载 tools/scan-missing.py，复用它的 build_declared()（**不复制**清单）。
2. 对每条序列调 `normalize.write_validated()` 落盘到 data/validated/<source>/<name>.json
   （行序列化与 columns 列都交给 normalize 层，避免两处实现漂移）。
3. 落盘后在信封上**追加两个字段** `series_key` 与 `expected_periods` ——
   write_validated 的固定信封里没有它们，而 `fill_strategy._scan_validated_series()`
   会优先读这两个字段来决定"按哪个 key 聚合"和"期望网格是什么"。没有它们，
   重扫时只能退回"从观测范围自动推网格"，series_start 这类头部缺口就判不出来。

关于 expected_periods
---------------------
取声明式清单里的窗口（本来就是 grid 的 min..max，例如 2015-2024 / 2015-2025）。

用法
----
    .\\.venv\\Scripts\\python.exe tools\\materialize-validated.py
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "python"))

from econ_core import normalize  # noqa: E402

SCAN_MISSING = PROJECT_ROOT / "tools" / "scan-missing.py"
VALIDATED_DIR = PROJECT_ROOT / "data" / "validated"

#: Windows 非法文件名字符（保留中文）
_FORBIDDEN = r'[\\/:*?"<>|\x00-\x1f]+'


def _safe(name: str) -> str:
    """series_key -> 安全文件名（只替换 Windows 非法字符，保留中文）。"""
    return re.sub(_FORBIDDEN, "_", str(name)).strip(" ._") or "unknown"


def load_declared_series() -> list[dict[str, Any]]:
    """按路径加载 tools/scan-missing.py 并复用它的声明式清单。"""
    spec = importlib.util.spec_from_file_location("scan_missing_tool", SCAN_MISSING)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 {SCAN_MISSING}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    out: list[dict[str, Any]] = []
    for item in mod.build_declared():
        meta = item.get("meta") or {}
        out.append({
            "series_key": str(meta.get("series_key")),
            "rows": item["rows"],
            "expected_periods": [str(x) for x in (meta.get("expected_periods") or [])],
            "label": item.get("label"),
        })
    return out


def _grid(rows: list[dict[str, Any]], expected: list[str]) -> list[str]:
    """期望网格：优先用声明窗口；没有就从观测 period 推 min..max（含端点）。"""
    if expected:
        return expected
    ps = sorted({str(r["period"]) for r in rows if r.get("period")})
    return ps


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    print("=" * 100)
    print("materialize-validated: 声明式清单 -> data/validated/")
    print("=" * 100)

    declared = load_declared_series()
    print(f"\n声明式序列: {len(declared)} 条")

    written: list[tuple[str, Path, int]] = []
    problems: list[str] = []

    for item in declared:
        key = item["series_key"]
        rows = item["rows"]
        if not rows:
            problems.append(f"{key}: 行数据为空，跳过")
            continue

        source = (key.split("|")[0] or "unknown").lower()
        out_dir = VALIDATED_DIR / _safe(source)
        name = _safe(key.replace("|", "_"))

        # 1) 让 normalize 层负责行序列化与 columns（单一事实来源）
        path = normalize.write_validated(rows, name, out_dir=out_dir)

        # 2) 追加 series_key / expected_periods（write_validated 的固定信封里没有这两个）
        obj = json.loads(path.read_text(encoding="utf-8"))
        obj["series_key"] = key
        obj["expected_periods"] = _grid(rows, item["expected_periods"])
        path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")

        size = path.stat().st_size
        written.append((key, path, size))
        print(f"  [ok] {key}")
        print(f"       -> {path.relative_to(PROJECT_ROOT)}  ({size:,} 字节, rows={len(rows)}, "
              f"grid={obj['expected_periods'][0]}..{obj['expected_periods'][-1]})")

        # 3) 立刻回读校验：两个字段必须真的在文件里
        back = json.loads(path.read_text(encoding="utf-8"))
        if back.get("series_key") != key or not back.get("expected_periods"):
            problems.append(f"{key}: 回读校验失败（series_key/expected_periods 未落盘）")

    print()
    print("-" * 100)
    print(f"落盘序列数: {len(written)}")
    total = sum(s for _, _, s in written)
    print(f"总字节数  : {total:,}")
    print(f"输出根目录: {VALIDATED_DIR}")

    if problems:
        print()
        for p in problems:
            print(f"  [FAIL] {p}", file=sys.stderr)
        return 1
    print("  [PASS] 声明式清单已全部落盘，fill_strategy 可直接扫 validated（无需再传 extra_series）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
