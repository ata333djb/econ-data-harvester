#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IMF DataMapper（WEO）客户端 —— 第三个、也是第一个真正独立的交叉验证源。

为什么需要它
------------
NBS 是国内官方源；World Bank 的 China NY.GDP.MKTP.CN 与 NBS **逐位相同**
（见 tools/compare-gdp.py 的结论），所以两者不构成独立交叉验证。
IMF WEO 是独立采集方（IMF 自己的经济学家团队）、独立修订周期（每年 4 月 / 10 月）、
独立口径，与 NBS 的差异率是**真实的统计口径分歧**，不是浮点噪声。

端点
----
`https://www.imf.org/external/datamapper/api/v1/{indicator}/{country}`
无需密钥。

重要实测：Akamai 会 403 掉 Chrome UA，只放行朴素 UA
--------------------------------------------------
这是接这个源时踩到的第一个坑，务必别"顺手把 UA 改回 Chrome"：

============================  ===========  =====================
请求的 User-Agent              HTTP 状态     响应
============================  ===========  =====================
http_client 默认（Chrome 128）  403          AkamaiGHost "Access Denied"
`python-urllib/3.12`           200          application/json
============================  ===========  =====================

实测细节：只改 UA 这一项即可（其余默认头保留），同一个 URL 立刻从 403 变 200；
站点根 `https://www.imf.org/` 同样 403，说明是 UA 特征而非路径问题。
因此本模块固定使用 :data:IMF_HEADERS。

响应结构（与直觉不同，注意）
----------------------------
`/v1/{indicator}/{country}` 返回的是::

    {"values": {"<indicator>": {"<ISO3>": {"1980": 7.8, ...}, ...}, "": null},
     "api": {"version": "1", "output-method": "json"}}

即层级是 `values[指标][国家][年份]`，**country 路径段并不做服务端过滤**——
实测请求 `/NGDPD/CHN` 仍返回全部 229 个国家，所以过滤必须在客户端做。
另外 `values` 里还有一个空字符串键（值为 `null`），解析时要跳过。
数据含**预测值**（实测 CHN 覆盖 1980–2031），做实际值比对时务必自己截年份。

落盘约定（与 nbs / worldbank 一致）
-----------------------------------
    data/parsed/imf/<endpoint>_<sha16>.json
    {"request": {...}, "raw_cache": "<绝对路径>", "data": ..., "fetched_at": "<ISO UTC>"}

用法
----
    # 自检
    $env:PYTHONPATH="python"; .\\.venv\\Scripts\\python.exe -m econ_core.imf_client --test

    # 库用法
    from econ_core.imf_client import fetch_indicator
    rows = fetch_indicator("NGDPD", "CHN")   # [{"period": "2020", "value": 15110.191}, ...]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, Optional, Sequence

if __package__ in (None, ""):  # 允许 python python/econ_core/imf_client.py 直跑
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from econ_core import http_client  # type: ignore[no-redef]
else:
    from . import http_client

__all__ = [
    "ImfApiError",
    "fetch_indicator",
    "list_indicators",
    "list_countries",
    "API_ROOT",
    "PARSED_DIR",
    "IMF_HEADERS",
    "last_meta",
]

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

API_ROOT: str = "https://www.imf.org/external/datamapper/api/v1"

#: 解析后数据落盘目录
PARSED_DIR: Path = http_client.PROJECT_ROOT / "data" / "parsed" / "imf"

#: 请求头：**必须**把 UA 从 Chrome 换成朴素 UA，否则 Akamai 403（见模块 docstring）
IMF_HEADERS: dict[str, str] = {"User-Agent": "python-urllib/3.12"}

#: 请求超时（秒）。indicators 仅 48KB，单指标 150KB 量级，给足即可。
TIMEOUT_S: float = 60.0

#: 年份键的形状
_RE_YEAR = re.compile(r"^\d{4}$")

#: 指标目录里的关键字段（其余原样保留在 parsed 里，此处只挑常用的）
_META_KEYS: tuple[str, ...] = ("label", "unit", "source", "dataset", "description")


#: 最近一次 API 调用的来源元数据（供 imf_client_cli 填 raw_cache / fetched_at）。
#: 单线程 CLI 场景够用；多线程调用请自行加锁。
_LAST_META: dict[str, Any] = {}


def last_meta() -> dict[str, Any]:
    """返回最近一次 API 调用的来源元数据。

    :returns: @@{"fetched_at", "raw_cache", "parsed_file"}@@；从未调用过时为空 dict。
    """
    return dict(_LAST_META)


