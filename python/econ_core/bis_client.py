#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BIS 客户端（国际清算银行 SDMX 2.1 REST）—— 中国 CPI 的**长序列**来源。

为什么接 BIS（读之前先纠正一个可能的误解）
==========================================

**不是为了独立性。** 方向 C 第二轮已实测判定：中国**没有**独立编制的 CPI，
四个候选（BIS / OECD / PWT 11.0 / Maddison 2023）全部是转载或二手汇编，
证据见 `python/_probes/README.md` 的「2026-09-27 探测」段与 PROJECT_STATE §3.10。
BIS 官方 FAQ 的原文就摆在那里：

    "Consumer price indices are predominantly compiled by national statistical offices."

接 BIS 的真实理由有两条，都在「加工」而不在「采集」：

1. **序列长度**：BIS 的 `M.CN.628` 从 **1995-01** 起（380 期），`M.CN.771` 从 1996-01 起（368 期）。
   本项目现有中国 CPI 只有 2015 起的 11 个年度点 —— 这一接把覆盖往前推了 **20 年**。
2. **拼接**：BIS 对自己的长序列做了**拼接**与**重定基**：

       "The BIS has constructed long consumer price indices, by joining the series
        available for consecutive periods."

   这是本项目里第一条**真正需要拼接**的链外序列（PROJECT_STATE §5.3 记的
   「系统至今从未真正拼接」），也是下一轮拼接器 + 拼接断点检查的真场景。
   **本模块只落盘、不拼接** —— 拼接是下一轮的事。

端点与坑（全部为实测，不要凭 SDMX 通识想当然）
===============================================

端点根
    ``https://stats.bis.org/api/v1/``（GET 根路径返回 ``{"v":"1.0.0"}``，可用于探活）。
    免密钥、无分页、无 cookie。

坑 1：``?format=jsondata`` 恒 406
    BIS **不是** OECD 那套写法。``format=jsondata`` 返回 HTTP 406，
    正文 ``Unsupported format: jsondata``。可用值至少包含 ``sdmx-json``。

        ?format=sdmx-json   ->  200 + application/json   ✅ 本模块使用
        ?format=jsondata    ->  406 Unsupported format   ❌

    注意这与 OECD SDMX 是**两套不同的白名单**（PROJECT_STATE §3.12）。

坑 2：密钥三位都要给，形状是 FREQ.REF_AREA.UNIT_MEASURE
    维度顺序取自 ``availableconstraint`` 的 ``cubeRegions[0].keyValues`` 数组顺序，
    不是字母序、也不是 ``all`` 响应里 series key 的下标顺序。少给一位就 404：

        /data/BIS,WS_LONG_CPI,1.0/CN.M        -> 404 "No data for data query"
        /data/BIS,WS_LONG_CPI,1.0/M.CN.771    -> 200 ✅

    报错正文长这样（**不区分「密钥语法错」与「该组合没数据」**，两种都是它）：

        {"errors":[{"code":404,"message":"No data for data query against the dataflow: ..."}]}

坑 3：``/datastructure/...`` 读超时（实测 180s 未返回）
    DSD 端点不可依赖。维度顺序改从 ``availableconstraint`` 取，或直接读数据响应
    里自带的 ``data.structure.dimensions.series``（本模块用后者做冗余校验）。

坑 4：BIS 返回**小写** `content-encoding: gzip`（**已在 http_client 源头修好**）
    ``http_client.DEFAULT_HEADERS`` 带 ``Accept-Encoding: gzip, deflate``，BIS 于是真的 gzip 压缩
    （46932 字节 -> 8206 字节）。但 BIS（FusionEdgeServer）回的响应头名是**全小写**
    （``content-encoding: gzip``，实测原始响应块逐字如此），而旧版 ``http_client._decompress``
    取的是 ``resp_headers.get("Content-Encoding")`` —— **大小写敏感**，取到 ``None``，
    于是**不解压**，把 gzip 二进制当成 body 交给 ``json.loads``：

        json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)

    **这条坑已经修掉**：http_client 新增 ``_header_get()`` 做大小写不敏感查找，
    ``_decompress`` / ``_decode_bytes`` / ``encoding`` 三处调用点全部改用它；
    顺带修掉了同一处 ``resp_headers["Content-Type"]`` 的 **KeyError**（比取不到更严重）。
    现在 ``http_client.get_json()`` 直取 BIS 就能拿到 dict，不需要任何绕过。
    详见 PROJECT_STATE §3.13 与 http_client._decompress 的 docstring。

    本模块的 ``_decode_body`` 仍保留 **gzip magic 兜底**，但那是为了兜「回了压缩流
    却**不声明** Content-Encoding」的另一类服务器 —— 与大小写无关，属于纵深防御。

