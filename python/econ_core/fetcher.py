#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""取数适配层（fetcher）—— catalog 配置 -> 各源 client -> **统一形状的行**。

为什么需要它
------------
`catalog.py` 知道「CPI 在 nbs 上要哪三个 UUID、在 bis 上要哪个 dataset+key」，
但它不取数；五个 `*_client.py` 会取数，但各家的调用方式、返回结构、时间标签、
频率语义全不一样：

| 源 | 怎么要时间 | 回来的时间标签 | 回来的数值字段 |
|---|---|---|---|
| nbs | `dts=["2020YY"]`（**必须显式给全**） | `dt="2020YY"`、`dt_name="2020年"` | `v`（**字符串**，占位行是空串） |
| worldbank | `date_range="2020:2024"` | `date="2020"` | `value`（可能是 `null`） |
| imf | **给不了**，一次返回全部年份 | `period="2020"` | `value` |
| fred | `start=` / `end=` | `period="2020-01"` | `value` |
| bis | **给不了**，一次返回整条序列 | `period="2020-01"` | `value` |

本模块就是这一层适配：**输入指标名 + 源名，输出同样形状的行**，
让上层（`tools/edh.py`、下一轮的导出链路）不必知道任何一家的怪癖。

行形状（9 个字段，顺序固定，与 `edh fetch` 的 CSV 列一致）
----------------------------------------------------------
    indicator / source / region / period / period_type / value / unit / frequency / series_name

三个接口
--------
`fetch_indicator(indicator, source=None, ...)`   取一个源（或全部源）的行
`fetch_all_sources(indicator, ...)`             按源分组返回 `{source: [rows]}`
`cross_check(rows_by_source, ...)`              两两比对（给 `--cross-check` 用）

`last_notes()` 记录**每个源这一轮到底发生了什么**（成功 / 跳过 / 失败），
沿用五个 client 的 `last_meta()` 惯例 —— 用户看到的简报就是它渲染出来的。

几个刻意的决定
--------------
* **月度年化取"年均值"**，与项目既有链路一致（FRED CPI 就是这么进的 validated）。
  年化后 `period_type` 变 `annual`、`frequency` 变 `annual`，**并保留原始月数**到
  `aggregated_from_months`（同 `scan-missing.py` 的字段名，不是新发明的）。
* **NBS 的占位行照收不误**（`v` 是空串 -> `value=None`）。理由同 §2.9「缺失行必须保留」：
  用户要能看见"这一年官方没发布"，而不是看见一个比真实年份少的表。
  简报里的「空缺」行数就是它。
* **IMF 含预测值**：默认**截到今年**。`fetch_indicator(..., allow_forecast=True)`
  才会把未来年份放出来（这是 IMF 数据的已知特性，见 PROJECT_STATE §3.8）。
* **fred / bis 的序列是国别固定的**（`CHNCPIALLMINMEI`、`M.CN.771` 里已经写死了国家）。
  所以 `region` 不是 CHN 时对这两个源**直接报错**，而不是返回中国的数却标成别国。
* `--cross-check` **不替用户做单位换算**。不同源的同名指标常常不同口径
  （NBS 是「上年=100」指数、FRED 是「2015=100」指数），直接算差异率会得到一个
  看着很大、其实毫无意义的数。`cross_check()` 因此**同时返回两侧的 unit 与
  `unit_match` 标志**，让简报能说"这两个不是一回事"，而不是硬报一个"冲突"。
  （真正要做口径对齐时，正例见 `tools/compare-cpi.py`：把 FRED 月度指数 -> 年均值
  -> 换算成「上年=100」，两边同基后再比。）

用法
----
    .\\.venv\\Scripts\\python.exe -m econ_core.fetcher --test
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from econ_core import (  # type: ignore[no-redef]
        bis_client, catalog, cross_validation, fred_client, imf_client,
        nbs_client, normalize, worldbank_client,
    )
else:
    from . import (
        bis_client, catalog, cross_validation, fred_client, imf_client,
        nbs_client, normalize, worldbank_client,
    )

__all__ = [
    "FetcherError",
    "ROW_FIELDS",
    "fetch_indicator",
    "fetch_all_sources",
    "cross_check",
    "annualize_rows",
    "last_notes",
    "reset_notes",
    "default_window",
]

#: 统一行的字段顺序（= `edh fetch` 的 CSV 列顺序，两边必须一致）。
ROW_FIELDS: tuple[str, ...] = (
    "indicator", "source", "region", "period", "period_type",
    "value", "unit", "frequency", "series_name",
)

#: NBS 的「全国」地区代码（`da` 参数）。**不是** "CHN" —— `da` 要的是 12 位数字码。
#: 其它省份要 `nbs_client.list_provinces()` 映射，本轮目录里只有全国口径。
NBS_REGION_DA: dict[str, str] = {"CHN": "000000000000"}

