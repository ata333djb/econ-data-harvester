#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""World Bank Open Data（v2 REST API）客户端 —— 正式生产接口。

背景
----
World Bank 提供公开 REST API，**不需要密钥、不需要逆向**：

    https://api.worldbank.org/v2/country/{country}/indicator/{indicator}?format=json&date={range}

响应统一是「两元素数组」信封 `[meta, data]`：

* `meta`：分页/来源信息，形如
  `{"page":1,"pages":1,"per_page":"1000","total":10,"sourceid":"2","lastupdated":"..."}`
* `data`：观测值数组；**没有数据时是 `null`**（不是 `[]`）

失败形态：HTTP 200 + `[{"message":[{"id":"120","key":"Invalid value",...}]}]`
（只有 1 个元素且含 `message` 键），因此必须显式识别，不能盲取 `obj[1]`。

落盘约定（与 nbs_client.py 一致）
--------------------------------
raw 走 `http_client` 的存档（`data/raw/_http_cache/`），另把解析后的
`data` 落一份到::

    data/parsed/worldbank/<endpoint>_<sha16>.json

结构::

    {"request": {...}, "raw_cache": "<绝对路径>", "data": [...], "fetched_at": "<ISO UTC>"}

sha16 = sha256(规范化 JSON 请求参数)[:16]，与 nbs_client 同一套指纹算法。

分页
----
World Bank 数据端点的 `per_page` 默认只有 50，不显式指定会在年限较长时
**静默截断**。本模块统一显式传 `per_page`（见 `DEFAULT_PER_PAGE_DATA`），
并在 `meta.pages > 1` 时向 stderr 打警告 —— 截断必须是可见的。

搜索
----
`/v2/indicator` **不支持服务端搜索**：`search` 参数被完全忽略（带 `search=GDP`、
带 `search=ZZZZNONSENSE`、不带 search，返回的 total 与首条 id 一模一样）。
因此 `:func:list_indicators` 改为"拉全量目录 + 本地大小写不敏感子串过滤"，
其中 `per_page` 的语义是**返回的命中条数上限**，实际命中总数见
`:func:last_search_stats`。

单位提醒
--------
观测记录里的 `unit` 字段通常为 `""`，真正的单位写在 `indicator.value`
的括号里（例如 `GDP (current LCU)`）。跨源比对不能只看 `unit`。

用法
----
    # 自检（搜索 GDP 指标 + 取中国 GDP 本币序列）
    $env:PYTHONPATH="python"; .\\.venv\\Scripts\\python.exe -m econ_core.worldbank_client --test

    # 库用法
    from econ_core.worldbank_client import fetch_indicator
    rows = fetch_indicator("CHN", "NY.GDP.MKTP.CN", "2015:2024")
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, Optional, Sequence

if __package__ in (None, ""):  # 允许 python python/econ_core/worldbank_client.py 直跑
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from econ_core import http_client  # type: ignore[no-redef]
else:
    from . import http_client

__all__ = [
    "WbApiError",
    "fetch_indicator",
    "list_indicators",
    "list_countries",
    "indicator_request",
    "last_search_stats",
    "API_ROOT",
    "PARSED_DIR",
]

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

API_ROOT: str = "https://api.worldbank.org/v2"

#: 解析后数据落盘目录
PARSED_DIR: Path = http_client.PROJECT_ROOT / "data" / "parsed" / "worldbank"

#: 指标搜索默认每页条数
DEFAULT_PER_PAGE_INDICATORS: int = 20

#: 国家列表默认每页条数（实测 296 条国家/聚合体，300 可一页取完）
DEFAULT_PER_PAGE_COUNTRIES: int = 300

#: 数据端点默认每页条数；显式放大以避免默认 50 条造成静默截断
DEFAULT_PER_PAGE_DATA: int = 1000

#: 拉取全量指标目录时的服务端分页大小（World Bank 单页上限 20000）
CATALOG_PAGE_SIZE: int = 20000

#: 请求超时（秒）。World Bank 偶发慢响应，比 http_client 默认 30s 放宽。
TIMEOUT_S: float = 60.0