坑 5：UNIT_MEASURE 的取值语义不在 SDMX 响应里
    ``771`` = 同比变化（%）；``628`` = 指数（**2010 = 100**）。SDMX 的 codelist
    没给出人可读名称，这两条是实测标定的（见 ``_UNIT_MEANING``）。实测定标方法：
    ``A.CN.771`` 与由 ``A.CN.628`` 折算出的 ``idx[y]/idx[y-1]*100-100`` **逐位相等**
    （1979–2025 全等），证明 771 就是 BIS 从 628 自己算出来的同比。

数据形态
--------
SDMX-JSON 的观测是**位置索引**编码，不是键值对：

    data.dataSets[0].series["0:0:0"].observations["<obs_idx>"] = ["<value>", ...]
    data.structure.dimensions.observation[0].values["<obs_idx>"]["id"] = "2026-08"

所以 period 要从 ``dimensions.observation`` 里按索引回查。value 是**字符串**，
缺值实测为 ``None``（不是空串），解析成 ``None`` 保留。

落盘约定（与 nbs / worldbank / imf / fred 一致）
------------------------------------------------

    data/parsed/bis/<dataset>_<key>_<sha16>.json
    {"request": {...}, "raw_cache": "<绝对路径>", "data": ..., "fetched_at": "<ISO UTC>"}

文件名里同时带 dataset 与 key（key 里的 ``.`` 保留，Windows 合法），
避免同一 dataset 下多条序列互相覆盖。

用法
----

    $env:PYTHONPATH="python"; ./.venv/Scripts/python.exe -m econ_core.bis_client --test

    from econ_core.bis_client import fetch_cpi
    rows = fetch_cpi(unit="771")           # [{"period": "1996-01", "value": 9.0}, ...]
    idx  = fetch_cpi(unit="628")           # 指数，2010=100，1995-01 起
    raw  = fetch_series("WS_LONG_CPI", "A.CN.628")   # 通用 SDMX 拉取
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys
import time
import zlib
from pathlib import Path
from typing import Any, Optional, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from econ_core import http_client  # type: ignore[no-redef]
else:
    from . import http_client

__all__ = [
    "BisApiError",
    "fetch_cpi",
    "fetch_series",
    "list_dataflows",
    "API_ROOT",
    "DEFAULT_DATASET",
    "PARSED_DIR",
    "BIS_HEADERS",
    "last_meta",
]

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

#: SDMX 2.1 端点根（与 https://stats.bis.org/api/v2/ 不同：v2 根返回 501 resource not supported）
API_ROOT: str = "https://stats.bis.org/api/v1"

#: 本模块默认数据集：BIS 长期消费价格统计
DEFAULT_DATASET: str = "WS_LONG_CPI"

#: 解析后数据落盘目录
PARSED_DIR: Path = http_client.PROJECT_ROOT / "data" / "parsed" / "bis"

#: 请求头。BIS 不挑 UA（Chrome 与朴素 UA 都能通），但与其它四个客户端保持一致用朴素 UA。
#: 注意：**这里没有 UA 坑**，与 imf_client / fred_client 的情况不同，别照抄那边的注释。
BIS_HEADERS: dict[str, str] = {"User-Agent": "python-urllib/3.12"}

#: 唯一可用的响应格式。实测 ?format=jsondata 恒 406（见模块 docstring 坑 1）。
SDMX_FORMAT: str = "sdmx-json"

#: 默认单位代码与其含义（实测标定，SDMX codelist 无人可读名称）
_UNIT_MEANING: dict[str, str] = {
    "771": "同比变化（%）",
    "628": "指数（2010=100）",
}

