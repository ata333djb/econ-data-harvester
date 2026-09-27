#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fred_client 的命令行封装 —— 供 DSH 插件（src/plugins/fred-adapter.js）调用。

设计意图
--------
与 nbs_client_cli / worldbank_client_cli / imf_client_cli 同构：JS 插件是**薄壳**，
只做 argv 翻译与 stdout JSON 信封翻译；采集逻辑在 Python 侧（fred_client）。

约定（插件侧依赖此契约）
------------------------
* **成功**：stdout 是一行 JSON 信封，ok=true，退出码 0。
* **失败**：stdout 仍是 ok=false + error，退出码 2。
* 人类可读日志走 stderr。

用法
----

    ./.venv/Scripts/python.exe -m econ_core.fred_client_cli fetch-series --series-id CHNCPIALLMINMEI
    ./.venv/Scripts/python.exe -m econ_core.fred_client_cli list-search --query "china cpi"
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from econ_core import fred_client  # type: ignore[no-redef]
else:
    from . import fred_client

__all__ = ["main"]

EXIT_OK = 0
EXIT_FAIL = 2


class CliError(RuntimeError):
    """CLI 层可预期的失败（参数问题、上游返回不可用等）。"""


def _emit(payload: dict[str, Any]) -> None:
    """把信封打到 stdout（单行 JSON，UTF-8，不转义中文）。"""
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _ok(command: str, data: Any, **extra: Any) -> dict[str, Any]:
    return {"ok": True, "command": command, "data": data, **extra}


def _fail(command: str, error: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "command": command, "data": None, "error": error, **extra}


def cmd_fetch_series(a: argparse.Namespace) -> dict[str, Any]:
    """采集一条 FRED 序列（月度 / 季度 / 年度由数据本身决定）。"""
    rows = fred_client.fetch_series(a.series_id, start=a.start, end=a.end)
    meta = fred_client.last_meta()
    periods = [str(r["period"]) for r in rows]
    lo = periods[0] if periods else "-"
    hi = periods[-1] if periods else "-"
    print(f"[unit] series_id={a.series_id} 条数={len(rows)} "
          f"频率={meta.get('frequency')} 范围={lo}~{hi}", file=sys.stderr)
    # 无数据时**省略** period_min / period_max，而不是写 null —— 插件 schema 里声明的是
    # string，写 null 会让 DSH 判定 "must be a string" 并让整次调用失败（同 imf 那个坑）。
    fields: dict[str, Any] = {
        "row_count": len(rows),
        "series_id": a.series_id,
        "frequency": str(meta.get("frequency") or ""),
        "raw_cache": meta.get("raw_cache", ""),
        "parsed_cache": meta.get("parsed_file", ""),
        "fetched_at": meta.get("fetched_at", ""),
    }
    if periods:
        fields["period_min"] = periods[0]
        fields["period_max"] = periods[-1]
    return _ok("fetch-series", rows, **fields)


def cmd_list_search(a: argparse.Namespace) -> dict[str, Any]:
    """FRED 搜索需要 api_key —— 恒返回空列表，并在信封里说明原因。"""
    items = fred_client.list_search(a.query)
    print(f"[list] FRED 搜索需要 api_key，本命令恒返回空列表（query={a.query!r}）",
          file=sys.stderr)
    return _ok("list-search", items, row_count=len(items), query=a.query,
               note="FRED 的搜索接口需要 api_key；本客户端只用免密钥的 fredgraph.csv",
               hint="请直接给出 series_id（如 CHNCPIALLMINMEI、CPALTT01CNM659N）")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fred_client_cli",
        description="FRED（fredgraph.csv 免密钥端点）采集 CLI（供 DSH 插件转发调用）",
    )
    sub = p.add_subparsers(dest="command", required=True)

    f = sub.add_parser("fetch-series", help="采集一条 FRED 序列")
    f.add_argument("--series-id", required=True,
                   help='FRED 序列 ID，如 "CHNCPIALLMINMEI"（中国 CPI 全项指数，2015=100，月度）')
    f.add_argument("--start", default=None, help="起始（含），如 2015 或 2015-01；缺省不限")
    f.add_argument("--end", default=None, help="结束（含），格式同上；缺省不限")
    f.set_defaults(func=cmd_fetch_series)

    ls = sub.add_parser("list-search", help="FRED 搜索（需要 api_key，恒返回空列表）")
    ls.add_argument("--query", required=True, help='搜索词，如 "china cpi"')
    ls.set_defaults(func=cmd_list_search)

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
    except fred_client.FredApiError as exc:
        _emit(_fail(command, f"FRED 接口错误: {exc}"))
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

