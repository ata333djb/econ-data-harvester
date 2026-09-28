#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cross_check_store.py —— 写 `data/validated/cross_check/` 产物，并**只保留同族最新一份**。

为什么需要它（修的是门禁的一个日期相关缺陷）
--------------------------------------------
四个对比脚本把结果写成**带日期戳**的文件（`gdp_3way_20260928.json`），而且**从不清理旧日期**。
于是 `cross_check/` 每跨一天就多一整套；而 `arbiter --test`（门禁**第 9 项**）读的就是这个目录，
它断言"恰好 7 条记录" —— 而写这套文件的 `compare-*.py` 是门禁**第 12–16 项**，**排在第 9 项之后**。

两件事叠在一起，结果是：

* 一天只跑一次门禁 -> 第 9 项看到上次留下的 7 条 -> PASS，**看不出问题**；
* 同一天跑第二次   -> 目录里有两天各一套 -> 13 条 -> **必 FAIL**（`记录数期望 7，实际 13`）。

这是**门禁本身不可重复**，比任何单条断言错误都严重：第二个跑门禁的人会以为代码坏了。
修法保留日期戳（历史仍然看得到），只是在写完新文件后**删掉同族的旧日期文件** ——
文件名里的日期就是"最新一份"的判据。

为什么单独一个模块
------------------
四个脚本都要做同一件事。写在各自文件里 = 四份会漂移的副本（本项目对"两处实现漂移"有明确教训，
见 `materialize-validated.py` 刻意复用 `normalize.write_validated` 的理由）。

安全约束（**别放宽**）
----------------------
`family` 必须由调用方显式给出，本模块**不猜**；清理范围严格限定为
`<family>_<8 位数字>.json` 且**父目录必须与本次输出一致**。
所以它永远不会删掉别的命名的文件、别的目录的文件，或本次刚写的那一份。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional, Sequence

__all__ = ["save_result", "prune_family", "family_files"]


def _family_pattern(family: str) -> re.Pattern[str]:
    """同族文件名的精确形状：``<family>_<8 位数字>.json``。

    用正则而不是宽 glob（`gdp_3way_*.json`），是为了**绝不误删**
    手写的 `gdp_3way_notes.json` 之类文件 —— 那些不符合"日期戳"的形状。
    """
    return re.compile(rf"^{re.escape(family)}_\d{{8}}\.json$")


def family_files(results_dir: Path, family: str) -> list[Path]:
    """列出该族**按日期升序**的全部产物文件。"""
    rx = _family_pattern(family)
    d = Path(results_dir)
    if not d.is_dir():
        return []
    return sorted((p for p in d.iterdir() if p.is_file() and rx.match(p.name)),
                  key=lambda p: p.name)


def prune_family(results_dir: Path, family: str, keep: Path) -> list[Path]:
    """删掉该族里除 `keep` 之外的旧文件，返回被删列表。

    :param keep: 要保留的那一份（通常是刚写的）。
    """
    removed: list[Path] = []
    keep = Path(keep).resolve()
    for p in family_files(results_dir, family):
        if p.resolve() == keep:
            continue
        # 双保险：父目录必须一致（family_files 已限定，这里防未来改动放宽）
        if p.parent.resolve() != Path(results_dir).resolve():
            continue
        p.unlink()
        removed.append(p)
    return removed


def save_result(payload: Any, out: Path, family: str,
                results_dir: Optional[Path] = None) -> list[Path]:
    """把 `payload` 写成 `out`，然后**删掉同族的旧日期文件**。

    :param payload: 要写的内容（任意可 JSON 序列化的对象）。
    :param out: 本次输出路径（形如 ``.../cross_check/gdp_3way_20260928.json``）。
    :param family: 同族前缀（形如 ``"gdp_3way"``），**显式给，本模块不猜**。
    :param results_dir: 同族文件所在目录；缺省取 `out.parent`。
    :returns: 被删掉的旧文件列表（按名字升序）。
    :raises ValueError: `out` 的文件名不符合 `<family>_<8 位数字>.json`。
    """
    out = Path(out)
    if not _family_pattern(family).match(out.name):
        raise ValueError(
            f"输出文件名 {out.name!r} 不符合族 {family!r} 的形状 "
            f"（应为 {family}_<8位数字>.json）—— 清理范围靠它判定，不能含糊")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    removed = prune_family(results_dir or out.parent, family, out)

    # 收尾自检：该族必须**恰好剩一份**（就是刚写的这份）
    left = family_files(results_dir or out.parent, family)
    if [p.resolve() for p in left] != [out.resolve()]:
        raise RuntimeError(
            f"{family}: 清理后同族文件不止一份 {[p.name for p in left]} —— "
            f"门禁第 9 项（arbiter）会因此误判记录数")
    return removed


def _main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        prog="econ_core.cross_check_store",
        description="cross_check 产物写入 + 同族旧文件清理（被 compare-*.py 使用）")
    ap.add_argument("--list", metavar="FAMILY", help="列出某族的当前文件（不写不删）")
    ap.add_argument("--dir", default=None, help="结果目录（默认 data/validated/cross_check）")
    args = ap.parse_args(list(argv) if argv is not None else None)
    if not args.list:
        ap.print_help()
        return 0
    d = Path(args.dir) if args.dir else (
        Path(__file__).resolve().parents[2] / "data" / "validated" / "cross_check")
    files = family_files(d, args.list)
    print(f"{d} / 族 {args.list}: {len(files)} 个")
    for f in files:
        print(f"  {f.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
