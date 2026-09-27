#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nbs_client 的命令行封装 —— 供 DSH 插件（src/plugins/nbs-adapter.js）调用。

设计意图
--------
JS 插件是**薄壳**：它只负责把 DSH 工具参数翻译成 argv、把 stdout 的 JSON 翻译回
工具返回值，不实现任何采集/解析逻辑。所有逻辑都在 Python 侧
（``nbs_client`` 采集、``normalize`` 规范化），因此本模块只做三件事：

1. argparse 分发到 nbs_client 的四个函数
2. fetch-indicator 额外走一遍 normalize，输出规范化长表
3. 统一把结果序列化成 ``{"ok": ..., "data": ..., ...}`` 信封打到 stdout

约定（插件侧依赖此契约）
------------------------
* **成功**：stdout 是一行 JSON 信封，``ok=true``，进程退出码 0。
* **失败**：stdout 仍是一行 JSON 信封，``ok=false`` 且带 ``error``，进程退出码 2。
  这样插件无论成败都能拿到结构化错误，不需要去猜 stderr。
* 人类可读的日志（http_client 的 `[INFO] 存档 raw -> ...`、nbs_client 的
  `[parsed] ...`）一律走 **stderr**，不污染 stdout。

用法
----
::

    .\\.venv\\Scripts\\python.exe -m econ_core.nbs_client_cli fetch-indicator \\
        --cid f7fd25aaad184414875632cf2327da60 \\
        --indicator-id db8e5a86c08246e79b1b11251927e740 \\
        --tree-node-id 7dc6a2ee6c614960b7059991e0cc4d96 \\
        --root-id 71d41888d5a44bb2a67402ef4e60003e \\
        --region-code 000000000000 \\
        --periods 2015YY,2016YY

    .\\.venv\\Scripts\\python.exe -m econ_core.nbs_client_cli get-catalog-tree --cid <i值>
    .\\.venv\\Scripts\\python.exe -m econ_core.nbs_client_cli get-default-indicator --code 21
    .\\.venv\\Scripts\\python.exe -m econ_core.nbs_client_cli list-provinces
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from econ_core import nbs_client, normalize  # type: ignore[no-redef]
else:
    from . import nbs_client, normalize

__all__ = ["main"]

EXIT_OK = 0
EXIT_FAIL = 2

#: 全国地区代码（getAllProvince 不含此项，单独约定）
NATIONAL_CODE = "000000000000"
NATIONAL_NAME = "全国"


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
# 地区名解析
# --------------------------------------------------------------------------- #

def resolve_region_name(region_code: str, explicit: Optional[str] = None) -> str:
    """按地区代码解析中文名。

    优先级：显式传入 > 全国约定 > 调 list_provinces 反查 > 空串。

    反查失败不抛错（地区名只是展示字段，不该让整次采集失败），但会打 stderr 提示。

    :param region_code: 12 位地区代码。
    :param explicit: 调用方显式指定的名称，给了就直接用。
    :returns: 地区中文名，解析不到时为 ``""``。
    """
    if explicit:
        return explicit
    if region_code == NATIONAL_CODE:
        return NATIONAL_NAME
    try:
        for p in nbs_client.list_provinces(save=False):
            if str(p.get("value")) == region_code:
                return str(p.get("text") or "")
    except Exception as exc:  # noqa: BLE001 - 展示字段，失败降级
        print(f"[warn] 无法解析地区名（{region_code}）: {type(exc).__name__}: {exc}",
              file=sys.stderr)
    return ""


# --------------------------------------------------------------------------- #
# 子命令实现
# --------------------------------------------------------------------------- #

