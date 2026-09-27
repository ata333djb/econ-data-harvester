#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""run-all-checks.py —— 回归门禁（薄壳总入口）。

定位
----
按固定顺序跑完所有**独立**检查，任何一个失败即整体失败。
它自己不做任何检查逻辑，只负责：拼 argv、跑子进程、计时、决定停不停。
加一条检查 = 往 `CHECKS` 里加一行。

刻意不做的事（保持薄）
----------------------
* 不并行（失败即停，并行会让"停在哪一步"失去意义）
* 不缓存（每次都真跑）
* 不生成 HTML / JSON 报告（要报告看 stdout）
* 不做参数解析（就是全跑一遍；想单跑某一项，直接调那个脚本）

当前检查清单
------------
1. node tools/smoke-nbs-adapter.mjs                    （插件 argv 拼装，桩）
2. node tools/smoke-worldbank-adapter.mjs              （插件 argv 拼装，桩）
3. python tools/check-cli-envelope.py                  （真跑 CLI 的信封契约）
4. python -m econ_core.normalize --test                （规范化层自检）
5. python -m econ_core.cross_validation --test         （交叉验证自检）
6. python tools/compare-gdp.py                         （NBS vs World Bank 端到端）

约定
----
* Python 检查一律用项目 venv 的绝对路径解释器，cwd = 项目根，PYTHONPATH = python。
* Node 检查用 `shutil.which("node")` 解析（PATH 里的 node）。
* 每项 120s 超时，超时按失败处理。
* 失败时打印该项**完整** stdout + stderr，然后立即停止，不跑后面的。

用法
----
    .\\.venv\\Scripts\\python.exe tools\\run-all-checks.py
退出码：全部通过 0；否则 1。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import NamedTuple, Optional

PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]

#: 项目 venv 解释器（绝对路径，与插件/契约测试同一约定）
PYTHON: str = r"D:\universe\econ-data-harvester\.venv\Scripts\python.exe"

#: 让 `-m econ_core.*` 可解析
PYTHONPATH: str = str(PROJECT_ROOT / "python")

#: 单项超时（秒）
TIMEOUT_S: int = 180

#: 名称列 + 点号的合计宽度（对齐用）
DOTS_WIDTH: int = 33

#: 汇总分隔线宽度
RULE_WIDTH: int = 40


class Check(NamedTuple):
    """一条独立检查。"""

    name: str               # 显示名（也用于对齐）
    kind: str               # "python" | "node"
    args: list[str]         # python: ["-m", mod, ...] 或 ["tools/x.py"]；node: ["tools/x.mjs"]


class Result(NamedTuple):
    """一次检查的执行结果。"""

    ok: bool
    exit_code: Optional[int]
    elapsed: float
    stdout: str
    stderr: str
    note: str               # 非空表示"没跑成"的原因（超时 / node 缺失 / OSError）


CHECKS: list[Check] = [
    Check("smoke-nbs-adapter", "node", ["tools/smoke-nbs-adapter.mjs"]),
    Check("smoke-worldbank-adapter", "node", ["tools/smoke-worldbank-adapter.mjs"]),
    Check("check-cli-envelope", "python", ["tools/check-cli-envelope.py"]),
    Check("normalize --test", "python", ["-m", "econ_core.normalize", "--test"]),
    Check("cross_validation --test", "python",
          ["-m", "econ_core.cross_validation", "--test"]),
    Check("compare-gdp", "python", ["tools/compare-gdp.py"]),
]

# --------------------------------------------------------------------------- #
# argv 组装与执行
# --------------------------------------------------------------------------- #

def build_argv(check: Check, node_path: Optional[str]) -> tuple[Optional[list[str]], str]:
    """把 Check 翻译成可直接 spawn 的 argv。

    :returns: (argv, "")；无法组装时返回 (None, 失败原因)。
    """
    if check.kind == "node":
        if not node_path:
            return None, 'shutil.which("node") 返回 None（PATH 里没有 node）'
        return [node_path, *check.args], ""
    return [PYTHON, *check.args], ""