#: 请求超时（秒）。单条序列的 SDMX-JSON 约 47 KB。
TIMEOUT_S: float = 60.0

#: 维度顺序（实测：取自 availableconstraint 的 cubeRegions[0].keyValues 数组顺序）
DIM_ORDER: tuple[str, ...] = ("FREQ", "REF_AREA", "UNIT_MEASURE")

#: 最近一次调用的来源元数据（供 bis_client_cli 填 raw_cache / fetched_at）。
_LAST_META: dict[str, Any] = {}


def last_meta() -> dict[str, Any]:
    """返回最近一次调用的来源元数据（含 frequency / n_rows / dataset / key）。"""
    return dict(_LAST_META)


class BisApiError(RuntimeError):
    """BIS 接口业务层异常（406 / 404 无数据 / SDMX-JSON 结构不符）。"""


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #

def _params_digest(params: dict[str, Any]) -> str:
    """请求参数的 sha256 前 16 位，用作 parsed 文件名指纹。"""
    canonical = json.dumps(params, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _frequency_of(periods: Sequence[str]) -> str:
    """从 period 标签推断频率。BIS 的标签形态：月度 ``2026-08`` / 年度 ``2026``。"""
    if not periods:
        return "unknown"
    if all(len(p) == 4 for p in periods):
        return "annual"
    if all(len(p) == 7 for p in periods):
        return "monthly"
    if all(len(p) >= 6 and p[4:5] == "-Q" for p in periods):
        return "quarterly"
    return "unknown"


def _persist(dataset: str, key: str, request: dict[str, Any], raw_resp: Any, data: Any,
             parsed_dir: Path, fetched_at: str) -> Path:
    """把解析后的数据落到 data/parsed/bis/<dataset>_<key>_<sha16>.json。

    结构与 nbs_client / worldbank_client / imf_client / fred_client 逐键一致：
    request / raw_cache / data / fetched_at。
    """
    parsed_dir.mkdir(parents=True, exist_ok=True)
    out = parsed_dir / f"{dataset}_{key}_{_params_digest(request)}.json"
    payload = {
        "request": request,
        "raw_cache": str(raw_resp.cache_path) if raw_resp.cache_path else "",
        "data": data,
        "fetched_at": fetched_at,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def _observation_periods(structure: dict[str, Any]) -> list[str]:
    """从 data.structure.dimensions 里取观测维（TIME_PERIOD）的位置索引 -> 标签表。

    实测 TIME_PERIOD 在 ``dimensions.observation`` 里；这里同时兜住「被放在 series 维」
    的畸形响应（那种情况下观测索引就没有意义，直接报错比猜更安全）。
    """
    dims = structure.get("dimensions") or {}
    obs_dims = dims.get("observation") or []
    if not obs_dims:
        raise BisApiError(
            "SDMX-JSON 里没有 observation 维度；"
            f"实际 series 维 = {[d.get('id') for d in (dims.get('series') or [])]}"
        )
    time_dim = obs_dims[0]
    return [str(v.get("id", "")) for v in (time_dim.get("values") or [])]


def _series_dim_labels(structure: dict[str, Any], series_key: str) -> dict[str, str]:
    """把形如 ``"0:0:1"`` 的 series key 解回 ``{维度名: 取值}``（仅用于元数据）。"""
    dims = ((structure.get("dimensions") or {}).get("series")) or []
    parts = series_key.split(":")
    out: dict[str, str] = {}
    for i, dim in enumerate(dims):
        if i >= len(parts):
            break
        try:
            idx = int(parts[i])
        except ValueError:
            continue
        values = dim.get("values") or []
        if 0 <= idx < len(values):
            out[str(dim.get("id", f"dim{i}"))] = str(values[idx].get("id", ""))
    return out


def _parse_sdmx_json(obj: Any, *, dataset: str, key: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """解析 SDMX-JSON 数据消息，返回 (rows, meta)。

    :param obj: ``http_client`` 解析后的 JSON（顶层 ``{meta, data}``）。
    :returns: ``(rows, meta)``；rows 为 ``[{"period": ..., "value": ...}]``（按 period 升序），
              meta 含 frequency / n_series / series_dims / dataflow_name。
    :raises BisApiError: 结构不符（缺 data / dataSets / structure / observation 维）。
    """
    if not isinstance(obj, dict):
        raise BisApiError(f"{dataset}/{key}: 响应不是 JSON 对象，而是 {type(obj).__name__}")
    data = obj.get("data")
    if not isinstance(data, dict):
        raise BisApiError(f"{dataset}/{key}: 响应里没有 data 对象（顶层键 {list(obj.keys())}）")
    data_sets = data.get("dataSets")
    if not isinstance(data_sets, list) or not data_sets:
        raise BisApiError(f"{dataset}/{key}: data.dataSets 缺失或为空")
    structure = data.get("structure")
    if not isinstance(structure, dict):
        raise BisApiError(
            f"{dataset}/{key}: data.structure 缺失。注意 BIS 的键名是单数 structure"
            "（不是 OECD 那套 structures 数组）"
        )

    time_labels = _observation_periods(structure)
    series_block = data_sets[0].get("series") or {}

    rows: list[dict[str, Any]] = []
    series_dims: dict[str, str] = {}
    for skey, sval in series_block.items():
        series_dims = _series_dim_labels(structure, str(skey))
        observations = (sval or {}).get("observations") or {}
        for obs_idx, obs_val in observations.items():
            try:
                pos = int(obs_idx)
            except (TypeError, ValueError):
                continue
            if pos < 0 or pos >= len(time_labels):
                continue
            raw_value: Any = None
            if isinstance(obs_val, (list, tuple)) and obs_val:
                raw_value = obs_val[0]
            elif obs_val is not None:
                raw_value = obs_val
            value: Optional[float]
            if raw_value is None or raw_value == "":
                value = None
            else:
                try:
                    value = float(raw_value)
                except (TypeError, ValueError):
                    value = None
            rows.append({"period": time_labels[pos], "value": value})

    rows.sort(key=lambda r: str(r["period"]))
    periods = [str(r["period"]) for r in rows]
    meta: dict[str, Any] = {
        "dataset": dataset,
        "key": key,
        "frequency": _frequency_of(periods),
        "n_series": len(series_block),
        "series_dims": series_dims,
        "dataflow_name": str(structure.get("name") or ""),
        "n_obs": len(rows),
    }
    return rows, meta


def _raise_for_status(resp: Any, dataset: str, key: str) -> None:
    """把 BIS 的非 2xx 翻译成带上下文的 BisApiError。

    BIS 的 404 正文是 ``{"errors":[{"code":404,"message":"No data for data query ..."}]}``，
    **不区分**「密钥维度数不对」与「该组合确实没数据」，所以错误信息里必须把密钥形状写出来。
    """
    if getattr(resp, "ok", False):
        return
    status = getattr(resp, "status", None)
    body = ""
    try:
        body = _decode_body(resp)[:400].decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - 错误路径不再抛二次异常
        body = ""
    if status == 406:
        raise BisApiError(
            f"{dataset}/{key}: HTTP 406（format 不被接受）。BIS 只认 format={SDMX_FORMAT}，"
            f"jsondata 会 406。正文: {body}"
        )
    if status == 404:
        raise BisApiError(
            f"{dataset}/{key}: HTTP 404 无数据。密钥形状必须是 "
            f"{'.'.join(DIM_ORDER)}（三位都要给，如 M.CN.771）；维度数不对与真的没数据"
            f"都会返回这个 404。正文: {body}"
        )
    raise BisApiError(f"{dataset}/{key}: HTTP {status}。正文: {body}")


def _decode_body(resp: Any) -> bytes:
    """取响应原始字节，并按需 gunzip（对 http_client 解压的**兜底**）。

    **历史**：这个函数最初是修 http_client 缺陷的**绕过**。当时 BIS 回的全小写
    ``content-encoding: gzip`` 被 http_client 的大小写敏感查找漏掉，导致 gzip 二进制
    被当 JSON 解析而崩（PROJECT_STATE §3.13）。

    **现状**：http_client 已在源头修好（新增 `_header_get` 做大小写不敏感查找，
    §3.13 已闭环），正常路径下这里收到的已经是**明文**，第一段判断不会命中。

    那为什么还留着？—— 按 **gzip magic（``1f 8b``）** 判断而不是靠
    ``Content-Encoding`` 头，覆盖的是另一类情况：服务器**回了压缩流但不声明**该头。
    这类响应任何按头解压的客户端都救不了，而 magic 能识别。代价只有一次两字节比较，
    所以保留为纵深防御，**不再打印任何日志**（正常路径不该有噪音；命中即说明
    http_client 又漏了，属于异常，交给下游 JSON 报错暴露）。
    """
    raw = getattr(resp, "content", b"") or b""
    if isinstance(raw, str):                     # 理论上 get_bytes 给的是 bytes，兜一下
        raw = raw.encode("utf-8", "replace")
    if raw[:2] == b"\x1f\x8b":                   # gzip magic：兜住"压缩但不声明"
        try:
            raw = gzip.decompress(raw)
        except Exception:  # noqa: BLE001 - 解压失败让下游 JSON 报错，信息更具体
            pass
    elif raw[:1] == b"\x78":                     # zlib/deflate 常见头（0x78）
        try:
            raw = zlib.decompress(raw)
        except Exception:  # noqa: BLE001
            try:
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
            except Exception:  # noqa: BLE001
                pass
    return raw


def _get_json_robust(url: str, dataset: str, key: str, *, save: bool) -> tuple[Any, Any]:
    """取 JSON，返回 (parsed, resp)。

    用 ``get_bytes()`` 而不是 ``get_json()`` —— 后者对 BIS 必然抛 JSONDecodeError（坑 4）。
    """
    resp = http_client.get_bytes(url, headers=BIS_HEADERS, save=save, timeout=TIMEOUT_S)
    _raise_for_status(resp, dataset, key)
    raw = _decode_body(resp)
    text = raw.decode("utf-8", "replace").strip()
    try:
        return json.loads(text), resp
    except json.JSONDecodeError as exc:
        raise BisApiError(
            f"{dataset}/{key}: 响应不是 JSON（前 120 字符: {text[:120]!r}）。"
            f"若为二进制，可能是未解的压缩流（见模块 docstring 坑 4）"
        ) from exc


# --------------------------------------------------------------------------- #
# 公开接口
# --------------------------------------------------------------------------- #

def fetch_series(dataset: str, key: str, save: bool = True,
                 parsed_dir: Optional[Path] = None) -> list[dict[str, Any]]:
    """通用 SDMX-JSON 拉取：取 ``dataset`` 下某一条（或多条）序列。

    这是本模块的**底层入口**；``fetch_cpi`` 只是它的一个便捷包装。

    :param dataset: dataflow id，如 `WS_LONG_CPI`（BIS 长期消费价格统计）。
    :param key: SDMX 密钥，**必须按 FREQ.REF_AREA.UNIT_MEASURE 给满三位**，
                如 `M.CN.771`（月度 / 中国 / 同比）或 `A.CN.628`（年度 / 中国 / 指数）。
                任一位可用 ``.`` 表示通配，但通配会把整个切片拉回来（体积可能很大）。
    :param save: 是否落盘（raw 走 http_client 的 url 指纹存档，parsed 走 data/parsed/bis/）。
    :param parsed_dir: 覆盖 parsed 落盘目录（测试用）。
    :returns: `[{"period": "1996-01", "value": 9.0}, ...]`，按 period 升序；缺值是 `None`。
              period 粒度由数据自身决定（月度 `YYYY-MM` / 年度 `YYYY`）。
    :raises BisApiError: 406 / 404 / SDMX-JSON 结构不符。
    """
    ds = str(dataset).strip()
    ky = str(key).strip()
    if not ds:
        raise BisApiError("dataset 不能为空")
    if not ky:
        raise BisApiError("key 不能为空")
    if ky.count(".") != len(DIM_ORDER) - 1:
        raise BisApiError(
            f"key={ky!r} 的维度数不对：BIS 要求 {'.'.join(DIM_ORDER)} 共 {len(DIM_ORDER)} 位，"
            f"实际 {ky.count('.') + 1} 位（实测少给一位会返回 404 而不是 400）"
        )

    url = f"{API_ROOT}/data/BIS,{ds},1.0/{ky}?format={SDMX_FORMAT}"
    obj, resp = _get_json_robust(url, ds, ky, save=save)

    rows, meta = _parse_sdmx_json(obj, dataset=ds, key=ky)

    fetched_at = _utc_now()
    raw_cache = str(resp.cache_path) if resp.cache_path else ""
    parsed_file = ""
    if save:
        data = {
            "dataset": ds,
            "key": ky,
            "frequency": meta["frequency"],
            "series_dims": meta["series_dims"],
            "observation_start": rows[0]["period"] if rows else None,
            "observation_end": rows[-1]["period"] if rows else None,
            "observations": rows,
        }
        out = _persist(ds, ky, {"dataset": ds, "key": ky}, resp, data,
                       parsed_dir or PARSED_DIR, fetched_at)
        parsed_file = str(out)
        print(f"[parsed] {out}", file=sys.stderr)

    _LAST_META.clear()
    _LAST_META.update({"fetched_at": fetched_at, "raw_cache": raw_cache,
                       "parsed_file": parsed_file, "dataset": ds, "key": ky,
                       "frequency": meta["frequency"], "n_rows": len(rows),
                       "n_series": meta["n_series"], "series_dims": meta["series_dims"],
                       "dataflow_name": meta["dataflow_name"]})
    return rows


def fetch_cpi(unit: str = "771", freq: str = "M", country: str = "CN",
              save: bool = True,
              parsed_dir: Optional[Path] = None) -> list[dict[str, Any]]:
    """取 BIS 的中国 CPI（`WS_LONG_CPI` 的便捷包装）。

    :param unit: `"771"` = 同比变化（%，实测 1996-01 起）；`"628"` = 指数（2010=100，
                 实测 1995-01 起）。其它值不在这里拦，交给上游 404 报错。
    :param freq: `"M"` = 月度（默认）；`"A"` = 年度。
    :param country: ISO2 地区代码，默认 `"CN"`。BIS 的 CPI 覆盖 63 个地区，中国在其中。
    :param save: 是否落盘。
    :param parsed_dir: 覆盖 parsed 落盘目录（测试用）。
    :returns: `[{"period": "1995-01", "value": 100.0}, ...]`，按 period 升序。

    .. note::
       BIS 的序列本身是**转载** NBS 的 CPI（见模块 docstring），且经过拼接与重定基。
       实测与 NBS「上年=100」折算后的同比差异为 max 0.174 pp / mean 0.09 pp。
       **不要**把这个一致性当作独立验证 —— 它只验证转载与拼接没引入大偏差。
    """
    f = str(freq).strip().upper()
    c = str(country).strip().upper()
    u = str(unit).strip()
    if not f:
        raise BisApiError("freq 不能为空（M=月度 / A=年度）")
    if not c:
        raise BisApiError("country 不能为空（ISO2，如 CN）")
    if not u:
        raise BisApiError("unit 不能为空（771=同比 / 628=指数）")
    key = f"{f}.{c}.{u}"
    rows = fetch_series(DEFAULT_DATASET, key, save=save, parsed_dir=parsed_dir)
    meta = last_meta()
    meta["unit_meaning"] = _UNIT_MEANING.get(u, "unknown")
    _LAST_META.update({"unit_meaning": meta["unit_meaning"]})
    return rows


def list_dataflows(save: bool = True,
                   parsed_dir: Optional[Path] = None) -> list[dict[str, Any]]:
    """列出 BIS 的全部 dataflow（`/dataflow/all/all/latest?format=sdmx-json`）。

    实测返回 32 个 dataflow，覆盖 CPI / 政策利率 / 有效汇率 / 信贷 / 债务证券等。
    本函数主要给「发现可用数据集」用，不参与对比链路。

    :param save: 是否落盘到 data/parsed/bis/。
    :param parsed_dir: 覆盖 parsed 落盘目录（测试用）。
    :returns: `[{"id": "WS_LONG_CPI", "name": "Consumer prices statistics", "version": "1.0"}, ...]`
    """
    url = f"{API_ROOT}/dataflow/all/all/latest?format={SDMX_FORMAT}"
    obj, resp = _get_json_robust(url, "BIS", "dataflow_all", save=save)
    flows_raw = ((obj or {}).get("data") or {}).get("dataflows") or []
    out: list[dict[str, Any]] = []
    for f in flows_raw:
        names = f.get("names") or {}
        out.append({
            "id": str(f.get("id") or ""),
            "name": str(names.get("en") or f.get("name") or ""),
            "version": str(f.get("version") or ""),
            "agency": str(f.get("agencyID") or ""),
        })
    out.sort(key=lambda r: r["id"])

    fetched_at = _utc_now()
    if save:
        raw_cache = str(resp.cache_path) if resp.cache_path else ""
        data = {"n_dataflows": len(out), "dataflows": out}
        _persist("BIS", "dataflow_all", {"endpoint": "dataflow/all/all/latest"}, resp, data,
                 parsed_dir or PARSED_DIR, fetched_at)
        _LAST_META.clear()
        _LAST_META.update({"fetched_at": fetched_at, "raw_cache": raw_cache,
                           "n_rows": len(out), "dataset": "BIS", "key": "dataflow_all"})
    return out


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #

#: 自检用的已知锚点（来自 2026-09-27 探测，见 python/_probes/README.md）
_ANCHOR_YOY: dict[str, float] = {"2018": 1.926, "2021": 0.849, "2024": 0.240}


def _selftest() -> int:
    print("=" * 78)
    print("self-test: bis_client（真实端点调用 + 落盘）")
    print("=" * 78)

    print("\n[1] fetch_cpi(unit='771')  —— 同比 %（月度）")
    yoy = fetch_cpi(unit="771")
    m1 = last_meta()
    print(f"  返回条数  : {len(yoy)}")
    print(f"  频率      : {m1.get('frequency')}  | 单位: {m1.get('unit_meaning')}")
    print(f"  序列维度  : {json.dumps(m1.get('series_dims'), ensure_ascii=False)}")
    if yoy:
        print(f"  年份范围  : {str(yoy[0]['period'])} ~ {str(yoy[-1]['period'])}")
        print(f"  首条      : {json.dumps(yoy[0], ensure_ascii=False)}")
        print(f"  末条      : {json.dumps(yoy[-1], ensure_ascii=False)}")
    print(f"  非空值个数: {sum(1 for r in yoy if r['value'] is not None)}")

    print("\n[2] fetch_cpi(unit='628')  —— 指数（2010=100，月度）")
    idx = fetch_cpi(unit="628")
    m2 = last_meta()
    print(f"  返回条数  : {len(idx)}")
    print(f"  频率      : {m2.get('frequency')}  | 单位: {m2.get('unit_meaning')}")
    if idx:
        print(f"  年份范围  : {str(idx[0]['period'])} ~ {str(idx[-1]['period'])}")
        print(f"  首条      : {json.dumps(idx[0], ensure_ascii=False)}")
        print(f"  末条      : {json.dumps(idx[-1], ensure_ascii=False)}")
    print(f"  非空值个数: {sum(1 for r in idx if r['value'] is not None)}")

    print("\n[3] 覆盖范围核对（探测记录：771 为 1996-01~2026-08，628 为 1995-01~2026-08）")
    yoy_first = str(yoy[0]["period"]) if yoy else "-"
    yoy_last = str(yoy[-1]["period"]) if yoy else "-"
    idx_first = str(idx[0]["period"]) if idx else "-"
    idx_last = str(idx[-1]["period"]) if idx else "-"
    print(f"  771: {yoy_first} ~ {yoy_last}")
    print(f"  628: {idx_first} ~ {idx_last}")

    print("\n[4] 口径交叉核对：BIS 的 771 是否等于它自己 628 的**月度**同比？")
    # 这是本轮探测判定「771 是 BIS 自建指数折算出来的」的核心证据，所以自检里要真验一遍。
    # 月度口径下 idx[m]/idx[m-12]*100-100 必须与 771[m] 逐位相等（实测成立）。
    idx_by_period = {str(r["period"]): r["value"] for r in idx if r["value"] is not None}
    yoy_by_period = {str(r["period"]): r["value"] for r in yoy if r["value"] is not None}
    monthly_diffs: list[tuple[str, float, float, float]] = []
    for period, yv in sorted(yoy_by_period.items()):
        if len(period) != 7:
            continue
        year, month = int(period[:4]), period[5:7]
        prev = f"{year - 1:04d}-{month}"
        if period in idx_by_period and prev in idx_by_period:
            derived = idx_by_period[period] / idx_by_period[prev] * 100 - 100
            monthly_diffs.append((period, float(yv), derived, abs(derived - float(yv))))
    max_monthly = max((d[3] for d in monthly_diffs), default=None)
    print(f"  可比月数  : {len(monthly_diffs)}")
    print(f"  月度 max |771 - 628 折算| = "
          f"{'%.6f' % max_monthly if max_monthly is not None else '-'} pp")
    for period, yv, dv, diff in monthly_diffs[:3]:
        print(f"    {period}: 771={yv} 628折算={dv:.6f} diff={diff:.6f}")

    print("\n[5] 年度锚点核对（探测记录值；freq='A' 是另一条序列，年度均值口径）")
    ann = fetch_cpi(unit="771", freq="A")
    m3 = last_meta()
    yoy_by_year = {str(r["period"]): float(r["value"])
                   for r in ann if r.get("value") is not None}
    print(f"  年度序列条数: {len(ann)}  范围: "
          f"{ann[0]['period'] if ann else '-'} ~ {ann[-1]['period'] if ann else '-'}"
          f"  freq={m3.get('frequency')}")
    anchor_ok = True
    for y, expect in _ANCHOR_YOY.items():
        got = yoy_by_year.get(y)
        if got is None:
            print(f"  {y}: 缺值 -> FAIL")
            anchor_ok = False
            continue
        ok = abs(got - expect) <= 0.002
        print(f"  {y}: 实测 {got:.3f} vs 探测记录 {expect} -> {'PASS' if ok else 'FAIL'}")
        anchor_ok = anchor_ok and ok

    print("\n[6] list_dataflows()  —— 预期 32 个")
    flows = list_dataflows()
    print(f"  返回条数  : {len(flows)}")
    targets = [f for f in flows if f["id"] == DEFAULT_DATASET]
    print(f"  含 {DEFAULT_DATASET}: {json.dumps(targets, ensure_ascii=False)}")

    checks = [
        ("771 返回条数 > 300", len(yoy) > 300),
        ("628 返回条数 > 300", len(idx) > 300),
        ("771 频率识别为 monthly", m1.get("frequency") == "monthly"),
        ("628 频率识别为 monthly", m2.get("frequency") == "monthly"),
        ("771 起点早于 1997-01（实测 1996-01，即扩了 20 年）",
         bool(yoy) and yoy_first < "1997-01"),
        ("628 起点早于 1996-01（实测 1995-01）", bool(idx) and idx_first < "1996-01"),
        ("末条不早于 2026-01（实测 2026-08）",
         bool(yoy) and yoy_last >= "2026-01" and bool(idx) and idx_last >= "2026-01"),
        ("两序列 period 全部唯一", len({str(r['period']) for r in yoy}) == len(yoy)
         and len({str(r['period']) for r in idx}) == len(idx)),
        ("非空值占比 > 95%",
         sum(1 for r in yoy if r['value'] is not None) / max(1, len(yoy)) > 0.95
         and sum(1 for r in idx if r['value'] is not None) / max(1, len(idx)) > 0.95),
        ("771 与 628 折算的月度同比逐位吻合（max diff < 0.01 pp，证明 771 是 BIS 自建指数折算）",
         max_monthly is not None and max_monthly < 0.01),
        ("月度可比月数 > 200", len(monthly_diffs) > 200),
        ("freq='A' 返回年度序列且频率识别为 annual",
         m3.get("frequency") == "annual" and len(ann) > 30),
        ("锚点年份全部命中探测记录", anchor_ok),
        (f"list_dataflows 有 {DEFAULT_DATASET}", bool(targets)),
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

    p = argparse.ArgumentParser(prog="bis_client",
                                description="BIS（国际清算银行 SDMX 2.1）客户端")
    p.add_argument("--test", action="store_true", help="跑自检（真实端点调用）")
    a = p.parse_args(argv)
    if a.test:
        try:
            return _selftest()
        except BisApiError as exc:
            print(f"[FAIL] {exc}", file=sys.stderr)
            return 2
    p.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