#: fred / bis 的序列 ID 里已经写死了国家，只支持中国。
COUNTRY_FIXED_SOURCES: tuple[str, ...] = ("fred", "bis")

#: 不给 --from/--to 时的默认窗口长度（年）。
DEFAULT_WINDOW_YEARS: int = 10

#: 本轮 `last_notes()` 的内容（每次 fetch 覆盖）。
_NOTES: list[dict[str, Any]] = []


class FetcherError(RuntimeError):
    """取数适配层可预期的失败（参数不合、源返回结构不符、无法适配）。"""


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #

def _num(v: Any) -> Optional[float]:
    """宽容解析数值：空串 / None / 解析不了都返回 None（**不抛**）。

    为什么要宽容：NBS 的占位行 `v` 是**空串**，World Bank 的 `value` 可能是 `null`。
    这两种都是"官方没发布"，是本项目要如实保留的信息，不是错误。
    """
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    text = str(v).strip()
    if not text:
        return None
    try:
        return normalize.parse_value(text)
    except Exception:  # noqa: BLE001 - NormalizeError 及一切解析异常
        return None


def _period_type_of(period: str) -> str:
    """从 period 标签判频率：``2020`` -> annual / ``2020-01`` -> monthly / ``2020-Q1`` -> quarterly。"""
    p = str(period or "")
    if len(p) == 4 and p.isdigit():
        return "annual"
    if len(p) == 7 and p[4] == "-" and p[:4].isdigit() and p[5:].isdigit():
        return "monthly"
    if "-Q" in p.upper():
        return "quarterly"
    return "unknown"


def _year_of(period: str) -> Optional[int]:
    """取 period 的年份部分（``2020-01`` -> 2020）。"""
    try:
        return int(str(period)[:4])
    except (TypeError, ValueError):
        return None


def default_window(from_year: Optional[int], to_year: Optional[int]) -> tuple[int, int]:
    """把可能为 None 的年份补成闭区间；都没给就用**最近 10 年**。

    为什么要给默认值而不是"不限"：五个源对"不限"的处理不一样 ——
    NBS 必须显式给 `dts`（不给就等于没取），World Bank 得给 `date`，
    而 IMF / BIS 一次就把整条序列吐回来。不统一的话，同一条 `edh fetch` 命令
    在不同源上会得到跨度差几十年的结果，合并成一张表就没法看了。
    """
    now = datetime.now(timezone.utc).year
    hi = int(to_year) if to_year else now
    lo = int(from_year) if from_year else hi - (DEFAULT_WINDOW_YEARS - 1)
    if lo > hi:
        raise FetcherError(f"--from {lo} 晚于 --to {hi}（区间为空）")
    return lo, hi


def _row(indicator: str, source: str, region: str, period: str, value: Any,
         unit: Any, series_name: Any) -> dict[str, Any]:
    """造一行统一行（period_type / frequency 由 period 推）。"""
    ptype = _period_type_of(period)
    return {
        "indicator": indicator,
        "source": source,
        "region": region,
        "period": str(period),
        "period_type": ptype,
        "value": value,
        "unit": "" if unit is None else str(unit),
        "frequency": ptype,
        "series_name": "" if series_name is None else str(series_name),
    }


def _note(source: str, status: str, detail: str = "", n_rows: int = 0) -> None:
    """记一条本轮的源级结果（成功 / 跳过 / 失败）。"""
    _NOTES.append({"source": source, "status": status, "detail": detail, "n_rows": n_rows})


def last_notes() -> list[dict[str, Any]]:
    """返回最近一次 fetch 的**源级**结果列表。

    每条形如 ``{"source": "nbs", "status": "ok|skipped|error", "detail": "...", "n_rows": 12}``。
    五个 client 用 `last_meta()` 报自己的元数据；本层跨了五个 client，
    所以报的是"每个源这一轮怎么了"。

    **注意累积语义**：`fetch_indicator` 只往列表里**追加**，不清空 ——
    否则 `fetch_all_sources` 每调一个源就把前一个源的记录清掉，
    简报里只剩最后一个源的计数（第一版就是这么错的，自检 [9] 抓出）。
    要重新开始记，用 `reset_notes()`。
    """
    return list(_NOTES)


def reset_notes() -> None:
    """清空 `last_notes()`。公开入口（`fetch_all_sources` / CLI）在开工前调一次。"""
    _NOTES.clear()


# --------------------------------------------------------------------------- #
# 频率处理
# --------------------------------------------------------------------------- #

