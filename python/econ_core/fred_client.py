#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FRED 客户端（St. Louis Fed 的 fredgraph.csv 免密钥端点）—— CPI 的独立交叉验证源。

为什么需要它
------------
NBS 的 CPI 此前是**唯一零交叉验证**的指标：GDP 有 World Bank + IMF，失业率有 IMF，
只有 CPI 一条独立来源都没有。FRED 上的 CHNCPIALLMINMEI 源自 OECD Main Economic
Indicators，是独立于中国国家统计局的二次汇编方，正好用来补这个洞。

端点与坑
--------
`https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}`，无需密钥，
返回两列 CSV：`observation_date,<SERIES_ID>`。

**实测：FRED 会把 http_client 默认的 Chrome UA 直接掐断连接**
============================================================

==============================  ==========================  ================
请求的 User-Agent               HTTP 结果                   说明
==============================  ==========================  ================
http_client 默认（Chrome 128）   RemoteDisconnected / 超时   重试 3 次全失败
空 UA                           RemoteDisconnected          同上
python-urllib/3.12              200 + 11791 字节 CSV        正常
==============================  ==========================  ================

这与 IMF 的 Akamai 是**同一类坑**（见 imf_client 模块 docstring）：伪装成浏览器反而
被拒。本模块固定使用 :data:FRED_HEADERS，**不要把 UA 改回 Chrome**。

数据形态
--------
日期一律 `YYYY-MM-DD`；频率从日期分布推断（annual / quarterly / monthly / daily）；
缺值 FRED 用单个 `.` 表示，解析成 `None`。

落盘约定（与 nbs / worldbank / imf 一致）
-----------------------------------------

    data/parsed/fred/<series_id>_<sha16>.json
    {"request": {...}, "raw_cache": "<绝对路径>", "data": ..., "fetched_at": "<ISO UTC>"}

用法
----

    $env:PYTHONPATH="python"; ./.venv/Scripts/python.exe -m econ_core.fred_client --test

    from econ_core.fred_client import fetch_series
    rows = fetch_series("CHNCPIALLMINMEI")   # [{"period": "1993-01", "value": 41.34689}, ...]
