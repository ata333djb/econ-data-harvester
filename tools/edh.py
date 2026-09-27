#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""edh —— EconDataHarvester 的命令行入口（用户产品层）。

定位
----
在此之前，用这个项目要写 Python：知道 `econ_core.nbs_client.fetch_indicator_data`
要 `cid` / `indicator_id` / `root_id` 三个 UUID，还得自己去目录树里翻。
`edh` 把这一层收起来，让「我要 CPI」变成一条命令。

本轮做完**目录 + 取数**两层：`list` / `info` / `summary` 是"能看清单"，
`fetch` 是"能拿数据"。

    edh list                  列出所有指标
    edh list --source nbs     只列 NBS 能提供的
    edh info CPI              看 CPI 的详细信息（含各源取数参数）
    edh summary               目录总览（每个源覆盖多少指标）

    edh fetch CPI --from 2020 --to 2024                 取数：CSV 到 stdout
    edh fetch CPI --source nbs --from 2020 --to 2024    只取一个源
    edh fetch CPI --from 2020 --to 2024 -o cpi.csv      写文件
    edh fetch CPI --from 2020 --to 2024 --cross-check   附两两对比简报

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
import csv
import io
import json
import sys
import unicodedata
from pathlib import Path
from typing import Any, Optional, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "python"))

from econ_core import catalog, fetcher  # noqa: E402
from econ_core.catalog import CatalogError  # noqa: E402
from econ_core.fetcher import ROW_FIELDS  # noqa: E402

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
# edh fetch
# --------------------------------------------------------------------------- #

def _fmt_num(v: Any) -> str:
    """数值显示：None -> "—"，整数不带小数点，其余 6 位有效数字。"""
    if v is None:
        return "—"
    if isinstance(v, float) and v.is_integer():
        return f"{int(v)}"
    if isinstance(v, float):
        return f"{v:.6g}"
    return str(v)


def write_csv(rows: list[dict[str, Any]], stream: Any) -> None:
    """按固定列序把行写成 CSV。

    值用 `csv` 模块（stdlib）而不是手拼——单位字段里有中文括号和逗号，
    手拼会在那些地方错列（`unit` 含逗号时尤其）。

    `lineterminator="\\n"` 是**必须**的：`csv.writer` 默认写 `\\r\\n`，
    而 Windows 上 `sys.stdout` 文本模式会把 `\\n` 再翻成 `\\r\\n`，
    于是每行后面多出一个空行（第一版实测：130 行变成 260 行）。
    """
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(ROW_FIELDS)
    for r in rows:
        writer.writerow(["" if r.get(f) is None else r.get(f) for f in ROW_FIELDS])


def _write_output(rows: list[dict[str, Any]], args: argparse.Namespace) -> Optional[str]:
    """把结果写到 stdout 或 --output 指定的文件；返回写到的路径（stdout 时 None）。

    文件用 **utf-8-sig**（带 BOM）：与项目既有的 `data/output/econ_data.csv` 一致，
    这样在 Windows 上双击用 Excel 打开中文列不会乱码。stdout 不加 BOM ——
    管道里的 `head`/`grep` 会把 BOM 当成数据的一部分。
    """
    if args.format == "json":
        text = json.dumps(rows, ensure_ascii=False, indent=2)
        if args.output:
            Path(args.output).write_text(text + "\n", encoding="utf-8")
            return args.output
        print(text)
        return None

    if args.output:
        p = Path(args.output)
        if p.parent and not p.parent.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", encoding="utf-8-sig", newline="") as fh:
            write_csv(rows, fh)
        return args.output
    buf = io.StringIO()
    write_csv(rows, buf)
    sys.stdout.write(buf.getvalue())
    return None