def annualize_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """把比年度更细的行**按年取算术平均**，压成年度行。

    与既有链路一致：`scan-missing.py` 处理 FRED 月度 CPI 就是取年均值，
    并在行上留 `aggregated_from_months` 记录**参与平均的月数** ——
    这个数很重要：只有 11 个月的均值和一个完整年的均值不该被当成同等可信。

    value 全是 None 的年份**保留一行**（value=None），不丢年份（§2.9）。
    """
    buckets: dict[tuple, list[Optional[float]]] = {}
    meta: dict[tuple, dict[str, Any]] = {}
    order: list[tuple] = []
    for r in rows:
        y = _year_of(r.get("period", ""))
        if y is None:
            continue
        key = (r.get("source"), r.get("indicator"), r.get("region"), y)
        if key not in buckets:
            buckets[key] = []
            meta[key] = r
            order.append(key)
        buckets[key].append(r.get("value"))

    out: list[dict[str, Any]] = []
    for key in order:
        vals = [v for v in buckets[key] if isinstance(v, (int, float))]
        tpl = meta[key]
        row = _row(tpl["indicator"], tpl["source"], tpl["region"], str(key[3]),
                   (sum(vals) / len(vals)) if vals else None,
                   tpl.get("unit"), tpl.get("series_name"))
        row["aggregated_from_months"] = len(buckets[key])
        row["n_non_null_months"] = len(vals)
        out.append(row)
    out.sort(key=lambda r: (r["source"], r["period"]))
    return out


def _to_frequency(rows: list[dict[str, Any]], frequency: Optional[str]) -> list[dict[str, Any]]:
    """按请求的频率整理行：``annual`` 时年化；行本来就是年度则原样返回。"""
    if frequency is None:
        return rows
    freq = frequency.strip().lower()
    if freq == "annual":
        if all(r["period_type"] == "annual" for r in rows):
            return rows
        return annualize_rows(rows)
    if freq == "monthly":
        # 只有原生月度的源能满足；年度的**不能**反向拆成月度（那是伪造数据，§2.5）。
        return [r for r in rows if r["period_type"] == "monthly"]
    raise FetcherError(f"不支持的频率 {frequency!r}（只支持 annual / monthly）")


# --------------------------------------------------------------------------- #
# 各源适配器（每个都返回 list[dict[统一行]]）
# --------------------------------------------------------------------------- #

def _fetch_nbs(indicator: str, spec: dict[str, Any], region: str, lo: int, hi: int,
               frequency: Optional[str]) -> list[dict[str, Any]]:
    """NBS：POST `getEsDataByIndicatorIdAndDa`。

    `dts` **必须显式给全**（NBS 不接受"不限"），所以年份区间在这里就变成时间点代码：
    年度 `"2020YY"`、月度 `"202001MM"`（见 `_probes/README.md` 的接口结论表）。
    """
    if region not in NBS_REGION_DA:
        raise FetcherError(
            f"NBS 只支持 region=CHN（目录里只有全国口径；其它地区要用 "
            f"nbs_client.list_provinces() 拿 12 位地区码，本轮未做）")
    da = NBS_REGION_DA[region]
    freq = str(spec.get("frequency") or "annual").lower()
    if frequency == "monthly" and freq != "monthly":
        raise FetcherError("该 NBS 映射是年度口径，无法满足 --frequency monthly")
    if freq == "monthly":
        dts = [f"{y}{m:02d}MM" for y in range(lo, hi + 1) for m in range(1, 13)]
    else:
        dts = [f"{y}YY" for y in range(lo, hi + 1)]

    raw = nbs_client.fetch_indicator_data(
        cid=spec["cid"], indicator_id=spec["indicator_id"],
        root_id=spec["root_id"], da=da, dts=dts)

    unit = spec.get("unit")
    series_name = spec.get("series_name") or spec.get("indicator_id")
    rows: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        code = str(item.get("dt") or "")
        # 用结构化的 dt 代码还原 period，**不去解析中文 dt_name**（"2020年1月" 这类
        # 文本解析会在季度/累计值上出错；dt 的 YY/MM 后缀是接口契约的一部分）。
        if code.endswith("YY"):
            period = code[:4]
        elif code.endswith("MM"):
            period = f"{code[:4]}-{code[4:6]}"
        elif code.endswith("SS"):
            period = f"{code[:4]}-Q{code[4:6]}"
        else:
            period = str(item.get("dt_name") or code)
        row = _row(indicator, "nbs", region, period, _num(item.get("v")),
                   unit or item.get("du_name") or item.get("unit"), series_name)
        rows.append(row)
    rows.sort(key=lambda r: r["period"])
    return rows


def _fetch_worldbank(indicator: str, spec: dict[str, Any], region: str, lo: int, hi: int,
                     frequency: Optional[str]) -> list[dict[str, Any]]:
    """World Bank：`/v2/country/{country}/indicator/{code}?date=lo:hi`。

    WB 的 `per_page` 默认只有 50，年限一长会**静默截断** —— `fetch_indicator`
    已经显式传了 1000，这里不重复设。
    """
    code = spec["indicator_code"]
    raw = worldbank_client.fetch_indicator(region, code, f"{lo}:{hi}")
    unit = spec.get("unit")
    rows: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        # indicator.value 形如 "GDP (current LCU)"，真实单位在括号里 —— 只在
        # 目录没给 unit 时才拿它兜底。
        name = (item.get("indicator") or {}).get("value") or code
        rows.append(_row(indicator, "worldbank", region, item.get("date"),
                         _num(item.get("value")), unit, name))
    rows.sort(key=lambda r: r["period"])
    return rows