"""

from __future__ import annotations

import argparse
import csv as _csv
import hashlib
import io
import json
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, Optional, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from econ_core import http_client  # type: ignore[no-redef]
else:
    from . import http_client

__all__ = [
    "FredApiError",
    "fetch_series",
    "list_search",
    "CSV_ROOT",
    "PARSED_DIR",
    "FRED_HEADERS",
    "last_meta",
]

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

CSV_ROOT: str = "https://fred.stlouisfed.org/graph/fredgraph.csv"

#: 解析后数据落盘目录
PARSED_DIR: Path = http_client.PROJECT_ROOT / "data" / "parsed" / "fred"

#: 请求头：**必须**用朴素 UA，Chrome UA 会被 FRED 掐断连接（见模块 docstring）
FRED_HEADERS: dict[str, str] = {"User-Agent": "python-urllib/3.12"}

#: 请求超时（秒）。单序列 CSV 只有几十 KB。
TIMEOUT_S: float = 60.0

#: 最近一次调用的来源元数据（供 fred_client_cli 填 raw_cache / fetched_at）。
_LAST_META: dict[str, Any] = {}


def last_meta() -> dict[str, Any]:
    """返回最近一次调用的来源元数据（含 frequency / n_rows）。"""
    return dict(_LAST_META)


class FredApiError(RuntimeError):
    """FRED 接口业务层异常（CSV 结构不符 / 序列不存在）。"""


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #

def _params_digest(params: dict[str, Any]) -> str:
    """请求参数的 sha256 前 16 位，用作 parsed 文件名指纹。"""
    canonical = json.dumps(params, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _frequency_of(dates: Sequence[str]) -> str:
    """从 observation_date 的分布推断频率。"""
    if not dates:
        return "unknown"
    years = {d[:4] for d in dates}
    months = {d[5:7] for d in dates}
    days = {d[8:10] for d in dates}
    if len(dates) == len(years):
        return "annual"
    if months <= {"01", "04", "07", "10"} and len(dates) <= 4 * len(years) + 1:
        return "quarterly"
    if days == {"01"}:
        return "monthly"
    return "daily"


def _period_label(date: str, frequency: str) -> str:
    """把 observation_date 变成 period 标签：年度 YYYY / 季度 YYYY-Qn / 月度 YYYY-MM。"""
    if frequency == "annual":
        return date[:4]
    if frequency == "quarterly":
        quarter = {"01": "Q1", "04": "Q2", "07": "Q3", "10": "Q4"}.get(date[5:7], "Q?")
        return date[:4] + "-" + quarter
    if frequency == "monthly":
        return date[:7]
    return date


def _norm_bound(value: Optional[str], *, end: bool) -> Optional[str]:
    """把 2015 / 2015-01 / 2015-01-01 统一成可比较的 ISO 上界或下界。"""
    if not value:
        return None
    s = str(value).strip()
    if len(s) == 4:
        return s + ("-12-31" if end else "-01-01")
    if len(s) == 7:
        return s + ("-31" if end else "-01")
    return s


def _parse_csv(text: str, series_id: str) -> list[tuple[str, Optional[float]]]:
    """解析 fredgraph.csv 的两列内容；(date, value) 列表，缺值是 None。"""
    reader = _csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration as exc:
        raise FredApiError(f"{series_id}: CSV 是空的") from exc
    if len(header) < 2:
        raise FredApiError(
            f"{series_id}: CSV 表头只有 {len(header)} 列（期望 observation_date,<ID>）: {header}")
    out: list[tuple[str, Optional[float]]] = []
    for row in reader:
        if len(row) < 2 or not row[0].strip():
            continue
        raw = row[1].strip()
        if raw in ("", "."):
            value: Optional[float] = None
        else:
            try:
                value = float(raw)
            except ValueError:
                value = None
        out.append((row[0].strip(), value))
    return out


def _persist(series_id: str, request: dict[str, Any], raw_resp: Any, data: Any,
             parsed_dir: Path, fetched_at: str) -> Path:
    """把解析后的数据落到 data/parsed/fred/<series_id>_<sha16>.json。

    结构与 nbs_client / worldbank_client / imf_client 逐键一致：
    request / raw_cache / data / fetched_at。
    """
    parsed_dir.mkdir(parents=True, exist_ok=True)
    out = parsed_dir / f"{series_id}_{_params_digest(request)}.json"
    payload = {
        "request": request,
        "raw_cache": str(raw_resp.cache_path) if raw_resp.cache_path else "",
        "data": data,
        "fetched_at": fetched_at,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


# --------------------------------------------------------------------------- #
# 公开接口
# --------------------------------------------------------------------------- #

def fetch_series(series_id: str, start: Optional[str] = None, end: Optional[str] = None,
                 save: bool = True,
                 parsed_dir: Optional[Path] = None) -> list[dict[str, Any]]:
    """取一条 FRED 序列（GET /graph/fredgraph.csv?id=<series_id>）。

    :param series_id: FRED 序列 ID，如 `CHNCPIALLMINMEI`（中国 CPI 全项指数，2015=100，月度）。
    :param start: 起始（含）。接受 `2015` / `2015-01` / `2015-01-01`；None 表示不限。
    :param end: 结束（含），格式同 start。
    :param save: 是否落盘（raw 走 http_client，parsed 走 data/parsed/fred/）。
    :param parsed_dir: 覆盖 parsed 落盘目录（测试用）。
    :returns: `[{"period": "2020-01", "value": 102.3}, ...]`，按 period 升序；
              缺值是 `None`。period 的粒度由频率决定（年度 YYYY / 季度 YYYY-Qn / 月度 YYYY-MM）。
    :raises FredApiError: CSV 结构不符，或序列 ID 不存在（FRED 会回 HTML 错误页）。
    """
    sid = str(series_id).strip()
    if not sid:
        raise FredApiError("series_id 不能为空")

    url = f"{CSV_ROOT}?id={urllib.parse.quote(sid)}"
    resp = http_client.get_text(url, headers=FRED_HEADERS, save=save, timeout=TIMEOUT_S)
    text = str(resp.content)
    if text.lstrip().startswith("<"):
        raise FredApiError(f"{sid}: FRED 返回了 HTML 而不是 CSV（序列 ID 不存在？）")

    pairs = _parse_csv(text, sid)
    dates = [d for d, _v in pairs]
    frequency = _frequency_of(dates)

    lo = _norm_bound(start, end=False)
    hi = _norm_bound(end, end=True)
    rows: list[dict[str, Any]] = []
    for date, value in pairs:
        if lo and date < lo:
            continue
        if hi and date > hi:
            continue
        rows.append({"period": _period_label(date, frequency), "value": value})
    rows.sort(key=lambda r: str(r["period"]))

    fetched_at = _utc_now()
    raw_cache = str(resp.cache_path) if resp.cache_path else ""
    parsed_file = ""
    if save:
        data = {
            "series_id": sid,
            "frequency": frequency,
            "observation_start": dates[0] if dates else None,
            "observation_end": dates[-1] if dates else None,
            "observations": rows,
        }
        out = _persist(sid, {"series_id": sid, "start": start, "end": end},
                       resp, data, parsed_dir or PARSED_DIR, fetched_at)
        parsed_file = str(out)
        print(f"[parsed] {out}", file=sys.stderr)

    _LAST_META.clear()
    _LAST_META.update({"fetched_at": fetched_at, "raw_cache": raw_cache,
                       "parsed_file": parsed_file, "frequency": frequency,
                       "n_rows": len(rows)})
    return rows


def list_search(query: str, limit: int = 20) -> list[dict[str, Any]]:
    """FRED 搜索**需要 api_key**，本客户端不提供 —— 恒返回空列表。

    `fredgraph.csv` 端点只支持按 series_id 直取；官方的 `/fred/series/search`
    要注册后签发的 `api_key`。本项目的约定是只用免密钥端点，所以这里不联网、
    直接返回 `[]`，并在 stderr 说明怎么绕过（自己先查到 series_id）。

    :param query: 搜索词（仅用于日志）。
    :param limit: 期望条数上限（仅用于日志）。
    :returns: 恒为 `[]`。
    """
    print(f"[info] FRED 搜索接口需要 api_key，本客户端只做免密钥直取；"
          f"请先给出 series_id（收到 query={query!r}, limit={limit}）", file=sys.stderr)
    return []


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #

#: 自检序列：中国 CPI 全项指数（OECD 派生，2015=100，月度）
FRED_TEST_SERIES = "CHNCPIALLMINMEI"


def _selftest() -> int:
    print("=" * 78)
    print("self-test: fred_client（真实端点调用 + 落盘）")
    print("=" * 78)

    print(f"\n[1] fetch_series({FRED_TEST_SERIES!r})")
    rows = fetch_series(FRED_TEST_SERIES)
    meta = last_meta()
    print(f"  返回条数  : {len(rows)}")
    print(f"  频率      : {meta.get('frequency')}")
    if rows:
        print(f"  年份范围  : {str(rows[0]['period'])[:4]} - {str(rows[-1]['period'])[:4]}")
        print(f"  首条      : {json.dumps(rows[0], ensure_ascii=False)}")
        print(f"  末条      : {json.dumps(rows[-1], ensure_ascii=False)}")
    print(f"  非空值个数: {sum(1 for r in rows if r['value'] is not None)}")

    print("\n[2] 2020-12 那一条")
    target = next((r for r in rows if r["period"] == "2020-12"), None)
    print("  " + json.dumps(target, ensure_ascii=False))

    print("\n[3] 口径核对（2015=100 指数）")
    first = next((r for r in rows if r["period"] == "1993-01"), None)
    base = next((r for r in rows if r["period"] == "2015-01"), None)
    print(f"  1993-01 = {first['value'] if first else None}")
    print(f"  2015-01 = {base['value'] if base else None}")

    print("\n[4] list_search('china cpi') —— 预期返回空列表")
    hits = list_search("china cpi")
    print(f"  返回条数: {len(hits)}")

    checks = [
        ("返回条数 > 300", len(rows) > 300),
        ("频率识别为 monthly", meta.get("frequency") == "monthly"),
        ("2020-12 有具体值", target is not None and target.get("value") is not None),
        ("指数单调上行（1993-01 < 2015-01）",
         bool(first and base and first.get("value") is not None
              and base.get("value") is not None
              and float(first["value"]) < float(base["value"]))),
        ("list_search 返回空列表", hits == []),
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

    p = argparse.ArgumentParser(prog="fred_client",
                                description="FRED（fredgraph.csv 免密钥端点）客户端")
    p.add_argument("--test", action="store_true", help="跑自检（真实端点调用）")
    a = p.parse_args(argv)
    if a.test:
        try:
            return _selftest()
        except FredApiError as exc:
            print(f"[FAIL] {exc}", file=sys.stderr)
            return 2
    p.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())

