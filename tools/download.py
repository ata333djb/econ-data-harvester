#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""download.py —— 用项目 venv 的 OpenSSL 栈下载一个文件。

为什么需要这个脚本
------------------
本机 Schannel 凭证库不可用（PROJECT_STATE §2.1），所以**任何走 .NET / WinHTTP 的下载都会 TLS 失败** ——
`Invoke-WebRequest`、`curl.exe`、`bitsadmin` 全废，报"基础连接已经关闭: 接收时发生错误"。
只有 Python 自带的 OpenSSL 栈能通。实测就是：
`Invoke-WebRequest python.org/...zip` 失败，本脚本同一个 URL 成功。

打包（`dist/embedded-python`）要用它下载 Python embeddable 包与 get-pip.py，
所以单独留一个脚本，而不是每次在命令行里手搓 urllib。

用法
----
    .\\.venv\\Scripts\\python.exe tools\\download.py <url> <输出路径> [--sha256 <期望值>]

退出码：0 成功；1 下载失败；2 校验不符。
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional, Sequence

#: 给 python.org / bootstrap.pypa.io 用的朴素 UA。
#: 沿用项目惯例（§2.12）：**先试朴素 UA**，不要伪装成浏览器。
UA = "python-urllib/3.12"


def download(url: str, dest: Path, expect_sha256: Optional[str] = None) -> int:
    """下载 `url` 到 `dest`，边下边算 sha256。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    print(f"GET {url}")
    print(f"  UA: {UA}")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            total = resp.headers.get("Content-Length")
            print(f"  HTTP {resp.status}  Content-Length={total}")
            h = hashlib.sha256()
            n = 0
            with dest.open("wb") as fh:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    fh.write(chunk)
                    h.update(chunk)
                    n += len(chunk)
    except (urllib.error.URLError, OSError) as exc:
        print(f"  失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    digest = h.hexdigest()
    print(f"  写入 {dest}  {n} 字节")
    print(f"  sha256 = {digest}")
    if expect_sha256 and digest.lower() != expect_sha256.lower():
        print(f"  [FAIL] 期望 sha256 {expect_sha256}", file=sys.stderr)
        return 2
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    parser = argparse.ArgumentParser(prog="download", description="用 OpenSSL 栈下载文件")
    parser.add_argument("url")
    parser.add_argument("dest")
    parser.add_argument("--sha256", default=None, help="期望的 sha256（可选，用于校验）")
    args = parser.parse_args(list(argv) if argv is not None else None)
    return download(args.url, Path(args.dest), args.sha256)


if __name__ == "__main__":
    raise SystemExit(main())
