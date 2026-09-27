#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check-cli-envelope.py —— CLI 信封契约测试（真跑子进程，不用桩）。

为什么需要它（补 smoke-*.mjs 的盲区）
------------------------------------
`smoke-*.mjs` 用桩函数替换 `execFile`，只能验证"插件拼出的 argv"，
**验证不了真实 CLI 打到 stdout 的信封契约**。已实测踩过两个坑：

* region 子串误匹配 —— 子串 "eas" 命中 "Middle East, ..."，EAS 从 37 条被放大到 60 条；
* `region=null` —— 触发 DSH output schema 校验失败
  （`tool "wb_list_countries" returned invalid output: "value.region" must be a string`）。

两个都是"桩测不出、真调才暴露"的类型。本脚本直接 subprocess 跑真实 CLI，
按固定规则逐条校验 stdout JSON 信封。

规则设计哲学
------------
**规则按命令语义分类，不追求字段表面统一。**

实测这个仓库的 CLI 有三类命令，它们"该有的字段"本来就不同：

======================  ======================  =============================
类别                     载荷形状                 该类必须有的可追溯字段
======================  ======================  =============================
1. 取数类 `fetch-*`      观测数组                fetched_at + row_count
2. 元数据类 `list-*` /    数组                    row_count
   `get-catalog-tree`
3. 配置类 `get-default-`  object                 （两者都没有）
   `indicator`
======================  ======================  =============================

曾经犯过的错：把 R3 写成"每个 ok=true 都必须有 row_count + fetched_at"。
那等于要求三类命令字段表面统一，结果是元数据类因为"没有 fetched_at"、
配置类因为"连 row_count 都没有"被判失败 —— 而它们本来就取不到 fetched_at。
正确做法是**先判类别，再套该类规则**（见 R11），而不是拿一张字段清单要求所有命令一致。

**新增命令时**：先判它属于哪一类（取数 / 元数据 / 配置），再套该类规则；
若出现第四类语义，应该在 R11 里显式加一条分支，而**不是放宽已有规则**。

校验规则
--------
R1   stdout 必须是单行合法 JSON（strip 后不含换行；除一个行尾换行外无多余空白）
R2   顶层必须含 ok(bool)、command(str)
R3a  若含 fetched_at 字段，必须是 ISO8601 字符串
R3b  若含 row_count 字段，必须是 int
R4   ok=false 时必须含 error(str)
R5   若含 region 字段，必须是 str（**不能是 null**）
R6   ok=true 时 data 必须存在且不为 null；可以是 array 或 object
R7   若含 raw_cache 字段，必须是 str
R8   exit code 与 ok 一致：ok=true -> 0；ok=false -> 非 0

特殊守卫（针对已知坑 + 语义分类）
----------------------------------
R9  worldbank_client_cli list-countries 不传 --region 时，信封里**不应有** region 字段
    （null-region 那个 bug 的守卫）
R10 nbs_client_cli fetch-indicator 的 data 每行必须有 indicator_id 与 tree_node_id，且两者不同
R11 按命令语义分类的必填守卫（关键）：
      * command 含 `fetch`（取数类）必须有 fetched_at —— 取数类必须可追溯到抓取时刻；
      * command 含 `list`（元数据类）必须有 row_count —— 元数据类必须报出计数；
      * `get-catalog-tree` 与 `get-default-indicator` 豁免上述两条
        （前者无 fetched_at，后者两者都无，均属既有语义）。
R9 / R10 / R11 通过时都会在输出里显式打印证据，且**在用例整体失败时也照样打印**，
以免被其它规则的失败吞掉。

用法
----
    .\\.venv\\Scripts\\python.exe tools\\check-cli-envelope.py
退出码：严格模式全部通过 -> 0；否则 -> 1。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, NamedTuple, Optional

PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]

#: 硬编码项目 venv 解释器（不用 PATH 里的 python，与插件同约定）
PYTHON: str = r"D:\universe\econ-data-harvester\.venv\Scripts\python.exe"

#: 让 `-m econ_core.*` 可解析
PYTHONPATH: str = str(PROJECT_ROOT / "python")

NBS_CLI = "econ_core.nbs_client_cli"
WB_CLI = "econ_core.worldbank_client_cli"
IMF_CLI = "econ_core.imf_client_cli"
BIS_CLI = "econ_core.bis_client_cli"

