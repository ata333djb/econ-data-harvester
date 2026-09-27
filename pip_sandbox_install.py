#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""沙箱环境下的 pip 安装包装器。

问题
----
本沙箱的文件系统层会把 `os.mkdir(path, 0o700)` 的 POSIX mode 当作**禁止性 ACL** 应用，
导致 `tempfile.mkdtemp()` 创建出来的目录**连创建进程自己都无法写入或列目录**：

    >>> d = tempfile.mkdtemp()
    >>> open(os.path.join(d, "x"), "w")
    PermissionError: [Errno 13] Permission denied

而 `pip` 内部大量使用 `tempfile.mkdtemp()`（``pip-unpack-*`` / ``pip-build-tracker-*``），
于是安装必然失败：

    ERROR: Could not install packages due to an OSError:
    [Errno 13] Permission denied: '...\\pip-unpack-xxx\\pandas-...whl.metadata'

同一个根因还解释了一个残留目录 `tmpgb9sfm6l/`（`tmp` + 8 位随机字符 = mkdtemp 命名），
它同样无法访问，且需要管理员权限才能删除。

对策
----
在调用 pip 之前，把 `tempfile.mkdtemp()` / `tempfile.mkstemp()` 替换成使用默认权限
（`os.mkdir` / `os.open` 不带 0o700/0o600）的实现，其余行为保持一致。

用法
----
    .\\.venv\\Scripts\\python.exe pip_sandbox_install.py install pandas pyarrow
    .\\.venv\\Scripts\\python.exe pip_sandbox_install.py list

参数原样透传给 pip。
"""

from __future__ import annotations

import os
import sys
import tempfile
from typing import Optional, Sequence

__all__ = ["patch_tempfile", "main"]


def patch_tempfile() -> None:
    """把 mkdtemp/mkstemp 换成不使用限制性 mode 的实现（幂等）。"""
    if getattr(tempfile, "_dsh_patched", False):
        return

    def mkdtemp(suffix: Optional[str] = None, prefix: Optional[str] = None,
                dir: Optional[str] = None) -> str:
        prefix, suffix, d, output_type = tempfile._sanitize_params(prefix, suffix, dir)
        for name in tempfile._get_candidate_names():
            path = os.path.join(d, (prefix or "") + name + (suffix or ""))
            try:
                os.mkdir(path)          # 关键：不传 0o700
            except FileExistsError:
                continue
            try:
                os.chmod(path, 0o777)   # 再显式放宽，抵消可能被套上的限制
            except OSError:
                pass
            return os.fsdecode(path) if output_type is str else os.fsencode(path)
        raise FileExistsError("无法创建临时目录（候选名耗尽）")

    def mkstemp(suffix: Optional[str] = None, prefix: Optional[str] = None,
                dir: Optional[str] = None, text: bool = False) -> tuple[int, str]:
        prefix, suffix, d, output_type = tempfile._sanitize_params(prefix, suffix, dir)
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
        if text:
            flags |= getattr(os, "O_TEXT", 0)
        for name in tempfile._get_candidate_names():
            path = os.path.join(d, (prefix or "") + name + (suffix or ""))
            try:
                fd = os.open(path, flags)   # 默认 0o777，不传 0o600
            except FileExistsError:
                continue
            try:
                os.chmod(path, 0o666)
            except OSError:
                pass
            return fd, os.fsdecode(path) if output_type is str else os.fsencode(path)
        raise FileExistsError("无法创建临时文件（候选名耗尽）")

    tempfile.mkdtemp = mkdtemp          # type: ignore[assignment]
    tempfile.mkstemp = mkstemp          # type: ignore[assignment]
    tempfile._dsh_patched = True        # type: ignore[attr-defined]

    # 自检：新建的临时目录必须可写
    probe = tempfile.mkdtemp()
    with open(os.path.join(probe, "probe.txt"), "w") as fh:
        fh.write("ok")
    print(f"[patch] tempfile.mkdtemp/mkstemp 已替换，自检目录可写: {probe}", file=sys.stderr)


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    args = list(argv if argv is not None else sys.argv[1:])
    patch_tempfile()

    try:
        from pip._internal.cli.main import main as pip_main
    except ImportError:
        print("无法导入 pip，请确认使用 venv 的 Python 运行本脚本。", file=sys.stderr)
        return 1
    return int(pip_main(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
