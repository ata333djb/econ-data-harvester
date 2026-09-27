#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""analyze-deps.py —— 从入口脚本量出**真实的** import 闭包，区分 stdlib / 本地 / 第三方。

为什么需要它
------------
"打包时到底要装哪几个第三方包"这个问题**不能靠翻 requirements 或凭印象答**：
一个模块可能 import 了 pandas 但只在某个从不走到的分支里用；
也可能在函数体内部才 `import yaml`（本项目就是，为了缺依赖时报错更清楚），
用 grep 扫文件头会漏掉。

本脚本用 AST 解析所有 `import` / `from ... import` 节点（**含函数体内的**），
从入口脚本出发做传递闭包，再按三条规则分类：

* **stdlib** —— 命中 `sys.stdlib_module_names`
* **local** —— 解析到项目 `python/` 下的模块（含相对导入 `from . import x`）
* **third-party** —— 其余（这些才是打包必须装的）

用法
----
    .\\.venv\\Scripts\\python.exe tools\\analyze-deps.py                 # 默认分析 tools/edh.py
    .\\.venv\\Scripts\\python.exe tools\\analyze-deps.py tools/report.py  # 分析别的入口
    .\\.venv\\Scripts\\python.exe tools\\analyze-deps.py --json           # 机器可读

退出码：0 正常；2 入口不存在。
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PY_ROOT = PROJECT_ROOT / "python"

#: 默认入口（用户产品只走这一条）
DEFAULT_ROOTS: tuple[str, ...] = ("tools/edh.py",)


def _module_name_for(path: Path) -> Optional[str]:
    """项目内的 .py 路径 -> 模块名（`python/econ_core/fetcher.py` -> `econ_core.fetcher`）。

    只认 `python/` 下的文件（那是 PYTHONPATH 的根）；`tools/` 下的脚本是入口，
    不是可 import 的模块，返回 None。
    """
    try:
        rel = path.resolve().relative_to(PY_ROOT.resolve())
    except ValueError:
        return None
    if rel.suffix != ".py":
        return None
    parts = list(rel.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts) if parts else None


def _package_of(path: Path) -> str:
    """该文件所在包（用于解析 `from . import x` 这类相对导入）。"""
    mod = _module_name_for(path)
    if mod is None:
        # tools/ 下的脚本没有包；相对导入会解析失败，但本项目入口都用绝对导入
        return ""
    return mod.rsplit(".", 1)[0] if "." in mod else ""


class ImportCollector(ast.NodeVisitor):
    """收集一个文件里**所有** import（含函数体内、含 try/except 里的）。"""

    def __init__(self) -> None:
        #: 形如 [{"module": "yaml", "level": 0, "lineno": 12, "scope": "load_knowledge"}]
        self.imports: list[dict[str, Any]] = []
        self._func_stack: list[str] = []

    def _scope(self) -> str:
        return self._func_stack[-1] if self._func_stack else "<module>"

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._func_stack.append(node.name)
        self.generic_visit(node)
        self._func_stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802
        for alias in node.names:
            self.imports.append({"module": alias.name, "level": 0,
                                 "lineno": node.lineno, "scope": self._scope(),
                                 "names": []})
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        self.imports.append({"module": node.module or "", "level": node.level,
                             "lineno": node.lineno, "scope": self._scope(),
                             "names": [a.name for a in node.names]})
        self.generic_visit(node)