# --- 已知常量（来自第一阶段的探测结论，不使用随机探测） --------------------- #
NBS_CID = "f7fd25aaad184414875632cf2327da60"
NBS_INDICATOR_ID = "db8e5a86c08246e79b1b11251927e740"   # 响应里的 i
NBS_TREE_NODE_ID = "7dc6a2ee6c614960b7059991e0cc4d96"   # 请求里的 id
NBS_ROOT_ID = "71d41888d5a44bb2a67402ef4e60003e"
PERIODS = ",".join(f"{y}YY" for y in range(2015, 2025))

#: 单条命令的超时（秒）。list-indicators 要拉 2 页全量目录，给足。
TIMEOUT_S = 300

#: ISO8601（UTC 或带偏移，允许小数秒）
ISO8601_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


class Case(NamedTuple):
    """一条待校验的命令。"""

    label: str
    module: str
    argv: list[str]
    special: str = ""       # "" | "no_region" | "nbs_rows"


class Breach(NamedTuple):
    """一次断言失败。"""

    rule: str               # R1..R11（含 R3a/R3b）
    message: str
    got: str                # 收到的原始字段值（repr）


CASES: list[Case] = [
    Case("nbs_client_cli list-provinces", NBS_CLI, ["list-provinces"]),
    Case("nbs_client_cli get-default-indicator --code 21", NBS_CLI,
         ["get-default-indicator", "--code", "21"]),
    Case(f"nbs_client_cli get-catalog-tree --cid {NBS_INDICATOR_ID}", NBS_CLI,
         ["get-catalog-tree", "--cid", NBS_INDICATOR_ID]),
    Case("nbs_client_cli fetch-indicator <gdp 2015-2024>", NBS_CLI,
         ["fetch-indicator", "--cid", NBS_CID, "--indicator-id", NBS_INDICATOR_ID,
          "--tree-node-id", NBS_TREE_NODE_ID, "--root-id", NBS_ROOT_ID,
          "--periods", PERIODS],
         special="nbs_rows"),
    Case("worldbank_client_cli fetch-indicator CHN NY.GDP.MKTP.CN 2015:2024", WB_CLI,
         ["fetch-indicator", "--country", "CHN", "--indicator", "NY.GDP.MKTP.CN",
          "--date-range", "2015:2024"]),
    Case("worldbank_client_cli list-indicators GDP", WB_CLI,
         ["list-indicators", "--query", "GDP", "--per-page", "20"]),
    Case("worldbank_client_cli list-countries", WB_CLI,
         ["list-countries"], special="no_region"),
    Case("worldbank_client_cli list-countries --region EAS", WB_CLI,
         ["list-countries", "--region", "EAS"]),
    # --- IMF WEO（第三阶段接入）。R11 分类天然覆盖，无需新分支：
    #     fetch-indicator 含 fetch -> 必有 fetched_at；两个 list-* 含 list -> 必有 row_count。---
    Case("imf_client_cli fetch-indicator NGDPD CHN", IMF_CLI,
         ["fetch-indicator", "--indicator", "NGDPD", "--country", "CHN"]),
    Case("imf_client_cli list-indicators", IMF_CLI,
         ["list-indicators"]),
    Case("imf_client_cli list-countries NGDPD", IMF_CLI,
         ["list-countries", "--indicator", "NGDPD"]),
    # --- BIS（方向 C 第三轮接入）。同上，R11 分类天然覆盖，无需新分支：
    #     两个子命令都含 fetch -> 必有 fetched_at。
    #     ⚠️ 注意这里**只加条目、不改任何已有条目**：BIS 是第五个源，它的 CLI 信封
    #     若不被真跑一次覆盖，smoke-bis-adapter 的桩测试就只是 argv 拼装验证，
    #     而 PROJECT_STATE §3.8 明确写过「smoke 只能验证 argv 拼装，验证不了真实
    #     CLI 的信封契约」。两条 BIS 用例实测各约 2.5s（gzip 响应解压后约 47 KB）。---
    Case("bis_client_cli fetch-cpi --unit 771", BIS_CLI,
         ["fetch-cpi", "--unit", "771"]),
    Case("bis_client_cli fetch-series WS_LONG_CPI A.CN.628", BIS_CLI,
         ["fetch-series", "--dataset", "WS_LONG_CPI", "--key", "A.CN.628"]),
]

# --------------------------------------------------------------------------- #
# 子进程执行
# --------------------------------------------------------------------------- #