def cmd_fetch_indicator(a: argparse.Namespace) -> dict[str, Any]:
    """采集指标数值并规范化成长期表行。"""
    periods = [p.strip() for p in a.periods.split(",") if p.strip()]
    if not periods:
        raise CliError("--periods 解析后为空，请检查逗号分隔的时间码")

    # 注意：取数接口实际收的 id 是 tree_node_id（请求参数），不是 indicator_id（响应 i）
    raw = nbs_client.fetch_indicator_data(
        cid=a.cid, indicator_id=a.tree_node_id, root_id=a.root_id,
        da=a.region_code, dts=periods,
    )

    request_params = {"cid": a.cid, "id": a.tree_node_id, "da": a.region_code,
                      "dt": "", "rootId": a.root_id, "dts": periods}
    region_name = resolve_region_name(a.region_code, a.region_name)
    meta = normalize.source_meta_from_parsed(
        "getEsDataByIndicatorIdAndDa", request_params,
        region_name=region_name, tree_node_id=a.tree_node_id,
    )
    rows = normalize.normalize_observations(raw, meta)

    return _ok(
        "fetch-indicator", rows,
        row_count=len(rows),
        region_code=a.region_code,
        region_name=region_name,
        period_requested=periods,
        period_count=len(periods),
        raw_cache=meta.get("raw_cache", ""),
        parsed_cache=meta.get("parsed_file", ""),
        fetched_at=meta.get("fetched_at", ""),
    )


def cmd_get_catalog_tree(a: argparse.Namespace) -> dict[str, Any]:
    """按 cid 取目录树。"""
    nodes = nbs_client.get_catalog_tree(a.cid)
    return _ok("get-catalog-tree", nodes, row_count=len(nodes), cid=a.cid)


def cmd_get_default_indicator(a: argparse.Namespace) -> dict[str, Any]:
    """取默认推荐指标数据（年度 code=21）。"""
    data = nbs_client.get_default_indicator(a.code)
    return _ok("get-default-indicator", data, code=a.code,
               y_series_count=len(data.get("yData") or []) if isinstance(data, dict) else 0,
               x_point_count=len(data.get("xData") or []) if isinstance(data, dict) else 0)


def cmd_list_provinces(a: argparse.Namespace) -> dict[str, Any]:
    """列出全部省级地区代码。"""
    provs = nbs_client.list_provinces()
    return _ok("list-provinces", provs, row_count=len(provs))


# --------------------------------------------------------------------------- #
# argparse
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="nbs_client_cli",
        description="国家统计局新版数据平台采集 CLI（供 DSH 插件转发调用）",
    )
    sub = p.add_subparsers(dest="command", required=True)

    f = sub.add_parser("fetch-indicator", help="采集指标数值并输出规范化长表")
    f.add_argument("--cid", required=True, help="数据集 UUID")
    f.add_argument("--indicator-id", required=True, dest="indicator_id",
                   help="响应里的 i 值（语义标识，仅写入结果列，不用于取数）")
    f.add_argument("--tree-node-id", required=True, dest="tree_node_id",
                   help="请求里的 id 值（取数实际使用）")
    f.add_argument("--root-id", required=True, dest="root_id", help="树的首个一级类目 _id")
    f.add_argument("--region-code", default=NATIONAL_CODE, dest="region_code",
                   help=f"地区代码，默认 {NATIONAL_CODE}（全国）")
    f.add_argument("--region-name", default=None, dest="region_name",
                   help="地区中文名；缺省时自动反查")
    f.add_argument("--periods", required=True,
                   help="逗号分隔的时间码，如 2015YY,2016YY")
    f.set_defaults(func=cmd_fetch_indicator)

    c = sub.add_parser("get-catalog-tree", help="按 cid 取目录树")
    c.add_argument("--cid", required=True)
    c.set_defaults(func=cmd_get_catalog_tree)

    d = sub.add_parser("get-default-indicator", help="取默认推荐指标数据")
    d.add_argument("--code", type=int, required=True, help="年度=21 / 季度=20 / 月度=19")
    d.set_defaults(func=cmd_get_default_indicator)

    lp = sub.add_parser("list-provinces", help="列出全部省级地区代码")
    lp.set_defaults(func=cmd_list_provinces)

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
    except nbs_client.NbsApiError as exc:
        _emit(_fail(command, f"NBS 接口错误: {exc}"))
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
