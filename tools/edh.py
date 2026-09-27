#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""edh —— EconDataHarvester 的命令行入口（用户产品层）。

定位
----
在此之前，用这个项目要写 Python：知道 `econ_core.nbs_client.fetch_indicator_data`
要 `cid` / `indicator_id` / `root_id` 三个 UUID，还得自己去目录树里翻。
`edh` 把这一层收起来，让「我要 CPI」变成一条命令。

本轮只做**目录**两件事（`list` / `info`），`fetch` 下一轮。

    edh list                  列出所有指标
    edh list --source nbs     只列 NBS 能提供的
    edh info CPI              看 CPI 的详细信息（含各源取数参数）
    edh summary               目录总览（每个源覆盖多少指标）

为什么先做目录而不是直接做 fetch
--------------------------------
因为「用户输入指标名」这件事的难点**不在取数，在命名**。同一个东西有
`CPI` / `cpi` / `通胀` / `居民消费价格指数` / `consumer price index` 五种说法；
反过来 `inflation` 既可能指 CPI 指数、也可能指通胀率。先把「名字 -> 规范指标 ->
各源参数」这条映射做扎实并且可验证，取数才有稳定的落脚点。

输出约定
--------
* 人类可读的表格走 **stdout**（它就是产品）；加 `--json` 时输出 JSON，便于管道。
* 出错走 **stderr**，退出码 2（与五个源 CLI 的失败码一致）。
* 表格宽度按**东亚字符宽度**算，中文列不会串位（`_display_width`）。

用法
----
    .\\.venv\\Scripts\\python.exe tools\\edh.py list
    .\\.venv\\Scripts\\python.exe tools\\edh.py list --source bis
    .\\.venv\\Scripts\\python.exe tools\\edh.py info CPI
    .\\.venv\\Scripts\\python.exe tools\\edh.py info 居民消费价格指数 --json