def run_cli(module: str, argv: list[str]) -> subprocess.CompletedProcess:
    """用项目 venv 的解释器真跑 CLI，返回 CompletedProcess（bytes 模式）。"""
    env = dict(os.environ)
    env["PYTHONPATH"] = PYTHONPATH
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [PYTHON, "-m", module, *argv],
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=TIMEOUT_S,
    )


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #

def _peek(s: Any, n: int = 200) -> str:
    """截断长文本，便于打日志。"""
    text = str(s)
    return text if len(text) <= n else f"{text[:n]}...(len={len(text)})"


def _is_int(v: Any) -> bool:
    """bool 是 int 的子类，必须排除。"""
    return isinstance(v, int) and not isinstance(v, bool)


def _tname(v: Any) -> str:
    return type(v).__name__


# --------------------------------------------------------------------------- #
# 信封校验
# --------------------------------------------------------------------------- #

def check_envelope(case: Case, proc: subprocess.CompletedProcess,
                   stdout_text: str) -> tuple[list[Breach], Optional[dict[str, Any]]]:
    """按 R1..R11 校验一次 CLI 调用的 stdout，返回 (违规列表, 解析出的信封)。

    信封无法解析为 object 时提前返回（后续规则无从校验）。
    """
    breaches: list[Breach] = []

    # ---- R1：单行合法 JSON ----
    leading = stdout_text[: len(stdout_text) - len(stdout_text.lstrip())]
    trailing = stdout_text[len(stdout_text.rstrip()):]
    stripped = stdout_text.strip()
    if leading != "":
        breaches.append(Breach("R1", "stdout 有前导空白", repr(leading)))
    if trailing not in ("\n", "\r\n"):
        breaches.append(Breach("R1", "stdout 行尾不是单个换行（或多出空白）", repr(trailing)))
    if "\n" in stripped or "\r" in stripped:
        breaches.append(Breach("R1", "stdout 不是单行（strip 后仍含换行）", _peek(stdout_text)))
    try:
        env = json.loads(stripped)
    except ValueError as exc:
        breaches.append(Breach("R1", f"stdout 不是合法 JSON: {exc}", _peek(stripped)))
        return breaches, None
    if not isinstance(env, dict):
        breaches.append(Breach("R1", "顶层不是 JSON object", f"{_tname(env)}: {_peek(stripped)}"))
        return breaches, None

    # ---- R2：ok / command ----
    ok_val = env.get("ok")
    if not isinstance(ok_val, bool):
        breaches.append(Breach("R2", "ok 缺失或不是 bool", f"{ok_val!r} ({_tname(ok_val)})"))
    cmd_val = env.get("command")
    if not isinstance(cmd_val, str):
        breaches.append(Breach("R2", "command 缺失或不是 str", f"{cmd_val!r} ({_tname(cmd_val)})"))

    # ---- R3a：fetched_at 若存在，必须是 ISO8601 字符串 ----
    # 注意这里是"若存在"：元数据类没有 fetched_at 是正常的，必填由 R11 按类别管。
    if "fetched_at" in env:
        fa = env["fetched_at"]
        if not (isinstance(fa, str) and ISO8601_RE.match(fa)):
            breaches.append(Breach("R3a", "fetched_at 存在但不是 ISO8601 字符串",
                                   f"{fa!r} ({_tname(fa)})"))

    # ---- R3b：row_count 若存在，必须是 int ----
    if "row_count" in env:
        rc = env["row_count"]
        if not _is_int(rc):
            breaches.append(Breach("R3b", "row_count 存在但不是 int",
                                   f"{rc!r} ({_tname(rc)})"))

    # ---- R4：ok=false 必须含 error(str) ----
    if ok_val is False:
        err = env.get("error")
        if not (isinstance(err, str) and err.strip()):
            breaches.append(Breach("R4", "ok=false 但 error 缺失/非 str/为空", f"{err!r}"))

    # ---- R5：region 若存在必须是 str（不能是 null） ----
    if "region" in env and not isinstance(env["region"], str):
        breaches.append(Breach("R5", "region 字段存在但不是 str（不能为 null）",
                               f"{env['region']!r} ({_tname(env['region'])})"))

    # ---- R6：ok=true 时 data 必须存在且不为 null；array / object 都可以 ----
    # 不再要求"必须是 array"：配置类命令（get-default-indicator）的 data 是 object，
    # 这是它已声明的工具语义（nbs-adapter OUTPUT_SCHEMA 注释写明「数组或对象」）。
    # ok=false 时不约束 data（既有失败信封按契约写 data=null）。
    if ok_val is True:
        if "data" not in env:
            breaches.append(Breach("R6", "ok=true 但缺少 data 字段", "<字段不存在>"))
        elif env["data"] is None:
            breaches.append(Breach("R6", "ok=true 但 data 为 null", "None"))

    # ---- R7：raw_cache 若存在必须是 str ----
    if "raw_cache" in env and not isinstance(env["raw_cache"], str):
        breaches.append(Breach("R7", "raw_cache 存在但不是 str", f"{env['raw_cache']!r}"))

    # ---- R8：exit code 与 ok 一致 ----
    code = proc.returncode
    if ok_val is True and code != 0:
        breaches.append(Breach("R8", "ok=true 但 exit code != 0", str(code)))
    if ok_val is False and code == 0:
        breaches.append(Breach("R8", "ok=false 但 exit code == 0", str(code)))

    # ---- R9：list-countries 未传 --region 时不应出现 region 字段 ----
    if case.special == "no_region" and "region" in env:
        breaches.append(Breach(
            "R9",
            "list-countries 未传 --region，信封里不应有 region 字段",
            f"region={env['region']!r} ({_tname(env['region'])})",
        ))

    # ---- R10：nbs fetch-indicator 行的 indicator_id / tree_node_id 必须存在且不同 ----
    if case.special == "nbs_rows":
        rows = env.get("data")
        if not isinstance(rows, list):
            breaches.append(Breach("R10", "fetch-indicator 的 data 不是 array，无法校验行语义",
                                   _tname(rows)))
        else:
            bad_idx = [
                i for i, r in enumerate(rows)
                if (not isinstance(r, dict)
                    or not r.get("indicator_id")
                    or not r.get("tree_node_id")
                    or r.get("indicator_id") == r.get("tree_node_id"))
            ]
            if bad_idx:
                sample = rows[bad_idx[0]]
                got = (
                    json.dumps({k: sample.get(k) for k in
                                ("period", "indicator_id", "tree_node_id")},
                               ensure_ascii=False)
                    if isinstance(sample, dict) else repr(sample)
                )
                breaches.append(Breach(
                    "R10",
                    f"{len(bad_idx)}/{len(rows)} 行的 indicator_id/tree_node_id 缺失或未分离"
                    f"（首个坏行 index={bad_idx[0]}）",
                    got,
                ))

    # ---- R11：按命令语义分类的必填守卫 ----
    #   取数类（command 含 fetch）    -> 必须有 fetched_at（可追溯到抓取时刻）
    #   元数据类（command 含 list）   -> 必须有 row_count（必须报出计数）
    #   get-catalog-tree / get-default-indicator -> 两条都豁免：
    #     前者没有 fetched_at，后者连 row_count 都没有，均属既有语义；
    #     它们的名字里既不含 fetch 也不含 list，因此天然不触发，无需特判。
    if isinstance(cmd_val, str):
        if "fetch" in cmd_val and not isinstance(env.get("fetched_at"), str):
            breaches.append(Breach(
                "R11",
                f"command={cmd_val!r} 属取数类（含 fetch），必须有 fetched_at",
                f"{env.get('fetched_at')!r} ({_tname(env.get('fetched_at'))})",
            ))
        if "list" in cmd_val and not _is_int(env.get("row_count")):
            breaches.append(Breach(
                "R11",
                f"command={cmd_val!r} 属元数据类（含 list），必须有 row_count",
                f"{env.get('row_count')!r} ({_tname(env.get('row_count'))})",
            ))

    return breaches, env


