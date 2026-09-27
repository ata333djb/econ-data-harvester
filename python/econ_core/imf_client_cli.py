#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""imf_client 的命令行封装 —— 供 DSH 插件（src/plugins/imf-adapter.js）调用。

设计意图
--------
与 `nbs_client_cli` / `worldbank_client_cli` 同构：JS 插件是**薄壳**，只做
argv 翻译与 stdout JSON 信封翻译；采集逻辑在 Python 侧（`imf_client`）。

与另外两个 CLI 的一个差异：IMF 的 fetch_indicator 返回的已经是
`[{"period", "value"}]` 长表友好结构，因此这里**不再过 normalize**
（不需要字段改名，也没有单位换算要在这里做）。

约定（插件侧依赖此契约）
------------------------
* **成功**：stdout 是一行 JSON 信封，`ok=true`，退出码 0。
* **失败**：stdout 仍是 `ok=false` + `error`，退出码 2。
* 人类可读日志走 stderr。

用法
----
::

    .\\.venv\\Scripts\\python.exe -m econ_core.imf_client_cli fetch-indicator --indicator NGDPD --country CHN
    .\\.venv\\Scripts\\python.exe -m econ_core.imf_client_cli list-indicators
    .\\.venv\\Scripts\\python.exe -m econ_core.imf_client_cli list-countries --indicator NGDPD
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from econ_core import imf_client  # type: ignore[no-redef]
else:
    from . import imf_client

__all__ = ["main"]

EXIT_OK = 0
EXIT_FAIL = 2


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
    """采集指标年度序列。

    注意：IMF 数据**含预测值**（实测 NGDPD/CHN 覆盖 1980–2031），
    这里原样返回并在信封里给出 period_min / period_max，截年份由调用方决定。
    """
    rows = imf_client.fetch_indicator(a.indicator, a.country)
    meta = imf_client.last_meta()
    periods = [str(r["period"]) for r in rows]
    print(f"[unit] indicator={a.indicator} country={a.country} 条数={len(rows)} "
          f"范围={periods[0] if periods else '-'}~{periods[-1] if periods else '-'}",
          file=sys.stderr)
    # 注意：没有数据时**省略** period_min / period_max，而不是写 null ——
    # 插件 output schema 声明这两个字段是 string，写 null 会让 DSH 判定
    # "must be a string" 并让整次调用失败（与 wb_list_countries 的 region 同款坑）。
    fields: dict[str, Any] = {
        "row_count": len(rows),
        "indicator": a.indicator,
        "country": a.country,
        "raw_cache": meta.get("raw_cache", ""),
        "parsed_cache": meta.get("parsed_file", ""),
        "fetched_at": meta.get("fetched_at", ""),
    }
    if periods:
        fields["period_min"] = periods[0]
        fields["period_max"] = periods[-1]
    return _ok("fetch-indicator", rows, **fields)


def cmd_list_indicators(a: argparse.Namespace) -> dict[str, Any]:
    """列出 IMF DataMapper 的全部指标（实测 132 条）。"""
    items = imf_client.list_indicators()
    meta = imf_client.last_meta()
    print(f"[list] list_indicators 共 {len(items)} 条", file=sys.stderr)
    return _ok("list-indicators", items, row_count=len(items),
               raw_cache=meta.get("raw_cache", ""),
               parsed_cache=meta.get("parsed_file", ""))


def cmd_list_countries(a: argparse.Namespace) -> dict[str, Any]:
    """从某指标的数据反推国家/地区列表（IMF 无独立国家目录端点）。"""
    items = imf_client.list_countries(a.indicator)
    meta = imf_client.last_meta()
    print(f"[list] list_countries({a.indicator!r}) 共 {len(items)} 个国家/地区",
          file=sys.stderr)
    return _ok("list-countries", items, row_count=len(items),
               indicator=a.indicator,
               raw_cache=meta.get("raw_cache", ""),
               parsed_cache=meta.get("parsed_file", ""))


# --------------------------------------------------------------------------- #
# argparse
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="imf_client_cli",
        description="IMF DataMapper (WEO) v1 采集 CLI（供 DSH 插件转发调用）",
    )
    sub = p.add_subparsers(dest="command", required=True)

    f = sub.add_parser("fetch-indicator", help="采集指标年度序列")
    f.add_argument("--indicator", required=True,
                   help='指标代码，如 "NGDPD"（GDP 现价美元，十亿）')
    f.add_argument("--country", required=True,
                   help='ISO3 国家代码，如 "CHN"')
    f.set_defaults(func=cmd_fetch_indicator)

    li = sub.add_parser("list-indicators", help="列出全部指标")
    li.set_defaults(func=cmd_list_indicators)

    lc = sub.add_parser("list-countries", help="列出某指标有数据的国家/地区")
    lc.add_argument("--indicator", required=True, help='指标代码，如 "NGDPD"')
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
    except imf_client.ImfApiError as exc:
        _emit(_fail(command, f"IMF 接口错误: {exc}"))
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