def _read_imports(path: Path) -> list[dict[str, Any]]:
    """解析一个文件，返回它所有的 import 记录。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    col = ImportCollector()
    col.visit(tree)
    return col.imports


def _resolve(name: str, level: int, pkg: str) -> Optional[str]:
    """把 import 目标解析成绝对模块名；相对导入用 `pkg` 补齐。"""
    if level == 0:
        return name
    parts = pkg.split(".") if pkg else []
    # level=1 -> 当前包；level=2 -> 上一级
    keep = parts[: len(parts) - (level - 1)] if level > 1 else parts
    if name:
        keep = keep + name.split(".")
    return ".".join(p for p in keep if p)


def _classify(mod: str) -> str:
    """`stdlib` / `local` / `third-party`。"""
    top = mod.split(".")[0]
    if top in sys.stdlib_module_names:
        return "stdlib"
    if (PY_ROOT / top).is_dir() or (PY_ROOT / f"{top}.py").is_file():
        return "local"
    return "third-party"


def analyze(roots: Sequence[str]) -> dict[str, Any]:
    """从入口出发做 import 闭包，返回分类结果。"""
    seen: set[Path] = set()
    queue: list[Path] = []
    for r in roots:
        p = (PROJECT_ROOT / r) if not Path(r).is_absolute() else Path(r)
        if not p.is_file():
            raise FileNotFoundError(str(p))
        queue.append(p)

    #: 模块 -> 它被哪些文件/哪一行 import（记证据，不只记结论）
    third: dict[str, list[dict[str, Any]]] = {}
    stdlib: dict[str, list[dict[str, Any]]] = {}
    local_mods: set[str] = set()
    files: list[str] = []

    while queue:
        path = queue.pop()
        if path in seen:
            continue
        seen.add(path)
        try:
            rel = path.relative_to(PROJECT_ROOT).as_posix()
        except ValueError:
            rel = str(path)
        files.append(rel)

        for imp in _read_imports(path):
            base = _resolve(imp["module"], imp["level"], _package_of(path))
            if not base:
                continue
            # `from . import a, b, c` 与 `from econ_core import x` 里的**每个名字都可能是子模块**，
            # 必须逐个展开成 `base.name` 再看它是不是文件。第一版只处理了 base，
            # 于是 `fetcher.py` 里那行 `from . import (bis_client, catalog, ...)` 的
            # 八个 client **一个都没进闭包** —— 第三方依赖因此被少报（这正是本脚本要防的错）。
            targets = [base] + [f"{base}.{n}" for n in imp.get("names") or []]
            for mod in targets:
                kind = _classify(mod)
                evidence = {"from": rel, "line": imp["lineno"], "scope": imp["scope"],
                            "module": mod}
                if kind == "third-party":
                    third.setdefault(mod.split(".")[0], []).append(evidence)
                    continue
                if kind == "stdlib":
                    stdlib.setdefault(mod.split(".")[0], []).append(evidence)
                    continue
                # 本地模块 -> 继续走闭包
                local_mods.add(mod.split(".")[0])
                sub = PY_ROOT / Path(*mod.split("."))
                for cand in (sub.with_suffix(".py"), sub / "__init__.py"):
                    if cand.is_file() and cand not in seen:
                        queue.append(cand)

    return {
        "roots": list(roots),
        "files_analyzed": sorted(files),
        "n_files": len(files),
        "stdlib": {k: v for k, v in sorted(stdlib.items())},
        "local_packages": sorted(local_mods),
        "third_party": {k: v for k, v in sorted(third.items())},
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    parser = argparse.ArgumentParser(
        prog="analyze-deps",
        description="AST 量 import 闭包：哪些第三方包是打包必须的。")
    parser.add_argument("roots", nargs="*", default=list(DEFAULT_ROOTS),
                        help=f"入口脚本（默认 {', '.join(DEFAULT_ROOTS)}）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        res = analyze(args.roots or list(DEFAULT_ROOTS))
    except FileNotFoundError as exc:
        print(f"入口不存在: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0

    print("=" * 88)
    print(f"import 闭包分析：入口 {' '.join(res['roots'])}")
    print("=" * 88)
    print(f"\n扫到 {res['n_files']} 个文件：")
    for f in res["files_analyzed"]:
        print(f"  {f}")

    print(f"\n--- 本地包（{len(res['local_packages'])}）---")
    print("  " + ", ".join(res["local_packages"]))

    print(f"\n--- 第三方包（{len(res['third_party'])}）—— **打包必须装这些** ---")
    if not res["third_party"]:
        print("  （无）")
    for mod, ev in res["third_party"].items():
        scopes = sorted({e["scope"] for e in ev})
        files = sorted({e["from"] for e in ev})
        print(f"  {mod}")
        print(f"      出现在: {', '.join(files)}")
        print(f"      作用域: {', '.join(scopes)}   节点数: {len(ev)}")
        for e in ev[:3]:
            print(f"        - {e['from']}:{e['line']} ({e['scope']})")

    print(f"\n--- stdlib（{len(res['stdlib'])}，打包不用管）---")
    print("  " + ", ".join(sorted(res["stdlib"])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