# --------------------------------------------------------------------------- #
# 守卫证据（R9 / R10 / R11）
# --------------------------------------------------------------------------- #

def _guard_notes(case: Case, env: Optional[dict[str, Any]]) -> list[tuple[str, str]]:
    """返回**已评估且通过**的守卫证据 [(规则号, 证据文本)]。

    只列出通过的守卫（失败的那条已作为 Breach 单独打印），
    目的是让"守卫确实跑过"这件事在输出里可见；调用方在用例整体失败时也要打印它，
    否则 R3a/R3b 之类的失败会把 R9/R10/R11 的证据吞掉。
    """
    if env is None:
        return []
    notes: list[tuple[str, str]] = []

    # R9：list-countries 未传 --region 时不应有 region 字段
    if case.special == "no_region" and "region" not in env:
        notes.append(("R9", "region 字段不存在（不是 null）"))

    # R10：nbs fetch-indicator 的 indicator_id != tree_node_id
    if case.special == "nbs_rows":
        rows = env.get("data")
        if isinstance(rows, list) and rows:
            r0 = rows[0]
            notes.append((
                "R10",
                f"{len(rows)} 行，"
                f"indicator_id={str(r0.get('indicator_id'))[:8]}… != "
                f"tree_node_id={str(r0.get('tree_node_id'))[:8]}…",
            ))

    # R11：按 command 语义分类
    cmd = env.get("command")
    if isinstance(cmd, str):
        if "fetch" in cmd and isinstance(env.get("fetched_at"), str):
            notes.append(("R11", f"command={cmd} 取数类 -> fetched_at={env['fetched_at']}"))
        if "list" in cmd and _is_int(env.get("row_count")):
            notes.append(("R11", f"command={cmd} 元数据类 -> row_count={env['row_count']}"))
        if cmd in ("get-catalog-tree", "get-default-indicator"):
            notes.append(("R11", f"command={cmd} 属豁免类（既不必 fetched_at 也不必 row_count）"))
    return notes