def _fetch_imf(indicator: str, spec: dict[str, Any], region: str, lo: int, hi: int,
               frequency: Optional[str], allow_forecast: bool) -> list[dict[str, Any]]:
    """IMF DataMapper：`/v1/{indicator}/{country}`。

    **接口给不了时间范围**，一次返回全部年份；而且**含预测值**（实测 CHN 的
    NGDPD / LUR 都排到 2031）。所以默认截到今年 —— 把预测值混进"实际数据"
    是最容易骗到人的一类错误（§3.8）。
    """
    code = spec["indicator_code"]
    raw = imf_client.fetch_indicator(code, region)
    now = datetime.now(timezone.utc).year
    unit = spec.get("unit")
    rows: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        period = str(item.get("period") or "")
        year = _year_of(period)
        if year is None or year < lo or year > hi:
            continue
        if year > now and not allow_forecast:
            continue
        rows.append(_row(indicator, "imf", region, period, _num(item.get("value")),
                         unit, code))
    rows.sort(key=lambda r: r["period"])
    return rows


def _fetch_fred(indicator: str, spec: dict[str, Any], region: str, lo: int, hi: int,
                frequency: Optional[str]) -> list[dict[str, Any]]:
    """FRED：`fredgraph.csv`（免密钥）。有 start/end，直接给年份边界。"""
    sid = spec["series_id"]
    raw = fred_client.fetch_series(sid, start=str(lo), end=str(hi))
    unit = spec.get("unit")
    rows: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        rows.append(_row(indicator, "fred", region, item.get("period"),
                         _num(item.get("value")), unit, sid))
    rows.sort(key=lambda r: r["period"])
    return rows


def _fetch_bis(indicator: str, spec: dict[str, Any], region: str, lo: int, hi: int,
               frequency: Optional[str]) -> list[dict[str, Any]]:
    """BIS SDMX：`{dataset}/{key}`。同样给不了范围，取回后本地按年份裁。"""
    dataset, key = spec["dataset"], spec["key"]
    raw = bis_client.fetch_series(dataset, key)
    unit = spec.get("unit")
    series_name = f"{dataset} {key}"
    rows: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        period = str(item.get("period") or "")
        year = _year_of(period)
        if year is None or year < lo or year > hi:
            continue
        rows.append(_row(indicator, "bis", region, period, _num(item.get("value")),
                         unit, series_name))
    rows.sort(key=lambda r: r["period"])
    return rows


#: 源名 -> 适配器
_ADAPTERS = {
    "nbs": _fetch_nbs,
    "worldbank": _fetch_worldbank,
    "imf": _fetch_imf,
    "fred": _fetch_fred,
    "bis": _fetch_bis,
}


# --------------------------------------------------------------------------- #
# 公开接口
# --------------------------------------------------------------------------- #

def _pick_spec(entry: dict[str, Any], source: str,
               frequency: Optional[str]) -> dict[str, Any]:
    """在该源的映射里挑出满足请求频率的那一份（主映射优先，其次 variants）。

    `variants` 的存在理由就是"同一源上的另一种频率/口径"（如 NBS CPI 年度 + 月度），
    所以只有这里能体现它的价值：`--frequency monthly` 会挑到月度那份，
    而不是拿年度那份硬撑。
    """
    spec = entry["sources"].get(source)
    if spec is None:
        raise KeyError(
            f"{entry['name']} 在 {source} 上没有映射。可用源: "
            f"{', '.join(entry['available_sources']) or '(无)'}")
    if frequency is None:
        return spec
    want = frequency.strip().lower()
    if str(spec.get("frequency") or "annual").lower() == want:
        return spec
    for var in spec.get("variants") or []:
        if str(var.get("frequency") or "").lower() == want:
            merged = {**spec, **var}
            merged.pop("variants", None)
            return merged
    # 没有匹配的 variant：仍返回主映射，让适配器/频率层去决定"年化"还是"跳过"
    return spec


