#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bis_client 的命令行封装 —— 供 DSH 插件（src/plugins/bis-adapter.js）调用。

设计意图
--------
与 nbs_client_cli / worldbank_client_cli / imf_client_cli / fred_client_cli 同构：
JS 插件是**薄壳**，只做 argv 翻译与 stdout JSON 信封翻译；采集逻辑在 Python 侧（bis_client）。

约定（插件侧依赖此契约）
------------------------
* **成功**：stdout 是一行 JSON 信封，ok=true，退出码 0。
* **失败**：stdout 仍是 ok=false + error，退出码 2。
* 人类可读日志走 stderr。

两个子命令都属于**取数类**（R11）：ok=true 时必须带 `fetched_at`。
`list-dataflows` 若日后加进来则属于元数据类（带 row_count）—— 本轮不加，
因为 adapter 只需要两个工具，多一个工具就多一份契约面。

用法
----

    ./.venv/Scripts/python.exe -m econ_core.bis_client_cli fetch-cpi --unit 771
    ./.venv/Scripts/python.exe -m econ_core.bis_client_cli fetch-cpi --unit 628 --freq M --country CN
    ./.venv/Scripts/python.exe -m econ_core.bis_client_cli fetch-series --dataset WS_LONG_CPI --key A.CN.628
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from econ_core import bis_client  # type: ignore[no-redef]
else:
    from . import bis_client

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


def _common_fields(meta: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    """组装两个子命令共用的信封字段。

    periods 为空时**省略** period_min / period_max，而不是写 null —— 插件 schema 里
    声明的是 string，写 null 会让 DSH 判定 "must be a string" 并让整次调用失败
    （同 imf / fred 那两个坑，见 PROJECT_STATE §2.2）。
    """
    periods = [str(r["period"]) for r in rows]
    fields: dict[str, Any] = {
        "row_count": len(rows),
        "dataset": str(meta.get("dataset") or ""),
        "key": str(meta.get("key") or ""),
        "frequency": str(meta.get("frequency") or ""),
        "raw_cache": meta.get("raw_cache", ""),
        "parsed_cache": meta.get("parsed_file", ""),
        "fetched_at": meta.get("fetched_at", ""),
    }
    if periods:
        fields["period_min"] = periods[0]
        fields["period_max"] = periods[-1]
    return fields


def cmd_fetch_cpi(a: argparse.Namespace) -> dict[str, Any]:
    """取 BIS 的中国 CPI（月度或年度，同比或指数）。"""
    rows = bis_client.fetch_cpi(unit=a.unit, freq=a.freq, country=a.country)
    meta = bis_client.last_meta()
    print(f"[unit] country={a.country} freq={a.freq} unit={a.unit} "
          f"({meta.get('unit_meaning')}) 条数={len(rows)} "
          f"范围={rows[0]['period'] if rows else '-'}~{rows[-1]['period'] if rows else '-'}",
          file=sys.stderr)
    fields = _common_fields(meta, rows)
    fields["unit"] = a.unit
    fields["unit_meaning"] = str(meta.get("unit_meaning") or "")
    fields["country"] = a.country
    fields["note"] = ("BIS 是转载方（原始数据来自中国国家统计局），且对序列做了拼接与重定基；"
                      "这不是独立验证，只用于扩长序列与拼接检查")
    return _ok("fetch-cpi", rows, **fields)


def cmd_fetch_series(a: argparse.Namespace) -> dict[str, Any]:
    """通用 SDMX-JSON 拉取：dataset + key（key 必须给满 FREQ.REF_AREA.UNIT_MEASURE 三位）。"""
    rows = bis_client.fetch_series(a.dataset, a.key)
    meta = bis_client.last_meta()
    print(f"[unit] dataset={a.dataset} key={a.key} 条数={len(rows)} "
          f"频率={meta.get('frequency')} "
          f"范围={rows[0]['period'] if rows else '-'}~{rows[-1]['period'] if rows else '-'}",
          file=sys.stderr)
    return _ok("fetch-series", rows, **_common_fields(meta, rows))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bis_client_cli",
        description="BIS（国际清算银行 SDMX 2.1）采集 CLI（供 DSH 插件转发调用）",
    )
    sub = p.add_subparsers(dest="command", required=True)

    fc = sub.add_parser("fetch-cpi", help="取 BIS 的中国 CPI（WS_LONG_CPI）")
    fc.add_argument("--unit", default="771",
                    help="771=同比变化%%（默认，实测 1996-01 起）；628=指数（2010=100，1995-01 起）")
    fc.add_argument("--freq", default="M", help="M=月度（默认）；A=年度")
    fc.add_argument("--country", default="CN", help="ISO2 地区代码，默认 CN")
    fc.set_defaults(func=cmd_fetch_cpi)

    fs = sub.add_parser("fetch-series", help="通用 SDMX 拉取（dataset + key）")
    fs.add_argument("--dataset", required=True,
                    help='dataflow id，如 "WS_LONG_CPI"（BIS 长期消费价格统计）')
    fs.add_argument("--key", required=True,
                    help='SDMX 密钥，必须三位 FREQ.REF_AREA.UNIT_MEASURE，如 "M.CN.771" / "A.CN.628"')
    fs.set_defaults(func=cmd_fetch_series)

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
    except bis_client.BisApiError as exc:
        _emit(_fail(command, f"BIS 接口错误: {exc}"))
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