def _guard_suffix(case: Case, env: Optional[dict[str, Any]]) -> str:
    """PASS 行尾的证据后缀，形如「  [R11 PASS: ...] [R9 PASS: ...]」。"""
    parts = [f"[{rule} PASS: {evidence}]" for rule, evidence in _guard_notes(case, env)]
    return ("  " + " ".join(parts)) if parts else ""


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    print("=" * 78)
    print("check-cli-envelope: CLI 信封契约测试（真跑子进程，无桩）")
    print(f"  解释器    : {PYTHON}")
    print(f"  PYTHONPATH: {PYTHONPATH}")
    print(f"  用例数    : {len(CASES)}（12 个子命令；wb list-countries 跑两次）")
    print("  覆盖源    : nbs / worldbank / imf / bis —— **fred 不在覆盖内**（历史遗留，")
    print("              见 PROJECT_STATE §3.8；它的信封契约目前只有 smoke 的 argv 桩覆盖）")
    print("=" * 78)

    n_pass = 0
    failures: list[tuple[str, list[Breach]]] = []
    rule_hits: dict[str, int] = {}

    for case in CASES:
        try:
            proc = run_cli(case.module, case.argv)
        except subprocess.TimeoutExpired:
            print(f"FAIL {case.label} -- 子进程超时（>{TIMEOUT_S}s）")
            failures.append((case.label, [Breach("R0", f"子进程超时 >{TIMEOUT_S}s", "")]))
            rule_hits["R0"] = rule_hits.get("R0", 0) + 1
            continue

        stdout_text = proc.stdout.decode("utf-8", "replace")
        stderr_text = proc.stderr.decode("utf-8", "replace")
        breaches, env = check_envelope(case, proc, stdout_text)

        if not breaches:
            n_pass += 1
            print(f"PASS {case.label}{_guard_suffix(case, env)}")
            continue

        print(f"FAIL {case.label} -- {breaches[0].message}")
        for b in breaches:
            rule_hits[b.rule] = rule_hits.get(b.rule, 0) + 1
            print(f"      [{b.rule}] {b.message}")
            print(f"           收到: {b.got}")
        if env is None:
            print(f"           stderr 末尾: {_peek(stderr_text[-400:], 400)!r}")
        # 守卫证据在用例整体失败时也照常打印，避免被其它规则的失败吞掉
        for rule, evidence in _guard_notes(case, env):
            print(f"      [{rule} PASS: {evidence}]")
        failures.append((case.label, breaches))

    total = len(CASES)
    print("-" * 78)
    print(f"{n_pass}/{total} PASS")

    if failures:
        print()
        print("失败按规则汇总:")
        for rule in sorted(rule_hits):
            print(f"  {rule}: {rule_hits[rule]} 处")
        print()
        print("逐条明细:")
        for label, breaches in failures:
            for b in breaches:
                print(f"  {b.rule}  {label}")
                print(f"        {b.message}")
                print(f"        收到: {b.got}")

        print()
        print("-" * 78)
        print("提示：规则按命令语义分类（见脚本顶部 docstring 的「规则设计哲学」）。")
        print("      先判该命令属于哪一类 —— 取数 fetch-* / 元数据 list-* 与 get-catalog-tree")
        print("      / 配置 get-default-indicator —— 再决定是修命令还是改分类；")
        print("      不要为了转绿直接放宽规则。")
        print("=" * 78)
        return 1

    print("=" * 78)
    print("全部用例通过 ✔")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