def fetch_indicator(indicator: str, source: Optional[str] = None, region: str = "CHN",
                    from_year: Optional[int] = None, to_year: Optional[int] = None,
                    frequency: Optional[str] = None,
                    allow_forecast: bool = False) -> list[dict[str, Any]]:
    """取一个指标的行；`source=None` 表示**所有可用源**合并返回。

    :param indicator: 指标名（规范名 / 别名 / 中文，交给 `catalog.get_indicator` 解析）。
    :param source: 源名；None = 目录里该指标的全部可用源。
    :param region: 地区代码，默认 ``CHN``（fred / bis 的序列国别固定，只接受 CHN）。
    :param from_year: 起始年（含）；与 `to_year` 都不给时取**最近 10 年**。
    :param to_year: 结束年（含）。
    :param frequency: ``annual`` 时把月度行年化；``monthly`` 时只保留原生月度源。
    :param allow_forecast: 是否保留 IMF 的预测年份（默认**截到今年**）。
    :returns: 统一形状的行列表（字段见 :data:`ROW_FIELDS`）；按 (source, period) 排序。
    :raises KeyError: 指标不存在 / 源不存在 / 该源不提供该指标。
    :raises FetcherError: 参数不合，或某源的返回无法适配。

    注意 `_NOTES` **不清空** —— 见 `last_notes()` 的说明：本函数只追加，
    由 `fetch_all_sources` / CLI 在开工前调 `reset_notes()`。
    """
    try:
        entry = catalog.get_indicator(indicator)
    except catalog.CatalogError as exc:
        raise KeyError(str(exc)) from exc

    lo, hi = default_window(from_year, to_year)
    sources = [source.strip().lower()] if source else list(entry["available_sources"])
    if source:
        src = source.strip().lower()
        if src not in catalog.SOURCES:
            raise KeyError(f"未知源 {source!r}（只支持 {', '.join(catalog.SOURCES)}）")
        if src not in entry["sources"]:
            # 明确报错，不返回空列表 —— 空列表会被读成"这个源没数据"，
            # 而事实是"这个源根本没有这个指标"，两件事必须分清（同 catalog 的规矩）。
            raise KeyError(
                f"{entry['name']} 在 {src} 上没有映射。可用源: "
                f"{', '.join(entry['available_sources']) or '(无)'}")

    out: list[dict[str, Any]] = []
    for src in sources:
        if src in COUNTRY_FIXED_SOURCES and region != "CHN":
            raise FetcherError(
                f"{src} 的序列 ID 里已写死国家（如 CHNCPIALLMINMEI / M.CN.771），"
                f"无法按 region={region!r} 取数。请用 region=CHN 或换源。")
        spec = _pick_spec(entry, src, frequency)
        try:
            if src == "imf":
                rows = _fetch_imf(entry["name"], spec, region, lo, hi, frequency,
                                  allow_forecast)
            else:
                rows = _ADAPTERS[src](entry["name"], spec, region, lo, hi, frequency)
        except (KeyError, catalog.CatalogError):
            raise
        except FetcherError:
            raise
        except Exception as exc:  # noqa: BLE001 - 各家 client 的异常类型不一
            raise FetcherError(f"{src} 取数失败: {type(exc).__name__}: {exc}") from exc

        before = len(rows)
        rows = _to_frequency(rows, frequency)
        if frequency == "monthly" and before and not rows:
            _note(src, "skipped", f"该源没有月度口径（原生频率 {spec.get('frequency')}）")
            continue
        _note(src, "ok", f"{entry['name']} {lo}-{hi}", len(rows))
        out.extend(rows)
    out.sort(key=lambda r: (r["source"], r["period"]))
    return out


def fetch_all_sources(indicator: str, **kwargs: Any) -> dict[str, list[dict[str, Any]]]:
    """按源分组返回：``{"nbs": [rows...], "worldbank": [rows...], ...}``。

    与 `fetch_indicator` 共用同一套适配逻辑，只是把结果按 source 分了组。
    某源失败**不会**影响其它源（失败原因记在 `last_notes()` 里）——
    用户要的是一张尽量完整的数据表，不是一个源不通就整体报错。

    :raises KeyError: 指标不存在。
    """
    reset_notes()
    try:
        entry = catalog.get_indicator(indicator)
    except catalog.CatalogError as exc:
        raise KeyError(str(exc)) from exc

    grouped: dict[str, list[dict[str, Any]]] = {}
    for src in entry["available_sources"]:
        try:
            grouped[src] = fetch_indicator(entry["name"], source=src, **kwargs)
        except (FetcherError, KeyError) as exc:
            # fetch_indicator 会清空 notes，这里把失败原因补回去
            _note(src, "error", str(exc))
            grouped[src] = []
    return grouped


# --------------------------------------------------------------------------- #
# 交叉验证
# --------------------------------------------------------------------------- #

def _unit_key(unit: Any) -> str:
    """把单位文本归一成"可比的量纲键"：**只去掉频率限定词，绝不动基期**。

    为什么需要：同一条口径在目录里会因为频率不同写成不同字符串 ——
    World Bank 的 CPI 是 ``指数（2010=100）``，BIS 的月度 CPI 是
    ``指数（2010=100，月度）``，两者其实**同为 2010=100 的 CPI 指数**，
    直接比字符串会误判成"口径不同"（第一版就是这样误报的）。

    但 ``指数（上年=100）``（NBS）与 ``指数（2010=100）``（WB）**必须**判为不同 ——
    它们的基期根本不一样，差异率是没意义的。所以这里只删频率词，
    基期数字原样保留。
    """
    s = str(unit or "").strip()
    for tok in ("，月度", "，年度", "，季度", "（月度）", "（年度）", "（季度）",
                "(月度)", "(年度)", "(季度)", "月度", "年度", "季度"):
        s = s.replace(tok, "")
    return s.strip().strip("，,、;； ").strip()


