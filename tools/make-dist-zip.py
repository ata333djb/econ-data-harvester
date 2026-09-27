#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""make-dist-zip.py —— 把 `dist/` 里的分发内容打成 zip（**正斜杠、可复现**）。

为什么不用 `Compress-Archive`
-----------------------------
`Compress-Archive` 在 Windows 上把路径写成**反斜杠**（`tools\\edh.py`）。
ZIP 规范（APPNOTE 4.4.17.1）要求路径分隔符是**正斜杠**；用反斜杠会带来两个后果：

* Windows 资源管理器能解（它按 Windows 惯例容错），所以本地测试**看不出来**；
* 但 Python `zipfile`、Linux/macOS 的 `unzip` 会把整个 `embedded-python\\python.exe`
  当成**一个文件名**，在当前目录里生成一个叫这个名字的文件，而不是建目录树 ——
  用户拿到的包在非资源管理器环境下直接废掉。

所以这里用 `zipfile` 自己写，路径一律 `posixpath` 风格。

打包范围是**白名单**，不是"dist/ 下所有东西"：`dist/.build/`（get-pip.py、
patched-run.py、下载缓存）与 `dist/.tmp/` 都是构建脚手架，不该进用户包。
白名单还有个好处：即使有人在 dist/ 里临时放了别的东西，也不会被误打进去。

用法
----
    .\\.venv\\Scripts\\python.exe tools\\make-dist-zip.py
    .\\.venv\\Scripts\\python.exe tools\\make-dist-zip.py --version 0.3
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path
from typing import Optional, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DIST = PROJECT_ROOT / "dist"

#: 进包的东西（相对 dist/）。顺序即写入顺序，目录在前便于阅读清单。
DIST_ITEMS: tuple[str, ...] = (
    "edh.bat",
    "README.md",
    "LICENSE",
    "python",
    "tools",
    "embedded-python",
    "examples",
)

#: 明确排除的目录名（构建脚手架 / 缓存），双保险
EXCLUDE_DIRS = {"__pycache__", ".build", ".tmp"}


def _iter_files(item: Path) -> list[Path]:
    """展开一个白名单项成文件列表（目录递归，按路径排序保证可复现）。"""
    if item.is_file():
        return [item]
    out: list[Path] = []
    for p in sorted(item.rglob("*")):
        if p.is_dir():
            continue
        if any(part in EXCLUDE_DIRS for part in p.parts):
            continue
        out.append(p)
    return out


def build(version: str) -> int:
    """打 zip，返回退出码。"""
    zip_path = DIST / f"econ-data-harvester-v{version}.zip"
    files: list[tuple[Path, str]] = []
    for name in DIST_ITEMS:
        item = DIST / name
        if not item.exists():
            print(f"[FAIL] dist/{name} 不存在 —— 分发内容不完整", file=sys.stderr)
            return 2
        for f in _iter_files(item):
            arc = f.relative_to(DIST).as_posix()   # 关键：正斜杠
            files.append((f, arc))

    if zip_path.exists():
        zip_path.unlink()

    total = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for f, arc in files:
            zf.write(f, arcname=arc)
            total += f.stat().st_size

    size = zip_path.stat().st_size
    print(f"写入 {zip_path.relative_to(PROJECT_ROOT).as_posix()}")
    print(f"  条目数   {len(files)}")
    print(f"  原始大小 {total:,} 字节 ({total / 1048576:.2f} MB)")
    print(f"  zip 大小 {size:,} 字节 ({size / 1048576:.2f} MB)")

    # 自检：解出来的名字必须是正斜杠，且不含排除项
    bad = [a for _, a in files if "\\" in a]
    if bad:
        print(f"[FAIL] 有条目用了反斜杠: {bad[:3]}", file=sys.stderr)
        return 1
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
    leaked = [n for n in names
              if any(part in EXCLUDE_DIRS for part in n.split("/"))
              or n.startswith("dist/")]
    if leaked:
        print(f"[FAIL] 包里混入了不该有的东西: {leaked[:5]}", file=sys.stderr)
        return 1
    print(f"  自检通过：{len(names)} 个条目全部正斜杠、无构建脚手架")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    parser = argparse.ArgumentParser(prog="make-dist-zip", description="打包 dist/ 为分发 zip")
    parser.add_argument("--version", default="0.2", help="版本号（默认 0.2）")
    args = parser.parse_args(list(argv) if argv is not None else None)
    return build(args.version)


if __name__ == "__main__":
    raise SystemExit(main())