"""

from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from pathlib import Path
from typing import Any, Optional, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "python"))

from econ_core import catalog  # noqa: E402
from econ_core.catalog import CatalogError  # noqa: E402

#: 源的中文标签（只用于显示，机器可读的键仍是英文）
SOURCE_LABELS: dict[str, str] = {
    "nbs": "国家统计局",
    "worldbank": "世界银行",
    "imf": "IMF",
    "fred": "FRED",
    "bis": "BIS",
}

#: 频率的中文标签
FREQ_LABELS: dict[str, str] = {
    "monthly": "月度",
    "quarterly": "季度",
    "annual": "年度",
}


def _display_width(text: str) -> int:
    """按东亚字符宽度算显示宽度（中文/全角算 2，其余算 1）。

    为什么要自己算：`len("国内生产总值")` 是 6，但终端里占 12 列，
    直接用 `len` 补空格会让中文列全部串位。
    """
    width = 0
    for ch in str(text):
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def _pad(text: str, width: int) -> str:
    """左对齐补空格到指定**显示**宽度。"""
    return str(text) + " " * max(0, width - _display_width(text))


def _freq_text(freqs: Sequence[str]) -> str:
    """['monthly','annual'] -> '月度/年度'。"""
    return "/".join(FREQ_LABELS.get(f, str(f)) for f in freqs)


def _rule(width: int = 88) -> str:
    return "─" * width


# --------------------------------------------------------------------------- #
# 子命令
# --------------------------------------------------------------------------- #

def cmd_list(args: argparse.Namespace) -> int:
    """`edh list [--source SRC]`：列出指标。"""
    try:
        rows = catalog.list_indicators()
    except CatalogError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2

    if args.source:
        src = args.source.strip().lower()
        if src not in catalog.SOURCES:
            print(f"错误: 未知源 {args.source!r}（可用: {', '.join(catalog.SOURCES)}）",
                  file=sys.stderr)
            return 2
        rows = [r for r in rows if src in r["sources"]]
        title = f"EconDataHarvester 指标目录 —— {SOURCE_LABELS[src]}（{src}）可提供 " \
                f"{len(rows)} 个指标"
    else:
        title = f"EconDataHarvester 指标目录 —— 共 {len(rows)} 个指标"

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    print(title)
    print(_rule())
    w_name = max([_display_width(r["name"]) for r in rows] + [8])
    w_disp = max([_display_width(r["display_name"]) for r in rows] + [6])
    header = (f"  {_pad('指标', w_name)}  {_pad('中文名', w_disp)}  "
              f"{_pad('可用源', 26)}  {_pad('频率', 10)} 源数")
    print(header)
    print(_rule())
    for r in rows:
        srcs = " ".join(r["available_sources"])
        print(f"  {_pad(r['name'], w_name)}  {_pad(r['display_name'], w_disp)}  "
              f"{_pad(srcs, 26)}  {_pad(_freq_text(r['frequency']), 10)} {r['n_sources']}")
    print(_rule())
    summ = catalog.catalog_summary()
    print(f"  源映射共 {summ['n_source_mappings']} 条："
          + "、".join(f"{SOURCE_LABELS[s]} {summ['per_source'][s]['n']}"
                      for s in catalog.SOURCES))
    print("  提示: `edh info <指标名>` 看某条指标的详细取数参数；"
          "指标名支持别名与中文。")
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    """`edh info NAME`：看单个指标。"""
    try:
        info = catalog.get_indicator(args.name)
    except CatalogError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0

    print(f"{info['name']} —— {info['display_name']}")
    print(_rule())
    print(f"  说明    {info['description']}")
    print(f"  别名    {', '.join(info['aliases'])}")
    print(f"  频率    {_freq_text(info['frequency'])}")
    srcs = "、".join(f"{SOURCE_LABELS[s]}（{s}）" for s in info["available_sources"])
    print(f"  可用源  {srcs}")
    if info["is_alias"]:
        print(f"  （由 {info['resolved_from']!r} 解析而来）")
    if info.get("notes"):
        print(f"  备注    {info['notes']}")
    print()
    print("  各源取数参数：")
    for source in info["available_sources"]:
        spec = info["sources"][source]
        print(f"    [{source}] {SOURCE_LABELS[source]}")
        for key, value in spec.items():
            if key == "variants":
                continue
            print(f"      {_pad(key, 16)} {value}")
        # variants 单独展开成缩进块。直接 print(dict) 会得到一行 Python repr
        # （`[{'variant': 'monthly', ...}]`），又长又难读，等于把内部结构漏给用户。
        for var in spec.get("variants") or []:
            name = var.get("variant")
            print(f"      variants -> {name}")
            for key, value in var.items():
                if key == "variant":
                    continue
                print(f"        {_pad(key, 14)} {value}")
    missing = [s for s in catalog.SOURCES if s not in info["sources"]]
    if missing:
        print()
        print(f"  不提供该指标的源: {', '.join(missing)}")
    return 0


def cmd_summary(args: argparse.Namespace) -> int:
    """`edh summary`：目录总览。"""
    try:
        summ = catalog.catalog_summary()
    except CatalogError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(summ, ensure_ascii=False, indent=2))
        return 0
    print(f"指标目录: {summ['path']}")
    print(_rule())
    print(f"  指标数      {summ['n_indicators']}")
    print(f"  源映射数    {summ['n_source_mappings']}")
    print()
    print("  每个源覆盖的指标：")
    for s in catalog.SOURCES:
        entry = summ["per_source"][s]
        print(f"    {_pad(SOURCE_LABELS[s] + f'（{s}）', 22)} {entry['n']:>2} 个  "
              f"{', '.join(entry['indicators'])}")
    return 0


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="edh",
        description="EconDataHarvester 命令行：按指标名查数据（本轮：目录 list / info）。",
        epilog="指标名支持规范名（CPI）、别名（inflation）与中文（居民消费价格指数）。")
    sub = parser.add_subparsers(dest="command")

    p_list = sub.add_parser("list", help="列出所有指标")
    p_list.add_argument("--source", default=None,
                        help=f"只看某个源能提供的（{', '.join(catalog.SOURCES)}）")
    p_list.add_argument("--json", action="store_true", help="输出 JSON")
    p_list.set_defaults(func=cmd_list)

    p_info = sub.add_parser("info", help="显示某个指标的详细信息")
    p_info.add_argument("name", help="指标名（规范名 / 别名 / 中文）")
    p_info.add_argument("--json", action="store_true", help="输出 JSON")
    p_info.set_defaults(func=cmd_info)

    p_summ = sub.add_parser("summary", help="目录总览")
    p_summ.add_argument("--json", action="store_true", help="输出 JSON")
    p_summ.set_defaults(func=cmd_summary)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        return int(args.func(args))
    except CatalogError as exc:  # 目录层可预期的失败
        print(f"错误: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