def cross_check(rows_by_source: dict[str, list[dict[str, Any]]],
                min_common: int = 2) -> list[dict[str, Any]]:
    """对所有有数据的源两两比对（给 `edh fetch --cross-check` 用）。

    只比 **period_type 相同**的行：把 NBS 的年度 CPI 和 FRED 的月度 CPI 混在一起
    对比是没意义的（期间根本对不齐）。每对取"两侧都有的那个 period_type"。

    **不替用户做单位换算**：两侧 unit 不同时 `unit_match=False`，
    调用方应当据此把结论标成"口径不同，差异率不具可比性"，而不是报"冲突"。
    正例见 `tools/compare-cpi.py`（把 FRED 月度指数换成「上年=100」再比）。

    :returns: 每对一条，含 `sources` / `period_type` / `units` / `unit_match` /
              `n_common` / `max_diff_rate` / `verdict` / `comparable`。
    """
    names = [s for s, rows in rows_by_source.items() if rows]
    out: list[dict[str, Any]] = []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            ra, rb = rows_by_source[a], rows_by_source[b]
            types_a = {r["period_type"] for r in ra}
            types_b = {r["period_type"] for r in rb}
            shared = types_a & types_b
            annualized = False
            if not shared:
                # 频率对不上（如 NBS 年度 vs FRED 月度）时，**自动年化后再比**。
                # 这不是替用户做单位换算（那是另一回事、且必须显式），只是把
                # "把两边放到同一时间粒度"这件必要动作做掉，并在结果里标明
                # `annualized_for_comparison=True`，绝不悄悄发生。
                ra2 = annualize_rows(ra)
                rb2 = annualize_rows(rb)
                if ra2 and rb2:
                    ra, rb, shared, annualized = ra2, rb2, {"annual"}, True
            if not shared:
                out.append({
                    "sources": [a, b], "period_type": None, "units": {},
                    "unit_match": None, "n_common": 0, "max_diff_rate": None,
                    "mean_diff_rate": None, "verdict": "无法比对",
                    "comparable": False,
                    "reason": f"没有共同的频率且无法年化（{a}: {sorted(types_a)} / "
                              f"{b}: {sorted(types_b)}）",
                })
                continue
            # 取共同里"两边行数都最多"的那个 period_type
            ptype = max(shared,
                        key=lambda t: (sum(1 for r in ra if r["period_type"] == t),
                                       sum(1 for r in rb if r["period_type"] == t)))
            sa = [r for r in ra if r["period_type"] == ptype]
            sb = [r for r in rb if r["period_type"] == ptype]
            cmp_result = cross_validation.compare_series(sa, sb)
            summary = cmp_result["summary"]
            abs_diffs = [abs(r["diff"]) for r in cmp_result["rows"]
                         if isinstance(r.get("diff"), (int, float))]
            unit_a = sa[0].get("unit", "") if sa else ""
            unit_b = sb[0].get("unit", "") if sb else ""
            key_a, key_b = _unit_key(unit_a), _unit_key(unit_b)
            unit_match = bool(key_a) and key_a == key_b
            n_common = summary["n_common"]
            comparable = unit_match and n_common >= min_common
            entry = {
                "sources": [a, b],
                "period_type": ptype,
                "annualized_for_comparison": annualized,
                "units": {a: unit_a, b: unit_b},
                "unit_keys": {a: key_a, b: key_b},
                "unit_match": unit_match,
                "n_common": n_common,
                # max_diff_rate 是**相对**差异（|a-b|/|b|，分母取 B 侧）；
                # max_abs_diff 是**绝对**差异，量纲就是该指标的 unit。
                # 简报里两个都给：只说相对值容易把"小量纲"读成大分歧（§5.4 的教训）。
                "max_diff_rate": summary["max_diff_rate"],
                "mean_diff_rate": summary["mean_diff_rate"],
                "max_abs_diff": max(abs_diffs) if abs_diffs else None,
                "verdict": summary["verdict"] if comparable else
                           ("口径不同" if not unit_match else "重叠期不足"),
                "comparable": comparable,
            }
            if not unit_match:
                entry["reason"] = ("两侧单位/口径不同，差异率不具可比性"
                                   "（要用先做显式换算，见 tools/compare-cpi.py）")
            elif n_common < min_common:
                entry["reason"] = f"重叠期只有 {n_common} 期（少于 {min_common}）"
            out.append(entry)
    return out


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #

