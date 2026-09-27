#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""国家统计局新版数据平台（DSF）客户端 —— 正式生产接口。

背景与实测结论（全部经原始响应验证，证据见 data/raw/_http_cache/ 与 python/_probes/）
------------------------------------------------------------------------------------
* 站点入口    : https://data.stats.gov.cn/dg/website/page.html  （hash 路由 SPA）
* 后端 API 根 : https://data.stats.gov.cn/dg/website/publicrelease/web/external/
                前端 axios 的 baseURL 为空，但 dsf 框架会用 webRoot.default="/dg/website/"
                给所有 `$http` 调用补前缀，因此 bundle 里写的 "/publicrelease/web/external/xxx"
                运行时实际是 "/dg/website/publicrelease/web/external/xxx"。
* 旧版 easyquery.htm 已被 WAF 按 UrlACL 403 封禁，不可用。
* 该后端 **HTTP 状态码不反映业务结果**：
    - 路径/参数错误时可能返回 200 + HTML「服务异常」页
    - 业务失败时返回 200 + {"success": false, ...}
  因此所有响应都必须解析后校验 success/state，不能只看状态码。

数据检索链路
------------
1. get_catalogs_and_index_tree(code)   code: 月度=1 / 季度=2 / 年度=3
2. 在返回的树里取 type == "indicator" 的节点 -> 该节点 _id 即 indicator_id，
   其 treeinfo_pid 即指标父节点 cid
3. fetch_indicator_data(cid, indicator_id, root_id, da, dts) 取数

关键参数语义（容易踩坑）
------------------------
* da      —— **不是**数据表 id，而是**地区代码**：全国 "000000000000"，
             香港 "810000000000"，澳门 "820000000000"，台湾 "710000000000"。
             传错值时后端不报错，只回显到 code 字段并返回空值 v=""。
* dts     —— 时间点代码数组，年度加 "YY"、季度 "SS"、月度 "MM"，例如 ["2020YY"]。
* root_id —— 指标树根的第一个一级类目 _id（前端取 root.childNodes[0].childNodes[0]._id）。

落盘约定
--------
每次调用除 http_client 的 raw 存档外，另把解析后的数据落一份到::

    data/parsed/nbs/<endpoint>_<sha16>.json

sha16 = sha256(规范化 JSON 请求参数)[:16]，便于与 raw 层按同一套指纹对齐。
统一结构::

    {"request": {...}, "raw_cache": "<绝对路径>", "data": [...], "fetched_at": "<ISO UTC>"}

用法
----
    # 自检（依次调四个接口并打印条数与首条记录）
    $env:PYTHONPATH="python"; .\\.venv\\Scripts\\python.exe -m econ_core.nbs_client --test

    # 库用法
    from econ_core.nbs_client import fetch_indicator_data
    rows = fetch_indicator_data(
        cid="f7fd25aaad184414875632cf2327da60",
        indicator_id="7dc6a2ee6c614960b7059991e0cc4d96",
        root_id="71d41888d5a44bb2a67402ef4e60003e",
        da="000000000000",
        dts=["2015YY", "2020YY"],
    )
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

if __package__ in (None, ""):  # 允许 `python python/econ_core/nbs_client.py` 直跑
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from econ_core import http_client  # type: ignore[no-redef]
else:
    from . import http_client

__all__ = [
    "NbsApiError",
    "fetch_indicator_data",
    "get_catalog_tree",
    "get_default_indicator",
    "list_provinces",
    "get_catalogs_and_index_tree",
    "PAGE_URL",
    "API_ROOT",
    "PARSED_DIR",
]

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

SITE: str = "https://data.stats.gov.cn"

#: SPA 入口页，同时作为 Referer
PAGE_URL: str = f"{SITE}/dg/website/page.html"

#: 后端 API 根（注意 /dg/website/ 前缀，见模块 docstring）
API_ROOT: str = f"{SITE}/dg/website/publicrelease/web/external"

#: 解析后数据落盘目录
PARSED_DIR: Path = http_client.PROJECT_ROOT / "data" / "parsed" / "nbs"