def _decode(data: object) -> str:
    """subprocess 的 bytes 输出 -> str（UTF-8，坏字节替换）。"""
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", "replace")
    return str(data)


def run_check(argv: list[str]) -> Result:
    """跑一条检查并返回 Result。**不抛异常**：超时/OSError 都落成失败结果。"""
    env = dict(os.environ)
    env["PYTHONPATH"] = PYTHONPATH
    env["PYTHONIOENCODING"] = "utf-8"
    started = time.monotonic()
    try:
        proc = subprocess.run(
            argv,
            cwd=str(PROJECT_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        return Result(False, None, time.monotonic() - started,
                      _decode(exc.stdout), _decode(exc.stderr),
                      f"超时 >{TIMEOUT_S}s")
    except OSError as exc:
        return Result(False, None, time.monotonic() - started, "", "",
                      f"{type(exc).__name__}: {exc}")
    return Result(proc.returncode == 0, proc.returncode,
                  time.monotonic() - started,
                  _decode(proc.stdout), _decode(proc.stderr), "")


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #

def verdict_text(result: Result) -> str:
    """行尾判定文本：PASS (0.8s) / FAIL (exit=1, 0.4s) / FAIL (超时 >120s, ...)。"""
    if result.ok:
        return f"PASS ({result.elapsed:.1f}s)"
    if result.note:
        return f"FAIL ({result.note}, {result.elapsed:.1f}s)"
    return f"FAIL (exit={result.exit_code}, {result.elapsed:.1f}s)"


def check_line(index: int, total: int, name: str, verdict: str) -> str:
    """形如「[1/6] smoke-nbs-adapter ........ PASS (0.8s)」。"""
    dots = "." * max(2, DOTS_WIDTH - len(name))
    return f"[{index}/{total}] {name} {dots} {verdict}"


def _section(title: str, body: str) -> None:
    """打印一个完整输出段（空则显示 (空)，避免看不出有没有输出）。"""
    print(f"----- {title} -----")
    print(body.rstrip("\n") if body.strip() else "(空)")


def _quote_arg(arg: str) -> str:
    """含空格的 argv 元素用双引号包起来。

    实测 `shutil.which("node")` 在本机解析到 `D:/New Folder/node.EXE`（Windows 绝对
    路径，含空格）；不引号的话，失败块里打印出来的命令复制粘贴执行会失败。
    这里自拼引号，不引 shell 转义库。
    """
    return f'"{arg}"' if (" " in arg or "\t" in arg) else arg


def _format_argv(argv: list[str]) -> str:
    """把 argv 渲染成可直接粘贴执行的一行。"""
    return " ".join(_quote_arg(a) for a in argv)


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    total = len(CHECKS)
    node_path = shutil.which("node")
    n_pass = 0
    elapsed_total = 0.0

    for index, check in enumerate(CHECKS, 1):
        argv, build_err = build_argv(check, node_path)
        if argv is None:
            result = Result(False, None, 0.0, "", "", build_err)
        else:
            result = run_check(argv)
        elapsed_total += result.elapsed

        print(check_line(index, total, check.name, verdict_text(result)))

        if result.ok:
            n_pass += 1
            continue

        # ---- 失败：打印完整 stdout + stderr，立即停止（不跑后面的检查）----
        print()
        print("-" * 72)
        print(f"FAILED  {check.name}")
        print(f"命令    {_format_argv(argv) if argv else '(未执行)'}")
        print(f"原因    {result.note or f'exit code = {result.exit_code}'}")
        print("-" * 72)
        _section("stdout", result.stdout)
        _section("stderr", result.stderr)
        print("-" * 72)
        print()
        print("─" * RULE_WIDTH)
        print(f"{n_pass}/{total} PASS, 1 FAIL, stopped at [{index}/{total}]  "
              f"({elapsed_total:.1f}s)")
        return 1

    print("─" * RULE_WIDTH)
    print(f"{n_pass}/{total} PASS ({elapsed_total:.1f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

