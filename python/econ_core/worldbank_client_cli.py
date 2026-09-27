#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""worldbank_client 的命令行封装 —— 供 DSH 插件（src/plugins/worldbank-adapter.js）调用。

设计意图
--------
与 `nbs_client_cli` 完全同构：JS 插件是**薄壳**，只做 argv 翻译与 stdout JSON
信封翻译；所有采集/规范化逻辑都在 Python 侧（`worldbank_client` 采集、
`normalize` 规范化）。本模块只做三件事：

1. argparse 分发到 worldbank_client 的三个函数
2. fetch-indicator 额外走一遍 normalize（World Bank 记录形状 -> NBS 同形长表）
3. 统一把结果序列化成 `{"ok": ..., "data": ..., ...}` 信封打到 stdout

约定（插件侧依赖此契约，与 nbs_client_cli 一致）
------------------------------------------------
* **成功**：stdout 是一行 JSON 信封，`ok=true`，进程退出码 0。
* **失败**：stdout 仍是一行 JSON 信封，`ok=false` 且带 `error`，退出码 2。
* 人类可读日志（http_client 的存档日志、worldbank_client 的 `[meta]/[parsed]`）
  一律走 **stderr**，不污染 stdout。

用法
----
::

    .\\.venv\\Scripts\\python.exe -m econ_core.worldbank_client_cli ^
        fetch-indicator --country CHN --indicator NY.GDP.MKTP.CN --date-range 2015:2024

    .\\.venv\\Scripts\\python.exe -m econ_core.worldbank_client_cli list-indicators --query GDP --per-page 20
    .\\.venv\\Scripts\\python.exe -m econ_core.worldbank_client_cli list-countries --region EAS
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from econ_core import normalize, worldbank_client  # type: ignore[no-redef]
else:
    from . import normalize, worldbank_client

__all__ = ["main"]

EXIT_OK = 0
EXIT_FAIL = 2

#: fetch-indicator 用的 endpoint 名（与 worldbank_client._get 落盘时一致）
ENDPOINT_COUNTRY_INDICATOR = "country_indicator"


class CliError(RuntimeError):
    """CLI 层可预期的失败（参数问题、上游返回不可用等）。"""


# --------------------------------------------------------------------------- #
# 输出信封
# --------------------------------------------------------------------------- #

def _emit(payload: dict[str, Any]) -> None:
    """把信封打到 stdout（单行 JSON，UTF-8，不转义中文）。"""
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _ok(command: str, data: Any, **extra: Any) -> dict[str, Any]:
    return {"ok": True, "command": command, "data": data, **extra}


def _fail(command: str, error: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "command": command, "data": None, "error": error, **extra}


# --------------------------------------------------------------------------- #
# 子命令实现
# --------------------------------------------------------------------------- #

def cmd_fetch_indicator(a: argparse.Namespace) -> dict[str, Any]:
    """采集指标数值并规范化成长期表行。"""
    raw = worldbank_client.fetch_indicator(a.country, a.indicator, a.date_range)
    request = worldbank_client.indicator_request(a.country, a.indicator, a.date_range)
    meta = normalize.source_meta_from_parsed_worldbank(ENDPOINT_COUNTRY_INDICATOR, request)
    rows = normalize.normalize_worldbank_observations(raw, meta)

    units = sorted({str(r.get("unit") or "") for r in rows})
    names = sorted({str(r.get("indicator_name") or "") for r in rows})
    print(f"[unit] unit 字段={units} indicator_name={names}", file=sys.stderr)

    return _ok(
        "fetch-indicator", rows,
        row_count=len(rows),
        country=a.country,
        indicator=a.indicator,
        date_range=a.date_range,
        raw_cache=meta.get("raw_cache", ""),
        parsed_cache=meta.get("parsed_file", ""),
        fetched_at=meta.get("fetched_at", ""),
    )


def cmd_list_indicators(a: argparse.Namespace) -> dict[str, Any]:
    """搜索指标（客户端过滤）。

    服务端 `/v2/indicator` 忽略 `search` 参数（实测带与不带返回完全相同），
    因此过滤在 worldbank_client.list_indicators 内部完成：拉全量目录后本地对
    id/name 做大小写不敏感子串匹配。

    信封语义：`--per-page` 是**返回命中条数上限**；`row_count` 是本次返回条数；
    `search_hits` 是**全量命中总数**（可能大于 row_count）；
    `catalog_scanned` 是被扫描的全量指标条数。
    """
    items = worldbank_client.list_indicators(a.query, per_page=a.per_page)
    stats = worldbank_client.last_search_stats()
    total_hits = int(stats.get("total_hits", len(items)))
    scanned = int(stats.get("scanned", 0))
    # 检索日志已由 worldbank_client.list_indicators 打到 stderr，这里不再重复
    return _ok("list-indicators", items, row_count=len(items),
               query=a.query, per_page=a.per_page, search_hits=total_hits,
               catalog_scanned=scanned)


def cmd_list_countries(a: argparse.Namespace) -> dict[str, Any]:
    """列国家/地区，可选 region 过滤。

    注意：未传 `--region` 时**不把 region 写进信封**（而不是写 null）——
    插件侧 output schema 声明该字段是 string，写 null 会让 DSH 判定
    "value.region must be a string" 并让整次调用失败（实测踩过）。
    """
    items = worldbank_client.list_countries(a.region)
    extra: dict[str, Any] = {}
    if a.region:
        extra["region"] = a.region
    return _ok("list-countries", items, row_count=len(items), **extra)


# --------------------------------------------------------------------------- #
# argparse
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="worldbank_client_cli",
        description="World Bank Open Data v2 REST 采集 CLI（供 DSH 插件转发调用）",
    )
    sub = p.add_subparsers(dest="command", required=True)

    f = sub.add_parser("fetch-indicator", help="采集指标数值并输出规范化长表")
    f.add_argument("--country", required=True, help='国家代码，如 "CHN" / "CN" / "CN;US"')
    f.add_argument("--indicator", required=True,
                   help='指标代码，如 "NY.GDP.MKTP.CN"（GDP 现价本币）')
    f.add_argument("--date-range", required=True, dest="date_range",
                   help='年份区间，如 "2015:2024"；单年 "2020"')
    f.set_defaults(func=cmd_fetch_indicator)

    li = sub.add_parser("list-indicators", help="搜索指标")
    li.add_argument("--query", required=True, help='搜索关键词，如 "GDP"')
    li.add_argument("--per-page", type=int, default=20, dest="per_page",
                    help="每页条数，默认 20")
    li.set_defaults(func=cmd_list_indicators)

    lc = sub.add_parser("list-countries", help="列国家/地区")
    lc.add_argument("--region", default=None,
                    help='区域过滤（region.id/iso2code/value 子串匹配），如 "EAS"')
    lc.set_defaults(func=cmd_list_countries)

    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command

    try:
        envelope = args.func(args)
    except CliError as exc:
        _emit(_fail(command, str(exc)))
        return EXIT_FAIL
    except worldbank_client.WbApiError as exc:
        _emit(_fail(command, f"World Bank 接口错误: {exc}"))
        return EXIT_FAIL
    except normalize.NormalizeError as exc:
        _emit(_fail(command, f"规范化错误: {exc}"))
        return EXIT_FAIL
    except Exception as exc:  # noqa: BLE001 - CLI 边界，必须兜住并结构化返回
        import traceback
        traceback.print_exc(file=sys.stderr)
        _emit(_fail(command, f"未预期错误: {type(exc).__name__}: {exc}"))
        return EXIT_FAIL

    _emit(envelope)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())