class ImfApiError(RuntimeError):
    """IMF DataMapper 接口业务层异常（信封结构不符 / 缺 values / 指标不存在）。"""


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
             data: Any, parsed_dir: Path, fetched_at: str) -> Path:
    """把解析后的数据落一份到 data/parsed/imf/<endpoint>_<sha16>.json。

    结构与 nbs_client / worldbank_client 逐键一致：
    request / raw_cache / data / fetched_at。
    """
    parsed_dir.mkdir(parents=True, exist_ok=True)
    out = parsed_dir / f"{endpoint}_{_params_digest(request_params)}.json"
    payload = {
        "request": request_params,
        "raw_cache": str(raw_resp.cache_path) if raw_resp.cache_path else "",
        "data": data,
        "fetched_at": fetched_at,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def _get_json(endpoint: str, path: str, request: dict[str, Any],
              parsed_dir: Path, save: bool) -> Any:
    """GET 一个 v1 端点并返回解析后的 JSON；save=True 时落 parsed。

    固定带 :data:IMF_HEADERS（朴素 UA），否则 Akamai 403 —— 见模块 docstring。
    """
    url = f"{API_ROOT}{path}"
    resp = http_client.get_json(url, headers=IMF_HEADERS, save=save, timeout=TIMEOUT_S)
    data = resp.content

    fetched_at = _utc_now()
    raw_cache = str(resp.cache_path) if resp.cache_path else ""
    parsed_file = ""
    if save:
        out = _persist(endpoint, request, resp, data, parsed_dir, fetched_at)
        parsed_file = str(out)
        print(f"[parsed] {out}", file=sys.stderr)

    # 记录来源元数据，供 imf_client_cli 填信封里的 raw_cache / fetched_at（契约 R11）
    _LAST_META.clear()
    _LAST_META.update({"fetched_at": fetched_at, "raw_cache": raw_cache,
                       "parsed_file": parsed_file})
    return data


def _indicator_block(obj: Any, indicator: str) -> dict[str, Any]:
    """从响应里取出 `values[indicator]`；缺了就抛 ImfApiError。

    注意 `values` 里还有个空字符串键（值为 null），这里不碰它。
    """
    if not isinstance(obj, dict):
        raise ImfApiError(f"响应不是 JSON object: {type(obj).__name__}")
    vals = obj.get("values")
    if not isinstance(vals, dict):
        raise ImfApiError(f"响应缺少 values 字段（顶层键={sorted(obj) if isinstance(obj, dict) else '?'}）")
    block = vals.get(indicator)
    if not isinstance(block, dict):
        raise ImfApiError(f"响应里没有指标 {indicator!r} 的数据（values 键={sorted(vals)}）")
    return block


# --------------------------------------------------------------------------- #
# 公开接口
# --------------------------------------------------------------------------- #

def fetch_indicator(indicator: str, country: str, save: bool = True,
                    parsed_dir: Optional[Path] = None) -> list[dict[str, Any]]:
    """取某国某指标的年度序列（GET `/v1/{indicator}/{country}`）。

    **过滤在客户端做**：实测该端点的 country 路径段不生效，响应里始终含全部
    229 个国家/地区，所以这里自己按 country 取那一支。

    :param indicator: 指标代码，如 `NGDPD`（GDP 现价美元，十亿）、
                      `NGDP_RPCH`（实际 GDP 增速 %）、`PCPIPCH`（通胀 %）、
                      `LUR`（失业率 %）。完整 132 条见 :func:list_indicators。
    :param country: ISO3 国家代码，如 `CHN`（大小写不敏感，内部会补一次大写重试）。
    :param save: 是否落盘（raw 走 http_client，parsed 走 data/parsed/imf/）。
    :param parsed_dir: 覆盖 parsed 落盘目录（测试用）。
    :returns: `[{"period": "1980", "value": 7.8}, ...]`，按 period 升序；
              `value` 可能是 `None`（该年缺值）。
              **含 IMF 预测值**（实测 CHN 覆盖 1980–2031），做实际值比对请自行截年份。
              该国无数据时返回 `[]`（不是错误，会打 stderr 提示）。
    :raises ImfApiError: 响应结构不符或该指标不存在。
    """
    path = f"/{urllib.parse.quote(str(indicator))}/{urllib.parse.quote(str(country))}"
    request = {"indicator": indicator, "country": country}
    obj = _get_json("values_by_country", path, request, parsed_dir or PARSED_DIR, save)
    block = _indicator_block(obj, indicator)

    per_country = block.get(country)
    if not isinstance(per_country, dict) and country != country.upper():
        per_country = block.get(country.upper())
    if not isinstance(per_country, dict):
        print(f"[warn] {indicator}/{country} 无数据"
              f"（该指标共 {len(block)} 个国家/地区）", file=sys.stderr)
        return []

    rows: list[dict[str, Any]] = [
        {"period": str(y), "value": v}
        for y, v in per_country.items()
        if _RE_YEAR.match(str(y))
    ]
    rows.sort(key=lambda r: r["period"])
    return rows


def list_indicators(save: bool = True,
                    parsed_dir: Optional[Path] = None) -> list[dict[str, Any]]:
    """取 IMF DataMapper 指标目录（GET `/v1/indicators`，实测 132 条）。

    返回条目形如::

        {"id": "NGDPD", "label": "GDP, current prices", "unit": "U.S. dollars",
         "source": "World Economic Outlook (April 2026)",
         "dataset": "WEO", "description": "Gross domestic product is ..."}

    :param save: 是否落盘。
    :param parsed_dir: 覆盖 parsed 落盘目录（测试用）。
    :returns: 按 id 排序的指标数组。
    :raises ImfApiError: 响应结构不符。
    """
    obj = _get_json("indicators", "/indicators", {"endpoint": "indicators"},
                    parsed_dir or PARSED_DIR, save)
    if not isinstance(obj, dict) or not isinstance(obj.get("indicators"), dict):
        raise ImfApiError("indicators 响应结构不符（缺 indicators 字段）")

    out: list[dict[str, Any]] = []
    for code, meta in obj["indicators"].items():
        m = meta if isinstance(meta, dict) else {}
        row: dict[str, Any] = {"id": code}
        for k in _META_KEYS:
            row[k] = m.get(k)
        out.append(row)
    out.sort(key=lambda d: str(d["id"]))
    return out


def list_countries(indicator: str, save: bool = True,
                   parsed_dir: Optional[Path] = None) -> list[dict[str, Any]]:
    """从某指标的数据里反推国家/地区列表（IMF 没有独立的国家目录端点）。

    调 `/v1/{indicator}`，取 `values[indicator]` 的键集合。

    :param indicator: 指标代码，如 `NGDPD`。
    :param save: 是否落盘。
    :param parsed_dir: 覆盖 parsed 落盘目录（测试用）。
    :returns: `[{"id": "CHN", "year_count": 52, "first_year": "1980",
              "last_year": "2031"}, ...]`，按 id 排序。
    :raises ImfApiError: 响应结构不符或该指标不存在。
    """
    path = f"/{urllib.parse.quote(str(indicator))}"
    request = {"indicator": indicator, "kind": "countries"}
    obj = _get_json("values_by_indicator", path, request, parsed_dir or PARSED_DIR, save)
    block = _indicator_block(obj, indicator)

    out: list[dict[str, Any]] = []
    for code, series in block.items():
        if not isinstance(series, dict):   # 跳过空字符串键（值为 null）
            continue
        years = sorted(str(y) for y in series if _RE_YEAR.match(str(y)))
        out.append({
            "id": code,
            "year_count": len(years),
            "first_year": years[0] if years else None,
            "last_year": years[-1] if years else None,
        })
    out.sort(key=lambda d: str(d["id"]))
    return out


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #

#: 自检用指标 / 国家（GDP 现价美元，十亿）
IMF_TEST_INDICATOR = "NGDPD"
IMF_TEST_COUNTRY = "CHN"


def _selftest() -> int:
    print("=" * 78)
    print("self-test: imf_client（真实 API 调用 + 落盘）")
    print("=" * 78)

    # ① 取中国 GDP（现价美元）
    print(f"\n[1] fetch_indicator({IMF_TEST_INDICATOR!r}, {IMF_TEST_COUNTRY!r})")
    rows = fetch_indicator(IMF_TEST_INDICATOR, IMF_TEST_COUNTRY)
    print(f"  返回条数  : {len(rows)}")
    if rows:
        print(f"  年份范围  : {rows[0]['period']} - {rows[-1]['period']}（含预测值）")
    print(f"  非空值个数: {sum(1 for r in rows if r['value'] is not None)}")
    y2020 = next((r for r in rows if r["period"] == "2020"), None)
    print(f"  2020 年值 : {y2020['value'] if y2020 else None}")

    # ② 指标目录
    print("\n[2] list_indicators()")
    inds = list_indicators()
    print(f"  总条数: {len(inds)}")
    for i, it in enumerate(inds[:5], 1):
        print(f"  {i}. id={it['id']!r}  label={it['label']!r}")

    # ③ 2020 行完整记录
    print("\n[3] 2020 年那行完整记录")
    print(json.dumps(y2020, ensure_ascii=False, indent=2))

    # ④ 附加：从数据反推国家列表
    print(f"\n[4] list_countries({IMF_TEST_INDICATOR!r})")
    cs = list_countries(IMF_TEST_INDICATOR)
    print(f"  国家/地区数: {len(cs)}")
    print(f"  前 3 条    : {json.dumps(cs[:3], ensure_ascii=False)}")

    checks = [
        ("返回条数 > 0", len(rows) > 0),
        ("2020 行存在", y2020 is not None),
        ("指标目录 > 100 条", len(inds) > 100),
        ("国家列表 > 200 个", len(cs) > 200),
    ]
    print()
    for label, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")

    bad = [label for label, ok in checks if not ok]
    print()
    print("=" * 78)
    print("self-test 完成 ✔" if not bad else f"self-test 失败: {bad}")
    print("=" * 78)
    return 0 if not bad else 1


def _main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    p = argparse.ArgumentParser(prog="imf_client",
                                description="IMF DataMapper (WEO) v1 客户端")
    p.add_argument("--test", action="store_true", help="跑自检（真实 API 调用）")
    a = p.parse_args(argv)
    if a.test:
        try:
            return _selftest()
        except ImfApiError as exc:
            print(f"[FAIL] {exc}", file=sys.stderr)
            return 2
    p.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())


