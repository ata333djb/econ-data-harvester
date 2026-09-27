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
3. node tools/smoke-imf-adapter.mjs                    （插件 argv 拼装，桩）
4. node tools/smoke-fred-adapter.mjs                   （插件 argv 拼装，桩）
5. python tools/check-cli-envelope.py                  （真跑 CLI 的信封契约）
6. python -m econ_core.missing --test                  （缺失分类自检，纯离线 0.1s）
7. python -m econ_core.source_profiler --test          （来源画像自检，纯离线 0.2s）
8. python -m econ_core.arbiter --test                  （口径判定自检，纯离线 0.3s）
9. python -m econ_core.normalize --test                （规范化层自检）
10. python -m econ_core.cross_validation --test         （交叉验证自检）
11. python tools/compare-gdp.py                         （NBS vs World Bank 端到端）
12. python tools/compare-gdp-3way.py                    （NBS vs WB vs IMF 三方交叉验证）
13. python tools/compare-gdp-real.py                    （NBS vs IMF 实际增速，无汇率污染）
14. python tools/compare-unemployment.py               （失业率三方：登记/调查 vs IMF LUR）
15. python tools/compare-cpi.py                        （CPI 交叉验证：NBS vs FRED/OECD）
16. python tools/scan-missing.py                       （缺失检测与分类）
17. python tools/materialize-validated.py              （声明式清单落盘 validated）
18. python tools/run-fill-strategy.py                  （填补策略执行器：只 leave_null/wait）
19. python -m econ_core.credibility --test             （可信度评分自检，纯离线 0.5s）
20. python tools/export.py                             （导出 CSV + SQLite + 数据字典）
21. python tools/report.py --test                      （单文件 HTML 质量报告 + 7 项自检，纯离线）

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

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple, Optional

PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]

#: 最近一次门禁结果的落盘位置（tools/report.py 的页脚读它）
GATE_STATUS_PATH: Path = PROJECT_ROOT / "data" / "output" / "last_gate.json"

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
    timeout_s: Optional[int] = None   # 覆盖全局 TIMEOUT_S（网络密集型检查用）


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
    Check("smoke-imf-adapter", "node", ["tools/smoke-imf-adapter.mjs"]),
    Check("smoke-fred-adapter", "node", ["tools/smoke-fred-adapter.mjs"]),
    # check-cli-envelope 要真跑 13 次网络调用（NBS 4 + WB 3 含 18MB 全量目录 + IMF 3 各约 12s），
    # 实测 48.6s；单个慢调用叠加 http_client 的 4 次重试可到 ~247s，故单独放宽到 420s。
    Check("check-cli-envelope", "python", ["tools/check-cli-envelope.py"], timeout_s=420),
    # missing --test 是纯离线自检（0.1s），覆盖四种缺失分类，最便宜的门禁项，放前面
    Check("missing --test", "python", ["-m", "econ_core.missing", "--test"]),
    # source_profiler --test 同样纯离线（读知识库 YAML + 读已有 validated 文件），0.2s 级
    Check("source_profiler --test", "python",
          ["-m", "econ_core.source_profiler", "--test"]),
    # arbiter --test 也是纯离线（读知识库 + 读 cross_check 产物），0.3s 级；
    # 它把画像判定与实测差异配成一条记录，见 python/econ_core/arbiter.py
    Check("arbiter --test", "python", ["-m", "econ_core.arbiter", "--test"]),
    Check("normalize --test", "python", ["-m", "econ_core.normalize", "--test"]),
    Check("cross_validation --test", "python",
          ["-m", "econ_core.cross_validation", "--test"]),
    Check("compare-gdp", "python", ["tools/compare-gdp.py"]),
    # 同 check-cli-envelope：3way 要跑 NBS + 3 次 WB（含 18MB 全量目录）+ IMF，
    # 实测 16.5s~85.1s（5 倍波动），一次重试风暴可到 ~247s，故同样放宽到 420s。
    Check("compare-gdp-3way", "python", ["tools/compare-gdp-3way.py"], timeout_s=420),
    Check("compare-gdp-real", "python", ["tools/compare-gdp-real.py"]),
    Check("compare-unemployment", "python", ["tools/compare-unemployment.py"]),
    # compare-cpi 要跑 NBS 默认指标 + FRED CSV，网络密集型 -> 420s
    Check("compare-cpi", "python", ["tools/compare-cpi.py"], timeout_s=420),
    # scan-missing 要跑 NBS 4 次 + WB 2 次 + IMF 2 次 + 默认指标 CPI，也是网络密集型
    # （实测出现过 TimeoutError 重试），与另两项同理放宽到 420s。
    Check("scan-missing", "python", ["tools/scan-missing.py"], timeout_s=420),
    # materialize-validated 要现取 NBS/WB/IMF 数据后落盘，网络密集型 -> 420s
    Check("materialize-validated", "python", ["tools/materialize-validated.py"], timeout_s=420),
    # run-fill-strategy 现已脱网（只读 data/validated/），但按保守策略先保留 420s，
    # 观察几轮确认稳定后再收回默认值。顺序上必须排在 materialize-validated 之后。
    Check("run-fill-strategy", "python", ["tools/run-fill-strategy.py"], timeout_s=420),
    # credibility --test 纯离线，但它读三样东西：data/validated/（materialize-validated 产）、
    # data/validated/cross_check/（compare-*.py 产）、data/processed/（run-fill-strategy 产）。
    # 所以必须排在这三者之后，不能挪到 arbiter --test 旁边，否则干净检出时会误报。
    Check("credibility --test", "python", ["-m", "econ_core.credibility", "--test"]),
    # export 只读 data/processed/，不联网，用默认超时
    Check("export", "python", ["tools/export.py"]),
    # report 读 data/ 下的 JSON 合成 HTML，不联网；arbiter/credibility 报告缺失时它会补生成，
    # 所以排在最后（「产物 -> 展示」的顺序，export 本身不依赖它）。
    Check("report", "python", ["tools/report.py", "--test"]),
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