def _selftest() -> int:
    failures: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            failures.append(msg)

    print("=" * 92)
    print("self-test: fetcher（适配层：五源统一形状 / 频率 / 区间 / 交叉验证）")
    print("=" * 92)

    # 1) 单源：NBS CPI 年度
    rows = fetch_indicator("CPI", source="nbs", from_year=2020, to_year=2024)
    print(f"\n[1] fetch_indicator('CPI', source='nbs', 2020-2024) -> {len(rows)} 行")
    for r in rows:
        print(f"      {json.dumps(r, ensure_ascii=False)}")
    check(len(rows) >= 5, f"1: 期望 >=5 行，实际 {len(rows)}")
    for r in rows:
        for f in ROW_FIELDS:
            check(f in r, f"1: 行缺字段 {f}")
        check(r["source"] == "nbs", "1: source 应为 nbs")
        check(r["indicator"] == "CPI", "1: indicator 应为 CPI")
        check(r["period_type"] == "annual", "1: period_type 应为 annual")
        check(r["region"] == "CHN", "1: region 应为 CHN")
    check(all(_year_of(r["period"]) in range(2020, 2025) for r in rows),
          "1: 有行落在 2020-2024 之外")

    # 2) 多源
    grouped = fetch_all_sources("CPI", from_year=2020, to_year=2024)
    print(f"\n[2] fetch_all_sources('CPI', 2020-2024) -> 键 {sorted(grouped)}")
    for s, rs in grouped.items():
        print(f"      {s:<10} {len(rs):>4} 行")
    check(set(grouped) == {"nbs", "worldbank", "fred", "bis"},
          f"2: 键应含 CPI 的 4 个可用源，实际 {sorted(grouped)}")
    check(all(len(rs) > 0 for rs in grouped.values()),
          f"2: 有空源 {[s for s, rs in grouped.items() if not rs]}")
    check(all(r["source"] == s for s, rs in grouped.items() for r in rs),
          "2: 分组与行内 source 不一致")
    notes2 = last_notes()
    print(f"      last_notes() 记了 {len(notes2)} 条: "
          f"{[(n['source'], n['n_rows']) for n in notes2]}")
    check(len(notes2) == 4,
          f"2: last_notes 应记下全部 4 个源（**不能被后一个源清掉**），实际 {len(notes2)}")

    # 3) 频率：月度年化
    monthly = fetch_indicator("CPI", source="bis", from_year=2020, to_year=2024)
    annual = fetch_indicator("CPI", source="bis", from_year=2020, to_year=2024,
                             frequency="annual")
    print(f"\n[3] BIS CPI 原生 {len(monthly)} 行（{monthly[0]['period_type']}）"
          f" -> 年化 {len(annual)} 行（{annual[0]['period_type']}）")
    check(all(r["period_type"] == "monthly" for r in monthly), "3: BIS 原生应为月度")
    check(all(r["period_type"] == "annual" for r in annual), "3: 年化后应为年度")
    check(len(annual) == 5, f"3: 2020-2024 年化应得 5 行，实际 {len(annual)}")
    check(all("aggregated_from_months" in r for r in annual),
          "3: 年化行应带 aggregated_from_months（本月数）")
    # 手算校验：2020 年年均值应等于 12 个月均值
    months = [r["value"] for r in monthly if r["period"].startswith("2020")]
    expect = sum(m for m in months if m is not None) / len([m for m in months if m is not None])
    got = next(r["value"] for r in annual if r["period"] == "2020")
    check(abs(got - expect) < 1e-9, f"3: 2020 年均值 期望 {expect}，实际 {got}")
    print(f"      2020 手算复核: 月均 {expect:.6f} == 年化 {got:.6f}")

    # 4) 不存在的指标 / 源 -> KeyError
    try:
        fetch_indicator("NONEXIST")
        failures.append("4: 不存在的指标应抛 KeyError")
    except KeyError as exc:
        print(f"\n[4] 不存在的指标 -> KeyError: {str(exc)[:66]}…")
    try:
        fetch_indicator("CPI", source="bogus")
        failures.append("4: 不存在的源应抛 KeyError")
    except KeyError as exc:
        print(f"    不存在的源 -> KeyError: {str(exc)[:66]}…")
    try:
        fetch_indicator("GOVERNMENT_DEBT", source="nbs")
        failures.append("4: 该源不提供该指标时应抛 KeyError")
    except KeyError as exc:
        print(f"    源不提供该指标 -> KeyError: {str(exc)[:66]}…")

    # 5) 年份区间过滤
    wide = fetch_indicator("CPI", source="nbs")
    narrow = fetch_indicator("CPI", source="nbs", from_year=2022, to_year=2023)
    lo, hi = default_window(None, None)
    print(f"\n[5] 默认窗口 {lo}-{hi} -> {len(wide)} 行；"
          f"2022-2023 -> {len(narrow)} 行")
    check(all(r["period"] in ("2022", "2023") for r in narrow),
          "5: --from/--to 过滤失效")
    check(len(narrow) == 2, f"5: 2022-2023 应得 2 行，实际 {len(narrow)}")
    check(len(wide) == DEFAULT_WINDOW_YEARS,
          f"5: 默认窗口应得 {DEFAULT_WINDOW_YEARS} 行，实际 {len(wide)}")
    try:
        default_window(2024, 2020)
        failures.append("5: from > to 应抛 FetcherError")
    except FetcherError:
        print("     from > to 正确报错")

    # 6) 交叉验证：频率不同要自动年化后比；单位不同要把结论标成"口径不同"
    pairs = cross_check(grouped)
    print(f"\n[6] cross_check('CPI' 四源) -> {len(pairs)} 对")
    for p in pairs:
        print(f"      {' vs '.join(p['sources']):<24} {str(p['period_type']):<8} "
              f"n={p['n_common']:<3} 年化={str(p.get('annualized_for_comparison')):<5} "
              f"unit_match={p['unit_match']} -> {p['verdict']}")
    check(len(pairs) == 6, f"6: 4 个源应得 6 对，实际 {len(pairs)}")
    for p in pairs:
        check("comparable" in p and "unit_match" in p, "6: 对里缺 comparable/unit_match")
        check(p["period_type"] is not None,
              f"6: {p['sources']} 应能自动年化后比对，不该是'无法比对'")
        if p["unit_match"] is False:
            check(p["comparable"] is False,
                  f"6: 单位不同时 comparable 必须为 False（{p['sources']}）")
            check("reason" in p, "6: 口径不同应给出 reason")
    # NBS(上年=100) vs FRED(2015=100) 正是"两条同源转载、但基数不同"的典型：
    # 必须被判成不可比，而不是报一个巨大的差异率当成"冲突"
    nbs_fred = next(p for p in pairs if set(p["sources"]) == {"nbs", "fred"})
    check(nbs_fred["annualized_for_comparison"] is True,
          "6: nbs vs fred 频率不同，应标记为已年化后比对")
    check(nbs_fred["comparable"] is False and nbs_fred["verdict"] == "口径不同",
          f"6: nbs vs fred 应判'口径不同'，实际 {nbs_fred['verdict']}")
    # 反向用例：World Bank(指数（2010=100）) vs BIS(指数（2010=100，月度）) ——
    # 字面不同但**基期相同**，必须判为可比。只删频率词、不动基期，是这条的关键。
    wb_bis = next(p for p in pairs if set(p["sources"]) == {"worldbank", "bis"})
    check(wb_bis["unit_match"] is True and wb_bis["comparable"] is True,
          f"6: worldbank vs bis 同为 2010=100，应判可比，实际 "
          f"unit_match={wb_bis['unit_match']} verdict={wb_bis['verdict']}")
    print(f"      [6b] worldbank vs bis 单位键 {wb_bis['unit_keys']} -> "
          f"{wb_bis['verdict']}（相对 {wb_bis['max_diff_rate']:.4%}）")

    # 7) 年化后的跨源对比应当只剩年度对
    grouped_annual = fetch_all_sources("CPI", from_year=2020, to_year=2024,
                                       frequency="annual")
    pairs_annual = cross_check(grouped_annual)
    print(f"\n[7] 年化后再比 -> {len(pairs_annual)} 对，"
          f"period_type={sorted({p['period_type'] for p in pairs_annual}, key=str)}")
    check(all(p["period_type"] == "annual" for p in pairs_annual),
          "7: 年化后所有对都应是 annual")

    # 8) fred / bis 的国别固定：换 region 必须报错，不能默默返回中国的数
    try:
        fetch_indicator("CPI", source="fred", region="USA", from_year=2020, to_year=2024)
        failures.append("8: fred + region=USA 应报错")
    except FetcherError as exc:
        print(f"\n[8] fred + region=USA 正确报错: {str(exc)[:60]}…")

    # 9) last_notes 记录源级结果（累积语义）
    reset_notes()
    fetch_indicator("CPI", source="nbs", from_year=2023, to_year=2024)
    notes = last_notes()
    print(f"\n[9] last_notes() -> {[(n['source'], n['status'], n['n_rows']) for n in notes]}")
    check(len(notes) == 1 and notes[0]["status"] == "ok",
          f"9: 单源取数后应恰好 1 条 ok 记录，实际 {notes}")
    reset_notes()
    check(last_notes() == [], "9: reset_notes() 后应为空")

    print("-" * 92)
    if failures:
        print(f"FAIL: {len(failures)} 项")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS: fetcher 自检全部通过")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    parser = argparse.ArgumentParser(
        prog="econ_core.fetcher",
        description="取数适配层：把 catalog 配置变成各源 client 调用（会联网）。")
    parser.add_argument("--test", action="store_true", help="跑自检（**会联网**）")
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.test:
        return _selftest()
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