class WbApiError(RuntimeError):
    """World Bank 接口业务层异常（信封不是两元素数组 / 带 message 的错误信封）。"""


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
    """把解析后的数据落一份到 data/parsed/worldbank/<endpoint>_<sha16>.json。

    结构与 nbs_client._persist 逐键一致：request / raw_cache / data / fetched_at。
    """
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


def _split_envelope(obj: Any, endpoint: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """把 `[meta, data]` 信封拆成 (meta, data)，并识别错误信封。

    :raises WbApiError: 不是数组、长度 < 2、或带 `message` 的业务错误信封。
    """
    if not isinstance(obj, list):
        raise WbApiError(
            f"{endpoint} 返回不是数组信封: {type(obj).__name__} -> {str(obj)[:200]!r}"
        )

    if len(obj) == 1 and isinstance(obj[0], dict) and "message" in obj[0]:
        raw_msgs = obj[0].get("message")
        parts: list[str] = []
        if isinstance(raw_msgs, list):
            for m in raw_msgs:
                if isinstance(m, dict):
                    parts.append(f"{m.get('id')}/{m.get('key')}: {m.get('value')}")
                else:
                    parts.append(str(m))
        elif raw_msgs is not None:
            parts.append(str(raw_msgs))
        detail = "; ".join(parts) if parts else json.dumps(obj[0], ensure_ascii=False)
        raise WbApiError(f"{endpoint} 业务错误: {detail}")

    if len(obj) < 2:
        raise WbApiError(f"{endpoint} 信封元素不足 2 个: {len(obj)} -> {str(obj)[:200]!r}")

    meta = obj[0] if isinstance(obj[0], dict) else {}
    data = obj[1]
    if data is None:  # 无数据时 WB 返回 null
        data = []
    if not isinstance(data, list):
        raise WbApiError(f"{endpoint} data 不是数组: {type(data).__name__} -> {str(data)[:200]!r}")
    return meta, data


def _log_meta(endpoint: str, meta: dict[str, Any], n: int,
              warn_pagination: bool = True) -> None:
    """把分页信息打到 stderr（人类可读日志，不污染 stdout 信封）。

    :param warn_pagination: 调用方自己会翻页取完时传 False，抑制
                            "只取了第 N 页"的警告（否则是误导性噪音）。
    """
    page, pages = meta.get("page"), meta.get("pages")
    print(
        f"[meta] {endpoint}: page={page}/{pages} per_page={meta.get('per_page')} "
        f"total={meta.get('total')} 本页条数={n}",
        file=sys.stderr,
    )
    try:
        if warn_pagination and pages is not None and int(pages) > 1:
            print(
                f"[warn] {endpoint}: 共 {pages} 页，本次只取了第 {page} 页"
                f"（per_page={meta.get('per_page')}）——结果可能被截断。",
                file=sys.stderr,
            )
    except (TypeError, ValueError):  # pragma: no cover - 防御性
        pass


def _get(endpoint: str, path: str, params: dict[str, Any], request: dict[str, Any],
         parsed_dir: Path, save: bool,
         warn_pagination: bool = True) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """GET 一个 v2 端点并拆信封；save=True 时落 parsed。

    全程走 `http_client`（raw 存档、重试、超时、UA 都复用同一套），本模块不自造轮子。
    """
    url = f"{API_ROOT}{path}?{urllib.parse.urlencode(params)}"
    resp = http_client.get_json(url, save=save, timeout=TIMEOUT_S)
    meta, data = _split_envelope(resp.content, endpoint)
    _log_meta(endpoint, meta, len(data), warn_pagination=warn_pagination)
    if save:
        out = _persist(endpoint, request, resp, data, parsed_dir)
        print(f"[parsed] {out}", file=sys.stderr)
    return meta, data


def _fetch_all_indicators(save: bool, parsed_dir: Path) -> list[dict[str, Any]]:
    """循环翻页拉取**全量**指标目录。

    服务端 `/v2/indicator` 不支持搜索（见 `:func:list_indicators` 的说明），
    只能整份取回本地过滤。`per_page=20000` 是 World Bank 的单页上限，
    实测 total=29544 => 2 页，因此这里按 `meta.pages` 循环到取完为止。

    :returns: 全量指标数组（顺序即服务端顺序）。
    """
    out: list[dict[str, Any]] = []
    page = 1
    while True:
        params = {"format": "json", "per_page": str(CATALOG_PAGE_SIZE), "page": str(page)}
        request = {"catalog": "indicator_all", "format": "json",
                   "per_page": str(CATALOG_PAGE_SIZE), "page": str(page)}
        meta, data = _get("indicator", "/indicator", params, request, parsed_dir, save,
                          warn_pagination=False)
        out.extend(data)
        try:
            pages = int(meta.get("pages") or 1)
        except (TypeError, ValueError):
            pages = 1
        if page >= pages or not data:
            break
        page += 1
    return out


# --------------------------------------------------------------------------- #
# 公开接口
# --------------------------------------------------------------------------- #

def indicator_request(country: str, indicator: str, date_range: str,
                      per_page: int = DEFAULT_PER_PAGE_DATA) -> dict[str, Any]:
    """构造 fetch_indicator 落盘用的 `request` 指纹（供调用方复现，避免规则漂移）。

    单独暴露的原因是：`normalize.source_meta_from_parsed_worldbank` 需要与调用
    `fetch_indicator` 时**完全相同**的 request dict 才能回填 raw_cache/fetched_at。

    :returns: `{"country":..., "indicator":..., "format":"json", "date":..., "per_page":"..."}`
    """
    return {
        "country": country,
        "indicator": indicator,
        "format": "json",
        "date": date_range,
        "per_page": str(per_page),
    }


def fetch_indicator(country: str, indicator: str, date_range: str,
                    save: bool = True, per_page: int = DEFAULT_PER_PAGE_DATA,
                    parsed_dir: Optional[Path] = None) -> list[dict[str, Any]]:
    """取某国某指标的观测值序列（GET）。

    对应 `/v2/country/{country}/indicator/{indicator}`。

    :param country: 国家代码，2 位（`CN`）或 3 位（`CHN`）均可，
                    也支持 `CN;US` 多国、`all` 全部。
    :param indicator: 指标代码，如 `NY.GDP.MKTP.CN`（GDP 现价本币）、
                      `NY.GDP.MKTP.CD`（GDP 现价美元）。
    :param date_range: 年份区间，`2015:2024`；单年 `2020` 亦可。
    :param save: 是否落盘（raw 走 http_client，parsed 走 data/parsed/worldbank/）。
    :param per_page: 每页条数，默认 1000（WB 原生默认 50，会静默截断）。
    :param parsed_dir: 覆盖 parsed 落盘目录（测试用）。
    :returns: 观测值数组，元素形如::

        {"indicator": {"id": "NY.GDP.MKTP.CN", "value": "GDP (current LCU)"},
         "country": {"id": "CN", "value": "China"},
         "countryiso3code": "CHN", "date": "2020",
         "value": 10348676000000000, "unit": "", "obs_status": "", "decimal": 0}

        注意 `value` 可能是 `null`（该年缺值）；`unit` 常为空串，
        真实单位在 `indicator.value` 的括号里。
    :raises WbApiError: 信封不是 [meta, data] 或带 message 的业务错误。
    """
    params = {"format": "json", "date": date_range, "per_page": str(per_page)}
    path = (f"/country/{urllib.parse.quote(str(country))}"
            f"/indicator/{urllib.parse.quote(str(indicator))}")
    request = indicator_request(country, indicator, date_range, per_page)
    _meta, data = _get("country_indicator", path, params, request,
                       parsed_dir or PARSED_DIR, save)
    return data


#: 最近一次 list_indicators 的检索统计（供 worldbank_client_cli 填 search_hits）。
#: 单线程 CLI 场景够用；多线程调用请自行加锁。
_LAST_SEARCH: dict[str, Any] = {}


def last_search_stats() -> dict[str, Any]:
    """返回最近一次 `:func:list_indicators` 的检索统计。

    :returns: `{"query", "scanned", "total_hits", "returned", "per_page"}`；
              从未调用过时为 `{}`。
    """
    return dict(_LAST_SEARCH)


def list_indicators(query: str, per_page: int = DEFAULT_PER_PAGE_INDICATORS,
                    save: bool = True, parsed_dir: Optional[Path] = None) -> list[dict[str, Any]]:
    """搜索 World Bank 指标（**客户端过滤**，因为服务端不支持搜索）。

    重要事实（实测，证据见 data/raw/_http_cache/ 与 python/_probes/）
    ---------------------------------------------------------------
    `/v2/indicator` **忽略 `search` 参数**：带 `search=GDP`、带
    `search=ZZZZNONSENSE`、以及完全不带 search，返回的 `meta.total`（29544）
    与首条 id（1.0.HCount.1.90usd）**完全相同**。也就是说该端点没有任何服务端
    搜索能力，旧实现返回的其实是目录前 N 条，与 query 无关。

    因此本函数改为：
      1. 拉**全量**指标目录（`per_page=20000`，按 `meta.pages` 循环翻页）；
      2. 在本地对 `id` 与 `name` 做**大小写不敏感子串**匹配；
      3. 返回前 `per_page` 条命中。

    :param query: 搜索关键词，如 `GDP`（不得为空白）。
    :param per_page: **返回的命中条数上限**（不再是服务端分页大小），默认 20。
    :param save: 是否落盘。全量拉取会为每页写一个 parsed 文件（实测 2 页：
                 page1=20000 条 / raw 约 0.53MB，page2=9544 条 / raw 约 0.27MB）；
                 raw 存档同样按页写。
    :param parsed_dir: 覆盖 parsed 落盘目录（测试用）。
    :returns: 命中的指标数组（截断到 per_page 条），元素形如::

        {"id": "NY.GDP.MKTP.CN", "name": "GDP (current LCU)", "unit": "",
         "source": {"id": "2", "value": "World Development Indicators"},
         "sourceNote": "...", "sourceOrganization": "...",
         "topics": [{"id": "...", "value": "..."}]}

              命中总数与扫描总数见 `:func:last_search_stats`。
    :raises WbApiError: query 为空白，或信封不是 [meta, data]。
    """
    needle = str(query or "").strip().lower()
    if not needle:
        raise WbApiError("list_indicators: query 不能为空白（服务端不支持空搜索）")

    catalog = _fetch_all_indicators(save, parsed_dir or PARSED_DIR)
    hits = [
        it for it in catalog
        if needle in str(it.get("id", "")).lower()
        or needle in str(it.get("name", "")).lower()
    ]

    _LAST_SEARCH.clear()
    _LAST_SEARCH.update({
        "query": query,
        "scanned": len(catalog),
        "total_hits": len(hits),
        "returned": min(len(hits), per_page),
        "per_page": per_page,
    })
    print(
        f"[search] query={query!r} 扫描全量 {len(catalog)} 条，命中 {len(hits)} 条，"
        f"返回前 {min(len(hits), per_page)} 条",
        file=sys.stderr,
    )
    return hits[:per_page]


def _region_match(item: dict[str, Any], region: str) -> bool:
    """判断一条 country 记录是否命中 region 过滤。

    采用**大小写不敏感的精确匹配**（region.id / region.iso2code / region.value），
    刻意**不用子串匹配** —— 这是修过的 bug：子串 "eas" 会命中
    "Middle East, North Africa, Afghanistan & Pakistan"（"East" 里含 "eas"），
    实测让 EAS 过滤混进 23 个 MEA 成员，把真实的 37 条放大成 60 条。
    """
    reg = item.get("region")
    if not isinstance(reg, dict):
        return False
    needle = str(region).strip().upper()
    if not needle:
        return False
    for k in ("id", "iso2code", "value"):
        v = reg.get(k)
        if v is not None and str(v).strip().upper() == needle:
            return True
    return False


def list_countries(region: Optional[str] = None, save: bool = True,
                   per_page: int = DEFAULT_PER_PAGE_COUNTRIES,
                   parsed_dir: Optional[Path] = None) -> list[dict[str, Any]]:
    """列国家/地区（GET），可选按 region 过滤。

    对应 `/v2/country?format=json&per_page=300`。

    :param region: 区域过滤，对 `region.id`（`EAS`）/ `region.iso2code`（`Z4`）
                   / `region.value`（`East Asia & Pacific`）做**大小写不敏感的精确**匹配。
                   精确而非子串：子串 "eas" 会误命中 "Middle East, North Africa,
                   Afghanistan & Pakistan"，实测把 EAS 从 37 条放大到 60 条。
                   `None` 时不过滤（含 World Bank 聚合体，其 `region.id == "NA"` ——
                   实测 295 条里 78 条是聚合体）。
    :param save: 是否落盘。
    :param per_page: 每页条数，默认 300（实测总数 295，一页取完）。
    :param parsed_dir: 覆盖 parsed 落盘目录（测试用）。
    :returns: 国家数组，元素形如::

        {"id": "CHN", "iso2Code": "CN", "name": "China",
         "region": {"id": "EAS", "iso2code": "Z4", "value": "East Asia & Pacific"},
         "adminregion": {...}, "incomeLevel": {...}, "lendingType": {...},
         "capitalCity": "Beijing", "longitude": "116.286", "latitude": "40.0495"}

        注意 `id` 是 **3 位 ISO3**，`iso2Code` 是 2 位；`region.id == "NA"`
        表示这是聚合体（如 World、Arab World）而非真实国家。
    :raises WbApiError: 同 `:func:fetch_indicator`。
    """
    params = {"format": "json", "per_page": str(per_page)}
    request = {"region": region, "format": "json", "per_page": str(per_page)}
    _meta, data = _get("country", "/country", params, request,
                       parsed_dir or PARSED_DIR, save)
    if region:
        data = [c for c in data if _region_match(c, region)]
        print(f"[filter] region={region!r} 命中 {len(data)} 条", file=sys.stderr)
    return data


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #

def _selftest() -> int:
    print("=" * 78)
    print("self-test: worldbank_client（真实 API 调用 + 落盘）")
    print("=" * 78)

    # ① 搜索指标（客户端过滤：服务端忽略 search）
    print("\n[1] list_indicators('GDP', per_page=5)  [拉全量 + 本地过滤]")
    inds = list_indicators("GDP", per_page=5)
    st = last_search_stats()
    print(f"  扫描全量: {st.get('scanned')} 条")
    print(f"  命中总数: {st.get('total_hits')} 条")
    print(f"  返回条数: {len(inds)}")
    for i, it in enumerate(inds[:5], 1):
        print(f"  {i}. id={it.get('id')!r}  name={it.get('name')!r}")

    # ② 取中国 GDP（本币）
    print("\n[2] fetch_indicator('CHN', 'NY.GDP.MKTP.CN', '2015:2024')")
    rows = fetch_indicator("CHN", "NY.GDP.MKTP.CN", "2015:2024")
    print(f"返回条数: {len(rows)}")

    # ③ 单位 + 2020 值
    unit_fields = sorted({str(r.get("unit")) for r in rows})
    names = sorted({str((r.get("indicator") or {}).get("value")) for r in rows})
    print(f"unit 字段取值: {unit_fields}")
    print(f"indicator.value: {names}")
    y2020 = [r for r in rows if str(r.get("date")) == "2020"]
    print(f"2020 年: {len(y2020)} 条")
    for r in y2020:
        print(f"  value={r.get('value')!r}  decimal={r.get('decimal')!r}")

    # ④ 完整第一条记录
    print("\n[3] 第一条完整记录")
    print(json.dumps(rows[0] if rows else None, ensure_ascii=False, indent=2))

    print("\n" + "=" * 78)
    print("self-test 完成")
    print("=" * 78)
    return 0


def _main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    p = argparse.ArgumentParser(prog="worldbank_client",
                                description="World Bank Open Data v2 REST 客户端")
    p.add_argument("--test", action="store_true", help="跑自检（真实 API 调用）")
    a = p.parse_args(argv)
    if a.test:
        try:
            return _selftest()
        except WbApiError as exc:
            print(f"[FAIL] {exc}", file=sys.stderr)
            return 2
    p.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())