def run_check(argv: list[str], timeout_s: int = TIMEOUT_S) -> Result:
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
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        return Result(False, None, time.monotonic() - started,
                      _decode(exc.stdout), _decode(exc.stderr),
                      f"超时 >{timeout_s}s")
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
# 状态落盘
# --------------------------------------------------------------------------- #

def _write_gate_status(records: list[dict[str, object]], n_pass: int, total: int,
                       elapsed_total: float) -> Optional[Path]:
    """把本次门禁结果落盘（成功、失败都写），供 tools/report.py 的页脚读。

    注意时序：它在本进程**结束时**才写，而同一次运行里的 report（第 19 项）排在
    它前面，所以报告页脚读到的是**上一次**门禁的结果。这是刻意保留的——页脚会连
    ran_at 一起显示，比编一个「本次」状态诚实。
    """
    obj = {
        "ran_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "n_pass": n_pass,
        "n_total": total,
        "elapsed_s": round(elapsed_total, 1),
        "checks": records,
    }
    try:
        GATE_STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
        GATE_STATUS_PATH.write_text(json.dumps(obj, ensure_ascii=False, indent=2),
                                    encoding="utf-8")
    except OSError as exc:
        print(f"[gate] 写 {GATE_STATUS_PATH} 失败: {exc}", file=sys.stderr)
        return None
    return GATE_STATUS_PATH


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
    records: list[dict[str, object]] = []

    for index, check in enumerate(CHECKS, 1):
        argv, build_err = build_argv(check, node_path)
        if argv is None:
            result = Result(False, None, 0.0, "", "", build_err)
        else:
            result = run_check(argv, check.timeout_s or TIMEOUT_S)
        elapsed_total += result.elapsed
        records.append({"name": check.name,
                        "status": "pass" if result.ok else "fail",
                        "elapsed_s": round(result.elapsed, 2)})

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
        for later in CHECKS[index:]:
            records.append({"name": later.name, "status": "skip", "elapsed_s": 0.0})
        _write_gate_status(records, n_pass, total, elapsed_total)

        print(f"{n_pass}/{total} PASS, 1 FAIL, stopped at [{index}/{total}]  "
              f"({elapsed_total:.1f}s)")
        print(f"状态已写入 {GATE_STATUS_PATH.relative_to(PROJECT_ROOT).as_posix()}（FAIL 也写）")
        return 1

    _write_gate_status(records, n_pass, total, elapsed_total)

    print("─" * RULE_WIDTH)
    print(f"{n_pass}/{total} PASS ({elapsed_total:.1f}s)")
    print(f"状态已写入 {GATE_STATUS_PATH.relative_to(PROJECT_ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