#: 默认请求头（Referer / XHR 特征为后端必需）
DEFAULT_HEADERS: dict[str, str] = {
    "Referer": PAGE_URL,
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json, text/plain, */*",
}

#: POST JSON 请求头
JSON_HEADERS: dict[str, str] = {**DEFAULT_HEADERS, "Content-Type": "application/json;charset=UTF-8"}

#: 前端 tabsCode 映射：datePage 名 -> code 参数
DATE_PAGE_CODES: dict[str, str] = {
    "monthData": "1",
    "quarterData": "2",
    "yearData": "3",
}


class NbsApiError(RuntimeError):
    """统计局接口业务层异常（HTTP 200 但非预期 JSON / success=false / HTML 服务异常页）。"""


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #

def _params_digest(params: dict[str, Any]) -> str:
    """请求参数的 sha256 前 16 位（键排序 + 非 ASCII 原样），用作 parsed 文件名指纹。"""
    canonical = json.dumps(params, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _persist(endpoint: str, request_params: dict[str, Any], raw_resp: Any,
             data: Any, parsed_dir: Path) -> Path:
    """把解析后的数据落一份到 data/parsed/nbs/<endpoint>_<sha16>.json。"""
    parsed_dir.mkdir(parents=True, exist_ok=True)
    out = parsed_dir / f"{endpoint}_{_params_digest(request_params)}.json"
    payload = {
        "request": request_params,
        "raw_cache": str(raw_resp.cache_path) if raw_resp.cache_path else "",
        "data": data,
        "fetched_at": _utc_now(),
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def _unwrap(resp: Any, endpoint: str) -> Any:
    """校验响应并取出 data 字段。

    容忍三种失败形态（HTTP 均为 200）：
      1. HTML「服务异常」页 / SPA 兜底页 -> 不是 JSON
      2. {"success": false, "message": ...}
      3. {"success": true, "state": 非 20000}
    """
    body = resp.content if isinstance(resp.content, str) else resp.content.decode("utf-8", "replace")
    head = body[:300]
    if not body.lstrip().startswith(("{", "[")):
        raise NbsApiError(
            f"{endpoint} 返回非 JSON（HTTP {resp.status}，{len(body)} 字符）。"
            f"常见原因：路径/参数名错误导致落到错误处理页。响应开头: {head!r}"
        )
    try:
        obj = json.loads(body)
    except json.JSONDecodeError as exc:
        raise NbsApiError(f"{endpoint} JSON 解析失败: {exc}. 响应开头: {head!r}") from exc

    if obj.get("success") is not True:
        raise NbsApiError(
            f"{endpoint} 业务失败: success={obj.get('success')!r} "
            f"state={obj.get('state')!r} message={obj.get('message')!r}"
        )
    state = obj.get("state")
    if state is not None and str(state) != "20000":
        raise NbsApiError(f"{endpoint} 业务状态异常: state={state!r} message={obj.get('message')!r}")
    return obj.get("data")


def _get_json(url: str, endpoint: str, params: dict[str, Any],
              parsed_dir: Path, save: bool = True) -> Any:
    """GET 并解析。用 request() 而非 get_json()，保留原始字节以便诊断「服务异常」页。"""
    resp = http_client.request(url, referer=PAGE_URL, headers=DEFAULT_HEADERS, save=save)
    data = _unwrap(resp, endpoint)
    if save:
        path = _persist(endpoint, params, resp, data, parsed_dir)
        print(f"[parsed] {path}", file=sys.stderr)
    return data


def _post_json(url: str, endpoint: str, body: dict[str, Any],
               parsed_dir: Path, save: bool = True) -> Any:
    """POST JSON 并解析。同 _get_json。"""
    resp = http_client.request(url, method="POST", referer=PAGE_URL, headers=JSON_HEADERS,
                               json_body=body, save=save)
    data = _unwrap(resp, endpoint)
    if save:
        path = _persist(endpoint, body, resp, data, parsed_dir)
        print(f"[parsed] {path}", file=sys.stderr)
    return data


# --------------------------------------------------------------------------- #
# 公开接口
# --------------------------------------------------------------------------- #

def fetch_indicator_data(
    cid: str,
    indicator_id: str,
    root_id: str,
    da: str = "000000000000",
    dts: Optional[Iterable[str]] = None,
    dt: str = "",
    save: bool = True,
    parsed_dir: Optional[Path] = None,
) -> list[dict[str, Any]]:
    """取指标数值序列（核心取数接口，POST JSON）。

    对应后端 `getEsDataByIndicatorIdAndDa`。

    :param cid: 指标节点 id（树节点 `_id`，通常为指标节点的 `treeinfo_pid`）。
    :param indicator_id: 指标 id（树里 `type=="indicator"` 节点的 `_id`）。
                        **必须传 `_id`，不能传 `i`/`ek_dp`**，否则返回 HTML 服务异常页。
    :param root_id: 指标树根的第一个一级类目 `_id`。
    :param da: **地区代码**（不是数据表 id）。全国 `"000000000000"`、
               香港 `"810000000000"`、澳门 `"820000000000"`、台湾 `"710000000000"`。
    :param dts: 时间点代码序列，年度加 `"YY"`、季度 `"SS"`、月度 `"MM"`，
                例如 `["2015YY", "2020YY"]`。为 ``None`` 时用 `[]`。
    :param dt: 数据表代码，默认空串（前端即传 `""`）。
    :param save: 是否落盘（raw 走 http_client，parsed 走 data/parsed/nbs/）。
    :param parsed_dir: 覆盖 parsed 落盘目录（测试用）。
    :returns: 后端 ``data`` 数组，元素为原始结构（不转换字段名），典型形如::

        {"dt": "2020YY", "dt_name": "2020年", "v": "1034867.6",
         "unit": "亿元", "du": "..", "du_name": "亿元",
         "code": "000000000000", "i": "..", "i_name": "国内生产总值"}

    :raises NbsApiError: 响应非 JSON、`success != true` 或 `state != 20000`。
    """
    body: dict[str, Any] = {
        "cid": cid,
        "id": indicator_id,
        "da": da,
        "dt": dt,
        "rootId": root_id,
        "dts": list(dts) if dts is not None else [],
    }
    data = _post_json(
        f"{API_ROOT}/getEsDataByIndicatorIdAndDa",
        "getEsDataByIndicatorIdAndDa", body, parsed_dir or PARSED_DIR, save,
    )
    return data or []


def get_catalog_tree(cid: str, save: bool = True,
                     parsed_dir: Optional[Path] = None) -> list[dict[str, Any]]:
    """取「指标 -> 数据表」目录树（GET）。

    对应后端 `getDaCatalogTreeByIndicatorCid`。
    实测：**参数名必须是 `cid`**；用 `?indicatorCid=` 或 POST body 都会得到 HTML 服务异常页。

    :param cid: 指标 id（即指标记录里的 `i` 值，例如 GDP 的
                `"db8e5a86c08246e79b1b11251927e740"`）。
    :param save: 是否落盘。
    :param parsed_dir: 覆盖 parsed 落盘目录（测试用）。
    :returns: 目录树节点数组，节点含 `_id` / `name` / `show_name` /
              `treeinfo_pid` / `treeinfo_globalid` / `treeinfo_level` /
              `publicrelease_web_dacatalog_id` 等字段。
    :raises NbsApiError: 同 :func:`fetch_indicator_data`。
    """
    params = {"cid": cid}
    data = _get_json(
        f"{API_ROOT}/getDaCatalogTreeByIndicatorCid?"
        f"{urllib.parse.urlencode(params)}",
        "getDaCatalogTreeByIndicatorCid", params, parsed_dir or PARSED_DIR, save,
    )
    return data or []


def get_default_indicator(code: int, save: bool = True,
                          parsed_dir: Optional[Path] = None) -> dict[str, Any]:
    """取默认指标数据（GET）。

    对应后端 `new/getDefaultIndicData`。
    实测：**只支持 GET**，POST（带 `{"code": ...}`）返回 HTML 服务异常页。

    `code` 由前端 `returnCode(datePage)` 推导，tabsCode 为
    月度 `"1"` / 季度 `"2"` / 年度 `"3"`；前端实际传 ``Number(code) + 18``，
    故年度对应 ``3 + 18 = 21``（实测返回居民消费价格指数 CPI 序列）。

    :param code: 指标代码，年度默认用 ``21``。
    :param save: 是否落盘。
    :param parsed_dir: 覆盖 parsed 落盘目录（测试用）。
    :returns: 后端 ``data`` 数组的**第一个元素**（dict），含 ``catalogName`` /
              ``catalogId`` / ``treeinfo_globalid`` / ``xData``（时间点标签数组）/
              ``yData``（各指标序列数组，元素含 ``name`` / ``value`` / ``du_name`` 等）。

              注意：后端返回的 ``data`` 是**数组**（``code=21`` 实测为 4 个目录对象），
              本函数按接口约定只返回首个目录对象；完整数组仍会原样写进
              ``data/parsed/nbs/getDefaultIndicData_<sha16>.json`` 的 ``data`` 字段。
    :raises NbsApiError: 同 :func:`fetch_indicator_data`。
    """
    params = {"code": code}
    data = _get_json(
        f"{API_ROOT}/new/getDefaultIndicData?{urllib.parse.urlencode(params)}",
        "getDefaultIndicData", params, parsed_dir or PARSED_DIR, save,
    )
    # parsed 落盘保留完整数组；返回值按接口约定收窄为首个目录对象
    if isinstance(data, list):
        return data[0] if data else {}
    return data or {}


def list_provinces(save: bool = True,
                   parsed_dir: Optional[Path] = None) -> list[dict[str, Any]]:
    """取省级行政区列表（GET）。

    对应后端 `getAllProvince`。

    :param save: 是否落盘。
    :param parsed_dir: 覆盖 parsed 落盘目录（测试用）。
    :returns: 数组，元素形如 ``{"text": "北京市", "value": "110000000000"}``
              （`value` 为 12 位地区代码，可直接作为取数接口的 ``da``）。

              注意：实测返回 **34** 条 = 31 个省/自治区/直辖市
              **+ 台湾省(710000000000) + 香港特别行政区(810000000000)
              + 澳门特别行政区(820000000000)**，不是 31 条。
    :raises NbsApiError: 同 :func:`fetch_indicator_data`。
    """
    data = _get_json(f"{API_ROOT}/getAllProvince", "getAllProvince", {}, parsed_dir or PARSED_DIR, save)
    return data or []


def get_catalogs_and_index_tree(code: str = "3", save: bool = True,
                                parsed_dir: Optional[Path] = None) -> list[dict[str, Any]]:
    """取指标目录树（GET），用于发现 ``cid`` / ``indicator_id`` / ``root_id``。

    对应后端 `getCatalogsAndIndexTree`。

    :param code: 目录代码，``"1"`` 月度 / ``"2"`` 季度 / ``"3"`` 年度（见 :data:`DATE_PAGE_CODES`）。
    :param save: 是否落盘。
    :param parsed_dir: 覆盖 parsed 落盘目录（测试用）。
    :returns: 树节点数组；节点字段含 `_id` / `name` / `othername` / `type`
              （叶子为 `"indicator"`）/ `treeinfo_pid` / `_level` / `children`。
    :raises NbsApiError: 同 :func:`fetch_indicator_data`。
    """
    params = {"code": code}
    data = _get_json(
        f"{API_ROOT}/getCatalogsAndIndexTree?{urllib.parse.urlencode(params)}",
        "getCatalogsAndIndexTree", params, parsed_dir or PARSED_DIR, save,
    )
    return data or []


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #

#: 自检用的已知恒定参数（来自 python/_probes/ 的探测结论）
SELFTEST = {
    "gdp_cid": "f7fd25aaad184414875632cf2327da60",           # 年度 > 国民经济核算 > 国内生产总值
    "gdp_indicator_id": "7dc6a2ee6c614960b7059991e0cc4d96",  # 国内生产总值 (亿元)
    "gdp_root_id": "71d41888d5a44bb2a67402ef4e60003e",       # 年度树首个一级类目「综合」
    "gdp_i": "db8e5a86c08246e79b1b11251927e740",             # 指标 i 值（get_catalog_tree 入参）
    "gdp_2020_expected": "1034867.6",
}


def _print_result(label: str, count: int, first: Any) -> None:
    print("-" * 74)
    print(f"[{label}]")
    print(f"  条数 : {count}")
    if isinstance(first, dict):
        shown = json.dumps(first, ensure_ascii=False)
    else:
        shown = repr(first)
    print(f"  首条 : {shown[:600]}")


def _selftest() -> int:
    failures: list[str] = []
    st = SELFTEST

    print("=" * 74)
    print("nbs_client 自检")
    print("=" * 74)

    # 1) fetch_indicator_data —— 2020 年 GDP
    rows = fetch_indicator_data(
        cid=st["gdp_cid"], indicator_id=st["gdp_indicator_id"],
        root_id=st["gdp_root_id"], da="000000000000", dts=["2020YY"],
    )
    _print_result("fetch_indicator_data  (2020年 GDP)", len(rows), rows[0] if rows else None)
    got = str(rows[0].get("v")) if rows else None
    if got != st["gdp_2020_expected"]:
        failures.append(f"fetch_indicator_data: 期望 v={st['gdp_2020_expected']}，实际 v={got!r}")
    else:
        print(f"  ✔ v == {st['gdp_2020_expected']}")

    # 2) get_catalog_tree —— 用 GDP 的 i 值
    nodes = get_catalog_tree(st["gdp_i"])
    _print_result(f"get_catalog_tree  (i={st['gdp_i']})", len(nodes), nodes[0] if nodes else None)
    if not nodes:
        failures.append("get_catalog_tree: 返回为空")
    else:
        print(f"  ✔ 非空（{len(nodes)} 个节点）")

    # 3) get_default_indicator(21)
    d = get_default_indicator(21)
    n_y = len(d.get("yData") or [])
    n_x = len(d.get("xData") or [])
    first_y = (d.get("yData") or [None])[0]
    print("-" * 74)
    print("[get_default_indicator(21)]")
    print(f"  catalogName : {d.get('catalogName')}")
    print(f"  catalogId   : {d.get('catalogId')}")
    print(f"  xData 条数  : {n_x}   {json.dumps(d.get('xData'), ensure_ascii=False)[:200]}")
    print(f"  yData 条数  : {n_y}")
    print(f"  首条 yData  : {json.dumps(first_y, ensure_ascii=False)[:600] if first_y else None}")
    if not isinstance(d, dict) or not d.get("catalogName"):
        failures.append("get_default_indicator: 返回为空或缺少 catalogName")
    else:
        print("  ✔ 返回 CPI 数据结构（含 catalogName/xData/yData）")

    # 4) list_provinces —— 实测 34 条（31 省区市 + 台湾/香港/澳门）
    provs = list_provinces()
    _print_result("list_provinces", len(provs), provs[0] if provs else None)
    last3 = [p.get("text") for p in provs[-3:]]
    print(f"  末 3 条: {last3}")
    if len(provs) != 34:
        failures.append(f"list_provinces: 期望 34 条（31 省区市 + 台港澳），实际 {len(provs)}")
    else:
        print("  ✔ 34 条（31 省区市 + 台湾省/香港特别行政区/澳门特别行政区）")

    print("=" * 74)
    if failures:
        print("自检失败:")
        for f in failures:
            print(f"  ✘ {f}")
        return 1
    print("自检全部通过 ✔")
    print("=" * 74)
    return 0


def _main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    p = argparse.ArgumentParser(prog="nbs_client", description="国家统计局新版数据平台客户端")
    p.add_argument("--test", action="store_true", help="运行自检：依次调用四个接口并打印结果")
    args = p.parse_args(argv)

    if args.test:
        return _selftest()
    p.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
