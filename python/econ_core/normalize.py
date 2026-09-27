#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""规范化层：把 nbs_client 的原始输出转成统一长表（每行一条观测）。

数据流
------
    data/raw/_http_cache/*.bin          （http_client 存档，逐字节原始响应）
        -> nbs_client.fetch_indicator_data()   （解析 JSON，取 data 数组）
        -> normalize_observations()            （本模块：重命名 + 类型转换，不丢数据）
        -> data/validated/nbs/<name>.json / .parquet

设计原则
--------
1. **规范化不丢数据**，只做重命名与类型转换。原始 dict 中除 ``v`` / ``unit`` 外的
   全部字段（``dt`` / ``dt_name`` / ``du`` / ``code`` / ``i`` / ``i_name`` /
   ``du_name`` 以及未来新增的任何字段）原样进 ``raw_fields`` 列（JSON 字符串）。
   这样即使后端改了字段语义，证据仍在。
2. **无法解析就抛错，不静默兜底**。时间格式不认识、数值转不动，一律 ``ValueError``
   并在消息里带上原始字符串，由调用方决定怎么处理。
3. 每一行自带 ``row_sha16``，可单独校验与去重；``raw_cache`` 指回 raw 层文件。

目标 schema（每行一条观测）
---------------------------
===============  =======  ==================================================
字段              类型      说明
===============  =======  ==================================================
region_code      str      地区代码，如 "000000000000"
region_name      str      地区名称（NBS 只返回 code，由调用方传入）
indicator_id     str      指标语义标识（原始 ``i`` 字段）
tree_node_id     str      取数用的树节点 id（请求参数 ``id``，不来自响应体）
indicator_name   str      指标名称（原始 ``i_name``；同 indicator_id 的行共享同一名称，
                          上游返回 null 时由 :func:`_fill_indicator_names` 补齐）
period           str      标准时间："2015" / "2020-01" / "2020-Q1"
period_type      str      "annual" | "quarterly" | "monthly"
value            float    数值（原始 ``v`` 转 float，空值 -> None）
unit             str      单位，如 "亿元" / "%"
source           str      固定 "NBS"
fetched_at       str      ISO8601 抓取时间
raw_cache        str      raw 存档路径
row_sha16        str      本行规范化后 JSON 的 sha256 前 16 位
raw_fields       str      原始 dict（除 v/unit）的 JSON 字符串
===============  =======  ==================================================

``indicator_id`` vs ``tree_node_id``（**这两个值不同，不要混用**）
------------------------------------------------------------------
实测同一指标会同时出现两个标识，语义完全不同：

* ``indicator_id`` = 响应体 ``i`` 字段，如 ``db8e5a86c08246e79b1b11251927e740``。
  是后端的**指标语义标识**，跨请求稳定，适合做键、做交叉验证、做跨期拼接。
* ``tree_node_id`` = 请求参数 ``id``，如 ``7dc6a2ee6c614960b7059991e0cc4d96``。
  是**取数时必须回传**的那个值（``fetch_indicator_data(indicator_id=...)`` 实际收的是它）。

也就是说：``tree_node_id`` 才能回填给取数接口，``indicator_id`` 不能。
``tree_node_id`` 来自请求而非响应，故由 ``source_meta["tree_node_id"]`` 传入，
并额外以 ``_request_tree_node_id`` 键写进 ``raw_fields`` 留痕。

时间解析规则
------------
=========================  ====================  =============
原始                        规范化                 period_type
=========================  ====================  =============
``2015YY`` / ``2015年``     ``2015``              annual
``2020MM``                  ``2020-01``           monthly  （只有年，月默认 01）
``202001MM``                ``2020-01``           monthly
``2020年01月`` / ``2020年1月``  ``2020-01``        monthly
``2020Q1``                  ``2020-Q1``           quarterly
``20201SS``                 ``2020-Q1``           quarterly
``202001MM-202012MM``       抛 ValueError        （区间，要求调用方分段请求）
其他                         抛 ValueError（含原始串）
=========================  ====================  =============

用法
----
    python -m econ_core.normalize --test
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from econ_core import http_client, nbs_client  # type: ignore[no-redef]
else:
    from . import http_client, nbs_client

__all__ = [
    "NormalizeError",
    "parse_period",
    "parse_value",
    "normalize_observations",
    "normalize_worldbank_observations",
    "source_meta_from_parsed",
    "source_meta_from_parsed_worldbank",
    "write_validated",
    "write_validated_parquet",
    "VALIDATED_DIR",
    "PARSED_NBS_DIR",
    "PARSED_WORLDBANK_DIR",
    "SOURCE",
    "SOURCE_WB",
]

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

SOURCE: str = "NBS"

#: nbs_client 的 parsed 落盘目录（用于回填 raw_cache / fetched_at）
PARSED_NBS_DIR: Path = http_client.PROJECT_ROOT / "data" / "parsed" / "nbs"

#: World Bank 数据源标识（第二阶段新增）
SOURCE_WB: str = "WorldBank"

#: worldbank_client 的 parsed 落盘目录（用于回填 raw_cache / fetched_at）
PARSED_WORLDBANK_DIR: Path = http_client.PROJECT_ROOT / "data" / "parsed" / "worldbank"

#: World Bank 观测里被视为"已被消费"的原始字段：其余全部进 raw_fields
WB_CONSUMED_KEYS: frozenset[str] = frozenset({"value", "unit"})

#: 本模块的 validated 落盘目录
VALIDATED_DIR: Path = http_client.PROJECT_ROOT / "data" / "validated" / "nbs"

#: 视为"已被消费"的原始字段：其余全部进 raw_fields
CONSUMED_KEYS: frozenset[str] = frozenset({"v", "unit"})

#: raw_fields 中需要剔除的原始键（业务上不该出现在证据里的噪声字段）
RAW_FIELDS_DROP: frozenset[str] = frozenset({"raw_fields", "row_sha16"})


class NormalizeError(RuntimeError):
    """规范化层异常（时间/数值无法解析、缺依赖、写盘失败等）。"""


# --------------------------------------------------------------------------- #
# 时间解析
# --------------------------------------------------------------------------- #

_RE_ANNUAL = re.compile(r"^(\d{4})(?:YY|年)$")
_RE_MONTHLY_YYYYMM = re.compile(r"^(\d{6})MM$")
_RE_MONTHLY_YYYY = re.compile(r"^(\d{4})MM$")
_RE_MONTHLY_CN = re.compile(r"^(\d{4})年(\d{1,2})月$")
_RE_QUARTER_Q = re.compile(r"^(\d{4})Q([1-4])$")
_RE_QUARTER_SS = re.compile(r"^(\d{4})([1-4])SS$")

_SUPPORTED_HINT = (
    "支持格式：年度 '2015YY' / '2015年'；月度 '2020MM' / '202001MM' / '2020年01月'；"
    "季度 '2020Q1' / '20201SS'"
)


def _parse_single_period(raw: str) -> Optional[tuple[str, str]]:
    """尝试把单个时间标记解析为 (period, period_type)；不匹配返回 None。"""
    m = _RE_ANNUAL.match(raw)
    if m:
        return m.group(1), "annual"

    m = _RE_MONTHLY_YYYYMM.match(raw)
    if m:
        s = m.group(1)
        year, month = s[:4], int(s[4:6])
        if not 1 <= month <= 12:
            raise ValueError(f"月份越界: {raw!r} 解析出 month={month}")
        return f"{year}-{month:02d}", "monthly"

    m = _RE_MONTHLY_YYYY.match(raw)
    if m:
        # 只有年份，月默认 01（与 NBS '2020MM' 表示法一致）
        return f"{m.group(1)}-01", "monthly"

    m = _RE_MONTHLY_CN.match(raw)
    if m:
        year, month = m.group(1), int(m.group(2))
        if not 1 <= month <= 12:
            raise ValueError(f"月份越界: {raw!r} 解析出 month={month}")
        return f"{year}-{month:02d}", "monthly"

    m = _RE_QUARTER_Q.match(raw)
    if m:
        return f"{m.group(1)}-Q{m.group(2)}", "quarterly"

    m = _RE_QUARTER_SS.match(raw)
    if m:
        return f"{m.group(1)}-Q{m.group(2)}", "quarterly"

    return None


def parse_period(raw: Any) -> tuple[str, str]:
    """把 NBS 的时间标记解析为 (period, period_type)。

    :param raw: 原始时间标记，如 ``"2020YY"`` / ``"2020年01月"`` / ``"2020Q1"``。
    :returns: ``(period, period_type)``，例如 ``("2020", "annual")``、
              ``("2020-01", "monthly")``、``("2020-Q1", "quarterly")``。
    :raises NormalizeError: 传入为空 / 不是字符串 / 是区间 / 无法识别的格式。
    """
    if raw is None:
        raise NormalizeError("时间字段为空（None），无法解析")
    if not isinstance(raw, str):
        raise NormalizeError(f"时间字段不是字符串: {type(raw).__name__} -> {raw!r}")
    s = raw.strip()
    if not s:
        raise NormalizeError("时间字段为空字符串，无法解析")

    hit = _parse_single_period(s)
    if hit is not None:
        return hit

    # 区间检测：两侧都能单独解析，说明调用方把一段区间塞进来了
    if "-" in s:
        left, _, right = s.partition("-")
        try:
            if _parse_single_period(left.strip()) and _parse_single_period(right.strip()):
                raise NormalizeError(
                    f"检测到时间区间 {raw!r}，本模块不拆分区间。"
                    f"请调用方按单期分段请求（例如按年循环，或让 dts 只含单期）。"
                )
        except NormalizeError:
            raise
        except ValueError:
            pass

    raise NormalizeError(f"无法识别的时间格式: {raw!r}。{_SUPPORTED_HINT}")


# --------------------------------------------------------------------------- #
# 数值解析
# --------------------------------------------------------------------------- #

def parse_value(raw: Any) -> Optional[float]:
    """把原始 ``v`` 字段解析为 float。

    :param raw: 原始值。``None`` 或空字符串 ``""`` 视为缺失，返回 ``None``。
    :returns: ``float`` 或 ``None``。
    :raises NormalizeError: 字符串无法转 float，或转出 NaN/Inf（会污染下游统计）。
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise NormalizeError(f"数值字段是布尔值，不符合预期: {raw!r}")
    if isinstance(raw, (int, float)):
        f = float(raw)
    else:
        s = str(raw).strip()
        if s == "":
            return None
        try:
            f = float(s)
        except (TypeError, ValueError) as exc:
            raise NormalizeError(f"数值无法转 float: {raw!r}") from exc

    if not math.isfinite(f):
        raise NormalizeError(f"数值为非有限值（NaN/Inf），拒绝写入 validated 层: {raw!r}")
    return f


# --------------------------------------------------------------------------- #
# 行指纹
# --------------------------------------------------------------------------- #

def _row_sha16(row: dict[str, Any]) -> str:
    """本行（不含 row_sha16 字段）规范化 JSON 的 sha256 前 16 位。"""
    payload = {k: v for k, v in row.items() if k != "row_sha16"}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# 缺失指标名补齐
# --------------------------------------------------------------------------- #

def _fill_indicator_names(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """让同一 indicator_id 的所有行共享同一个 indicator_name（原地写回并返回 rows）。

    背景
    ----
    NBS 后端会有记录在 `i_name` 上返回 `null`（实测 2024 年 GDP 行的
    `i_name` 就是 null，见 data/parsed/nbs/ 与 python/_probes/），但同一
    `indicator_id` 的其它行带着完整名称，因此名称可以安全地组内补齐。

    规则
    ----
    1. 组内取**首个非空** indicator_name 作为该组的规范名称（等价于前向填充，
       并对"组首即缺失"的情况前向找到组内首个非空值）；
    2. 若**整组**都没有非空名称，则用 `indicator_id` 兜底；
    3. 只改 indicator_name 一个字段：不删行、不丢其它字段、保持行顺序。

    注意
    ----
    本函数改了字段内容，调用方必须在**之后**重算 `row_sha16`，
    否则行指纹与实际内容失配。

    :param rows: normalize_observations 组装的行列表（函数内原地修改）。
    :returns: 同一个列表对象。
    """
    canonical: dict[str, str] = {}
    for row in rows:
        iid = str(row.get("indicator_id") or "")
        if iid in canonical:
            continue
        name = row.get("indicator_name")
        if name not in (None, ""):
            canonical[iid] = str(name)

    for row in rows:
        iid = str(row.get("indicator_id") or "")
        filled = canonical.get(iid) or iid
        if filled:
            row["indicator_name"] = filled
    return rows


# --------------------------------------------------------------------------- #
# 主转换
# --------------------------------------------------------------------------- #

def normalize_observations(raw_data: Iterable[dict[str, Any]],
                           source_meta: dict[str, Any]) -> list[dict[str, Any]]:
    """把 nbs_client 返回的 ``data`` 数组转成规范化长表。

    :param raw_data: nbs_client 各接口返回的 ``data`` 数组，每个元素是一条观测，
                     至少含 ``dt``（时间）与 ``v``（数值）。缺 ``dt`` 时回退用 ``dt_name``。
    :param source_meta: 来源元数据，支持的键：

                        * ``region_name``   (str)  —— 地区名称，NBS 不返回，由调用方传入；缺省 ``""``
                        * ``tree_node_id``  (str)  —— 取数请求里的 ``id``；缺省 ``""``（见模块 docstring）
                        * ``fetched_at``    (str)  —— ISO8601；缺省取当前 UTC 时间
                        * ``raw_cache``     (str)  —— raw 存档路径；缺省 ``""``
                        * ``source``        (str)  —— 覆盖默认 ``"NBS"``
                        * ``region_code``   (str)  —— 覆盖行内 ``code``（一般不需要）

    :returns: 规范化后的行列表，字段顺序见模块 docstring。
    :raises NormalizeError: 任一行的时间或数值无法解析（消息内带原始值）。
    """
    region_name = str(source_meta.get("region_name", "") or "")
    tree_node_id = str(source_meta.get("tree_node_id", "") or "")
    fetched_at = str(source_meta.get("fetched_at", "") or _utc_now())
    raw_cache = str(source_meta.get("raw_cache", "") or "")
    source = str(source_meta.get("source", "") or SOURCE)
    region_code_override = source_meta.get("region_code")

    rows: list[dict[str, Any]] = []
    for idx, item in enumerate(raw_data):
        if not isinstance(item, dict):
            raise NormalizeError(f"第 {idx} 条观测不是 dict: {type(item).__name__} -> {item!r}")

        # 时间：优先 dt，回退 dt_name
        dt_raw = item.get("dt")
        if dt_raw in (None, ""):
            dt_raw = item.get("dt_name")
            if dt_raw in (None, ""):
                raise NormalizeError(f"第 {idx} 条观测缺少时间字段（dt / dt_name）: {item!r}")
        try:
            period, period_type = parse_period(dt_raw)
        except NormalizeError as exc:
            raise NormalizeError(f"第 {idx} 条观测时间解析失败: {exc}") from exc

        try:
            value = parse_value(item.get("v"))
        except NormalizeError as exc:
            raise NormalizeError(f"第 {idx} 条观测数值解析失败: {exc}") from exc

        raw_fields = {
            k: v for k, v in item.items()
            if k not in CONSUMED_KEYS and k not in RAW_FIELDS_DROP
        }
        # 请求态上下文（不在响应体里）也留痕：'_' 前缀标明这是注入键，非 NBS 原始字段
        if tree_node_id:
            raw_fields = {**raw_fields, "_request_tree_node_id": tree_node_id}

        row: dict[str, Any] = {
            "region_code": str(region_code_override if region_code_override is not None
                               else (item.get("code") or "")),
            "region_name": region_name,
            "indicator_id": str(item.get("i") or ""),
            "tree_node_id": tree_node_id,
            "indicator_name": item.get("i_name"),
            "period": period,
            "period_type": period_type,
            "value": value,
            "unit": str(item.get("unit") or ""),
            "source": source,
            "fetched_at": fetched_at,
            "raw_cache": raw_cache,
            "row_sha16": "",
            "raw_fields": json.dumps(raw_fields, ensure_ascii=False, sort_keys=True),
        }
        row["row_sha16"] = _row_sha16(row)
        rows.append(row)

    # 补齐 indicator_name（上游 i_name 可能为 null）后重算行指纹，保证指纹与内容一致
    _fill_indicator_names(rows)
    for row in rows:
        row["row_sha16"] = _row_sha16(row)

    return rows


# --------------------------------------------------------------------------- #
# 来源元数据桥接
# --------------------------------------------------------------------------- #

def source_meta_from_parsed(endpoint: str, request_params: dict[str, Any],
                            region_name: str = "",
                            tree_node_id: str = "",
                            parsed_dir: Optional[Path] = None) -> dict[str, Any]:
    """从 nbs_client 落盘的 parsed 文件回填 ``raw_cache`` / ``fetched_at``。

    ``nbs_client`` 各接口目前只返回 ``data`` 数组、不返回元数据，但它在
    ``data/parsed/nbs/<endpoint>_<sha16>.json`` 里记录了 ``raw_cache`` 与
    ``fetched_at``。本函数按 ``request`` 字段比对定位该文件（不复制其指纹算法，
    避免规则漂移），供自检与流水线拼接 source_meta 使用。

    :param endpoint: 接口名，如 ``"getEsDataByIndicatorIdAndDa"``。
    :param request_params: 与调用 nbs_client 时**完全相同**的参数 dict。
    :param region_name: 地区名称，透传进返回的 meta。
    :param tree_node_id: 取数请求里的 ``id``（见模块 docstring），透传进返回的 meta。
    :param parsed_dir: 覆盖 parsed 目录（测试用）。
    :returns: 可直接喂给 :func:`normalize_observations` 的 source_meta。
    :raises NormalizeError: 找不到匹配的 parsed 文件。
    """
    d = parsed_dir or PARSED_NBS_DIR
    candidates = sorted(d.glob(f"{endpoint}_*.json")) if d.is_dir() else []
    for f in candidates:
        try:
            obj = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if obj.get("request") == request_params:
            return {
                "region_name": region_name,
                "tree_node_id": tree_node_id,
                "fetched_at": obj.get("fetched_at", ""),
                "raw_cache": obj.get("raw_cache", ""),
                "source": SOURCE,
                "parsed_file": str(f),
            }
    raise NormalizeError(
        f"未在 {d} 找到 request 匹配的 parsed 文件（endpoint={endpoint}）。"
        f"请确认已用相同参数调用过 nbs_client，且 save=True。"
    )


# --------------------------------------------------------------------------- #
# World Bank 规范化与 meta 桥接（第二阶段新增；上方 NBS 路径逐字未动）
# --------------------------------------------------------------------------- #

#: World Bank 年度 date 是裸年份（"2020"），需补成 NBS 记法再复用 parse_period
_RE_WB_BARE_YEAR = re.compile(r"^\d{4}$")


def normalize_worldbank_observations(raw_data: Iterable[dict[str, Any]],
                                     source_meta: dict[str, Any]) -> list[dict[str, Any]]:
    """把 worldbank_client 返回的 `data` 数组转成与 NBS 同形的规范化长表。

    与 `:func:normalize_observations` 的差异（World Bank 的记录形状不同）：

    * 时间是 `date` 字段，年度就是**裸年份** `2020`（没有 NBS 的 YY 后缀），
      这里补成 `2020YY` 再复用 `:func:parse_period`；
    * 指标在 `indicator.{id,value}`，地区在 `country.{id,value}` 与
      `countryiso3code`；
    * 没有 tree_node_id 概念，该列固定空串；
    * `raw_fields` 保留除 `value` / `unit` 外的全部原始键（含 `date` /
      `indicator` / `country` / `decimal` 等）。

    :param raw_data: worldbank_client 各接口返回的 data 数组。
    :param source_meta: 支持 `region_name` / `fetched_at` / `raw_cache` /
                        `source`（缺省 `WorldBank`）。
    :returns: 与 NBS 同字段顺序的长表行。
    :raises NormalizeError: 任一行时间或数值无法解析。
    """
    region_name = str(source_meta.get("region_name", "") or "")
    fetched_at = str(source_meta.get("fetched_at", "") or _utc_now())
    raw_cache = str(source_meta.get("raw_cache", "") or "")
    source = str(source_meta.get("source", "") or SOURCE_WB)

    rows: list[dict[str, Any]] = []
    for idx, item in enumerate(raw_data):
        if not isinstance(item, dict):
            raise NormalizeError(f"第 {idx} 条观测不是 dict: {type(item).__name__} -> {item!r}")

        raw_date = item.get("date")
        if raw_date in (None, ""):
            raise NormalizeError(f"第 {idx} 条观测缺少时间字段（date）: {item!r}")
        s = str(raw_date).strip()
        if _RE_WB_BARE_YEAR.match(s):
            s = s + "YY"
        try:
            period, period_type = parse_period(s)
        except NormalizeError as exc:
            raise NormalizeError(f"第 {idx} 条观测时间解析失败: {exc}") from exc

        try:
            value = parse_value(item.get("value"))
        except NormalizeError as exc:
            raise NormalizeError(f"第 {idx} 条观测数值解析失败: {exc}") from exc

        indicator = item.get("indicator")
        indicator = indicator if isinstance(indicator, dict) else {}
        country = item.get("country")
        country = country if isinstance(country, dict) else {}

        raw_fields = {k: v for k, v in item.items() if k not in WB_CONSUMED_KEYS}

        row: dict[str, Any] = {
            "region_code": str(item.get("countryiso3code") or country.get("id") or ""),
            "region_name": region_name or str(country.get("value") or ""),
            "indicator_id": str(indicator.get("id") or ""),
            "tree_node_id": "",
            "indicator_name": indicator.get("value"),
            "period": period,
            "period_type": period_type,
            "value": value,
            "unit": str(item.get("unit") or ""),
            "source": source,
            "fetched_at": fetched_at,
            "raw_cache": raw_cache,
            "row_sha16": "",
            "raw_fields": json.dumps(raw_fields, ensure_ascii=False, sort_keys=True),
        }
        row["row_sha16"] = _row_sha16(row)
        rows.append(row)

    return rows


def source_meta_from_parsed_worldbank(endpoint: str, request_params: dict[str, Any],
                                      region_name: str = "",
                                      parsed_dir: Optional[Path] = None) -> dict[str, Any]:
    """从 worldbank_client 落盘的 parsed 文件回填 `raw_cache` / `fetched_at`。

    与 `:func:source_meta_from_parsed` 同构，只是把目录换成
    `data/parsed/worldbank/`、source 换成 `WorldBank`；
    `request_params` 须与调用 worldbank_client 时完全一致
    （用 `worldbank_client.indicator_request(...)` 构造即可）。

    :raises NormalizeError: 找不到匹配的 parsed 文件。
    """
    d = parsed_dir or PARSED_WORLDBANK_DIR
    candidates = sorted(d.glob(f"{endpoint}_*.json")) if d.is_dir() else []
    for f in candidates:
        try:
            obj = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if obj.get("request") == request_params:
            return {
                "region_name": region_name,
                "fetched_at": obj.get("fetched_at", ""),
                "raw_cache": obj.get("raw_cache", ""),
                "source": SOURCE_WB,
                "parsed_file": str(f),
            }
    raise NormalizeError(
        f"未在 {d} 找到 request 匹配的 parsed 文件（endpoint={endpoint}）。"
        f"请确认已用相同参数调用过 worldbank_client，且 save=True。"
    )


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# --------------------------------------------------------------------------- #
# 落盘
# --------------------------------------------------------------------------- #

def write_validated(rows: list[dict[str, Any]], name: str,
                    out_dir: Optional[Path] = None) -> Path:
    """把规范化行写成 JSON 到 ``data/validated/nbs/<name>.json``。

    文件结构（带元信息的信封，便于校验）::

        {"name": ..., "row_count": ..., "written_at": ..., "columns": [...], "rows": [...]}

    :param rows: :func:`normalize_observations` 的输出。
    :param name: 文件名主干（不含扩展名）。
    :param out_dir: 覆盖输出目录（测试用）。
    :returns: 写入的文件路径。
    """
    d = out_dir or VALIDATED_DIR
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{name}.json"
    envelope = {
        "name": name,
        "row_count": len(rows),
        "written_at": _utc_now(),
        "columns": list(rows[0].keys()) if rows else [],
        "rows": rows,
    }
    path.write_text(json.dumps(envelope, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def write_validated_parquet(rows: list[dict[str, Any]], name: str,
                            out_dir: Optional[Path] = None) -> Path:
    """把规范化行写成 Parquet 到 ``data/validated/nbs/<name>.parquet``。

    依赖 pandas + pyarrow（本沙箱需用 pip_sandbox_install.py 安装，原因见该脚本 docstring）。

    注意：Parquet 无 ``None`` 字符串语义，``indicator_name=None`` 等空值回读后会变成
    ``NaN``；**严格保真场景请读 JSON 版本**（``write_validated`` 的产物），
    Parquet 版本定位是"分析就绪"，不承担保真职责。

    :param rows: :func:`normalize_observations` 的输出。
    :param name: 文件名主干（不含扩展名）。
    :param out_dir: 覆盖输出目录（测试用）。
    :returns: 写入的文件路径。
    :raises NormalizeError: 缺少 pandas / pyarrow。
    """
    try:
        import pandas as pd  # noqa: PLC0415
    except ImportError as exc:
        raise NormalizeError(
            "写 Parquet 需要 pandas 与 pyarrow，但当前解释器缺少依赖。"
            "本沙箱 pip 无法直接安装（tempfile.mkdtemp 权限问题），"
            "请使用：.\\\\.venv\\\\Scripts\\\\python.exe pip_sandbox_install.py install pandas pyarrow"
        ) from exc

    d = out_dir or VALIDATED_DIR
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{name}.parquet"

    df = pd.DataFrame(rows)
    try:
        df.to_parquet(path, engine="pyarrow", index=False)
    except ImportError as exc:  # pyarrow 缺失
        raise NormalizeError(
            "写 Parquet 需要 pyarrow 引擎，请先安装："
            ".\\\\.venv\\\\Scripts\\\\python.exe pip_sandbox_install.py install pyarrow"
        ) from exc
    return path


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #

#: 自检用参数（来自 python/_probes/ 的探测结论）
SELFTEST_REQUEST: dict[str, Any] = {
    "cid": "f7fd25aaad184414875632cf2327da60",
    "indicator_id": "7dc6a2ee6c614960b7059991e0cc4d96",
    "root_id": "71d41888d5a44bb2a67402ef4e60003e",
    "da": "000000000000",
    "dts": [f"{y}YY" for y in range(2015, 2025)],
    "region_name": "全国",
    "tree_node_id": "7dc6a2ee6c614960b7059991e0cc4d96",
}
_EXPECT_2020 = 1034867.6


def _selftest() -> int:
    failures: list[str] = []
    req = SELFTEST_REQUEST

    print("=" * 74)
    print("normalize 自检")
    print("=" * 74)

    # 1) 取数
    print(f"[1] fetch_indicator_data  dts={req['dts'][0]}..{req['dts'][-1]} 共 {len(req['dts'])} 期")
    raw_data = nbs_client.fetch_indicator_data(
        cid=req["cid"], indicator_id=req["indicator_id"], root_id=req["root_id"],
        da=req["da"], dts=req["dts"],
    )
    print(f"    原始观测条数: {len(raw_data)}")

    # 2) 规范化
    req_params = {"cid": req["cid"], "id": req["indicator_id"], "da": req["da"],
                  "dt": "", "rootId": req["root_id"], "dts": req["dts"]}
    meta = source_meta_from_parsed("getEsDataByIndicatorIdAndDa", req_params,
                                   region_name=req["region_name"],
                                   tree_node_id=req["tree_node_id"])
    print(f"[2] source_meta: raw_cache={meta['raw_cache']}")
    print(f"                fetched_at={meta['fetched_at']}")
    rows = normalize_observations(raw_data, meta)
    print(f"    规范化行数: {len(rows)}")

    # 3) 写 JSON
    name = "selftest_gdp_annual_2015_2024"
    p_json = write_validated(rows, name)
    print(f"[3] JSON    -> {p_json}  ({p_json.stat().st_size:,} 字节)")

    # 4) 写 Parquet
    p_pq = write_validated_parquet(rows, name)
    print(f"[4] Parquet -> {p_pq}  ({p_pq.stat().st_size:,} 字节)")

    # 5) 断言与打印
    print("-" * 74)
    print(f"[5] 行数: {len(rows)}  (期望 10)")
    if len(rows) != 10:
        failures.append(f"行数期望 10，实际 {len(rows)}")

    print(f"    首行: {json.dumps(rows[0], ensure_ascii=False)}")
    print(f"    末行: {json.dumps(rows[-1], ensure_ascii=False)}")

    v2020 = next((r["value"] for r in rows if r["period"] == "2020"), None)
    print(f"    2020 value = {v2020!r}  类型={type(v2020).__name__}   (float 比较，期望 {_EXPECT_2020})")
    if not isinstance(v2020, float) or v2020 != _EXPECT_2020:
        failures.append(f"2020 value 期望 float {_EXPECT_2020}，实际 {type(v2020).__name__} {v2020!r}")
    else:
        print("    ✔ 2020 value 是 float 且等于 1034867.6")

    r2024 = next((r for r in rows if r["period"] == "2024"), None)
    if r2024 is None:
        failures.append("未找到 2024 行")
    else:
        rf = json.loads(r2024["raw_fields"])
        print(f"    2024 indicator_name = {r2024['indicator_name']!r}  (原始 i_name 为 null，已组内补齐)")
        print(f"    2024 raw_fields     = {r2024['raw_fields']}")
        if r2024["indicator_name"] != "国内生产总值":
            failures.append(
                f"2024 indicator_name 期望 '国内生产总值'，实际 {r2024['indicator_name']!r}"
            )
        elif "i_name" not in rf or rf["i_name"] is not None:
            failures.append("2024 raw_fields 未保留原始 i_name 键")
        else:
            print("    ✔ 2024 行 indicator_name 已补齐为 '国内生产总值'，"
                  "且 raw_fields 保留了原始 i_name=null")

    # 同一 indicator_id 的名称必须统一（本数据集只有 GDP 一个指标）
    names = sorted({str(r["indicator_name"]) for r in rows})
    if names != ["国内生产总值"]:
        failures.append(f"同 indicator_id 的 indicator_name 未统一: {names}")
    else:
        print(f"    ✔ {len(rows)} 行 indicator_name 已统一为 {names[0]!r}")

    # 列完整性
    expected_cols = ["region_code", "region_name", "indicator_id", "tree_node_id",
                     "indicator_name", "period", "period_type", "value", "unit",
                     "source", "fetched_at", "raw_cache", "row_sha16", "raw_fields"]
    if list(rows[0].keys()) != expected_cols:
        failures.append(f"列顺序/名称不符: {list(rows[0].keys())}")
    else:
        print(f"    ✔ 列共 {len(expected_cols)} 个，与 schema 一致: {expected_cols}")

    # indicator_id 与 tree_node_id 必须分离
    r0 = rows[0]
    if r0["indicator_id"] == r0["tree_node_id"]:
        failures.append("indicator_id 与 tree_node_id 未分离")
    elif r0["tree_node_id"] != req["tree_node_id"]:
        failures.append(f"tree_node_id 期望 {req['tree_node_id']}，实际 {r0['tree_node_id']}")
    else:
        print(f"    ✔ indicator_id({r0['indicator_id'][:8]}…) != tree_node_id({r0['tree_node_id'][:8]}…) 已分离")
        print(f"       raw_fields 含 _request_tree_node_id: "
              f"{json.loads(r0['raw_fields']).get('_request_tree_node_id') == req['tree_node_id']}")

    # row_sha16 唯一性
    shas = {r["row_sha16"] for r in rows}
    if len(shas) != len(rows):
        failures.append(f"row_sha16 有重复: {len(shas)}/{len(rows)}")
    else:
        print(f"    ✔ row_sha16 全部唯一（{len(shas)} 个）")

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

    p = argparse.ArgumentParser(prog="normalize", description="NBS 观测数据规范化层")
    p.add_argument("--test", action="store_true", help="运行自检")
    args = p.parse_args(argv)
    if args.test:
        return _selftest()
    p.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