def _briefing(rows: list[dict[str, Any]], args: argparse.Namespace,
              pairs: Optional[list[dict[str, Any]]]) -> None:
    """把「数据来自哪、能不能用」打到 stderr。

    `--quiet` 时只留**一行**摘要：stdout 永远是纯 CSV，但脚本化调用不想看见
    十几行装饰。默认（不 quiet）给完整简报 —— 这是本命令的"能不能用"那半。
    """
    notes = fetcher.last_notes()
    ok = [n for n in notes if n["status"] == "ok"]
    skipped = [n for n in notes if n["status"] != "ok"]

    if args.quiet:
        names = ",".join(n["source"] for n in ok)
        extra = f"，跳过 {len(skipped)}" if skipped else ""
        print(f"[edh] {args.name} {len(ok)} 源 {len(rows)} 行 ({names}){extra}",
              file=sys.stderr)
        return

    names = ", ".join(n["source"] for n in ok)

    def emit(line: str = "") -> None:
        print(line, file=sys.stderr)

    emit()
    emit(f"→ 从 {len(ok)} 个源取数（{names}）")
    w = max([_display_width(SOURCE_LABELS.get(n['source'], n['source'])) + 12
             for n in notes] + [12])
    for n in notes:
        label = f"{SOURCE_LABELS.get(n['source'], n['source'])}（{n['source']}）"
        if n["status"] == "ok":
            emit(f"  {_pad(label, w)} {n['n_rows']:>5} 行")
        else:
            emit(f"  {_pad(label, w)}   ——  {n['status']}: {n['detail'][:58]}")
    emit(f"  {'─' * (w + 6)}")
    emit(f"  {_pad('合计', w)} {len(rows):>5} 行")

    values = [r["value"] for r in rows if isinstance(r.get("value"), (int, float))]
    n_missing = sum(1 for r in rows if r.get("value") is None)
    emit()
    emit("→ 简报：")
    if values:
        emit(f"  - 数值范围: {_fmt_num(min(values))} ~ {_fmt_num(max(values))}")
    else:
        emit("  - 数值范围: （无可用数值）")
    emit(f"  - 空缺:     {n_missing} 行（该期源没有发布，不是取数失败）")
    if rows:
        kinds: dict[str, int] = {}
        for r in rows:
            kinds[r["period_type"]] = kinds.get(r["period_type"], 0) + 1
        emit(f"  - 频率分布: " + "、".join(f"{k} {v} 行" for k, v in sorted(kinds.items())))
        units = sorted({r["unit"] for r in rows if r.get("unit")})
        if units:
            emit(f"  - 单位（共 {len(units)} 种）: " + " | ".join(units))

    if pairs is not None:
        emit("  - 差异（--cross-check）:")
        if not pairs:
            emit("      只有 1 个源有数据，无法两两比对")
        for p in pairs:
            a, b = p["sources"]
            rate = p.get("max_diff_rate")
            rate_s = f"{rate * 100:.3g}%" if isinstance(rate, (int, float)) else "—"
            absdiff = p.get("max_abs_diff")
            abs_s = _fmt_num(absdiff)
            tag = "" if p.get("annualized_for_comparison") else ""
            if p["comparable"]:
                emit(f"      {a} vs {b}: 重叠 {p['n_common']} 期，"
                     f"最大绝对差 {abs_s}（相对 {rate_s}） -> {p['verdict']}{tag}")
            else:
                emit(f"      {a} vs {b}: {p['verdict']}"
                     + (f"（重叠 {p['n_common']} 期）" if p["n_common"] else "")
                     + (f"，最大绝对差 {abs_s} 仅供参考" if absdiff is not None else ""))
                if p.get("reason"):
                    emit(f"          {p['reason']}")
            if p.get("annualized_for_comparison"):
                emit("          （两侧原生频率不同，已按年均值对齐后再比）")


def cmd_fetch(args: argparse.Namespace) -> int:
    """`edh fetch NAME`：取数 -> CSV(stdout) + 简报(stderr)。"""
    try:
        indicator = catalog.get_indicator(args.name)["name"]
    except CatalogError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2

    kwargs: dict[str, Any] = {
        "region": args.region,
        "from_year": args.from_year,
        "to_year": args.to_year,
        "frequency": args.frequency,
        "allow_forecast": args.allow_forecast,
    }
    try:
        if args.source:
            rows = fetcher.fetch_indicator(indicator, source=args.source, **kwargs)
            grouped = {args.source: rows}
        else:
            grouped = fetcher.fetch_all_sources(indicator, **kwargs)
            rows = [r for src in catalog.SOURCES for r in grouped.get(src, [])]
    except (KeyError, fetcher.FetcherError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2

    rows.sort(key=lambda r: (r["source"], r["period"]))
    if not rows:
        print(f"错误: {indicator} 在给定条件下没有取到任何行"
              f"（检查 --source / --from / --to / --frequency）", file=sys.stderr)
        for n in fetcher.last_notes():
            if n["status"] != "ok":
                print(f"      {n['source']}: {n['detail']}", file=sys.stderr)
        return 2

    pairs = fetcher.cross_check(grouped) if args.cross_check else None

    written = _write_output(rows, args)
    if written:
        print(f"\n→ 写入 {written}", file=sys.stderr)
    _briefing(rows, args, pairs)
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

    p_fetch = sub.add_parser(
        "fetch", help="取数：CSV 到 stdout，简报到 stderr",
        epilog="不指定 --source 时**取所有可用源**并合并成一张长表；"
               "不给 --from/--to 时取最近 10 年。")
    p_fetch.add_argument("name", help="指标名（规范名 / 别名 / 中文）")
    p_fetch.add_argument("--region", default="CHN", help="地区代码，默认 CHN")
    p_fetch.add_argument("--source", default=None,
                         help=f"只取一个源（{', '.join(catalog.SOURCES)}）")
    p_fetch.add_argument("--from", dest="from_year", type=int, default=None,
                         help="起始年（含），如 2020")
    p_fetch.add_argument("--to", dest="to_year", type=int, default=None,
                         help="结束年（含），如 2024")
    p_fetch.add_argument("--frequency", default=None, choices=["annual", "monthly"],
                         help="annual=月度行按年均值年化；monthly=只保留原生月度源")
    p_fetch.add_argument("--output", "-o", default=None, help="写文件（默认写 stdout）")
    p_fetch.add_argument("--format", default="csv", choices=["csv", "json"],
                         help="输出格式，默认 csv")
    p_fetch.add_argument("--cross-check", action="store_true",
                         help="对同一指标的所有源做两两对比（写进简报）")
    p_fetch.add_argument("--quiet", action="store_true",
                         help="简报只留一行摘要")
    p_fetch.add_argument("--allow-forecast", action="store_true",
                         help="保留 IMF 的预测年份（默认截到今年）")
    p_fetch.set_defaults(func=cmd_fetch)
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
