#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe_catalog_sources.py —— 为 catalog_data.yaml 探测五个源的可达性（取证脚本）。

为什么存在
----------
``catalog_data.yaml`` 里每一条 source 映射都必须是**真跑过一次取数**的结论，
不能靠猜代码或照着文档抄。本脚本就是那些结论的证据生成器：
把各源的指标目录拉回本地、拍平成可检索的索引，再按关键词列出候选，
最后由人来挑出「真的能取到数据」的组合。

产物（都落在 ``data/raw/_probe_catalog/``，该目录被 .gitignore 忽略）
--------------------------------------------------------------------
* ``nbs_tree_<code>.json`` —— NBS 指标目录树原文（1=月度 / 2=季度 / 3=年度）
* ``nbs_index.json``       —— 拍平后的 NBS 指标节点（_id / 名称 / 父链 / 层级 / 类型）
* ``wb_index.json``        —— World Bank 全量指标目录（29544 条，只留 id + name）
* ``imf_index.json``       —— IMF DataMapper 目录（132 条）
* ``bis_dataflows.json``   —— BIS 全部 dataflow（id / name）
* ``fred_probe.json``      —— FRED 候选序列 ID 的逐条可达性
* ``bis_probe.json``       —— BIS 候选 dataset+key 的逐条可达性

用法
----
    .venv\\Scripts\\python.exe python\\_probes\\probe_catalog_sources.py nbs
    ... wb | imf | bis | fred | all

**只读探测**：只调 GET/POST 取数接口，不写 validated / processed / output 任何一层。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Optional

ROOT = Path(r"D:\universe\econ-data-harvester")
sys.path.insert(0, str(ROOT / "python"))

from econ_core import (  # noqa: E402
    bis_client, fred_client, imf_client, nbs_client, worldbank_client,
)

OUT = ROOT / "data" / "raw" / "_probe_catalog"
OUT.mkdir(parents=True, exist_ok=True)


def _dump(name: str, obj: Any) -> Path:
    """写一份证据 JSON（缩进 + 保留中文），返回路径。"""
    p = OUT / name
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def _log(msg: str) -> None:
    """人类可读日志走 stderr，与项目的 CLI 契约一致。"""
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# NBS
# --------------------------------------------------------------------------- #

def _flatten(nodes: Any, parent: list[str], out: list[dict[str, Any]],
             ancestors: Optional[list[dict[str, Any]]] = None) -> None:
    """把 NBS 目录树递归拍平成节点列表（保留父链与祖先，便于人工判断口径）。

    ``root_id`` 取**一级类目**的 ``_id``（``fetch_indicator_data`` 需要的 ``root_id``
    就是它），``treeinfo_pid`` 即 ``cid``（见 ``nbs_client.fetch_indicator_data`` 的
    docstring：cid 通常是指标节点的 ``treeinfo_pid``）。
    """
    if not isinstance(nodes, list):
        return
    chain = list(ancestors or [])
    for n in nodes:
        if not isinstance(n, dict):
            continue
        name = str(n.get("name") or n.get("othername") or "")
        path = parent + ([name] if name else [])
        here = chain + [n]
        out.append({
            "_id": n.get("_id"),
            "name": name,
            "type": n.get("type"),
            "_level": n.get("_level"),
            "cid": n.get("treeinfo_pid"),
            "root_id": (here[0].get("_id") if here else None),
            "root_name": (str(here[0].get("name") or "") if here else ""),
            "path": " / ".join(path),
        })
        _flatten(n.get("children"), path, out, here)


def stage_nbs() -> dict[str, Any]:
    """拉三棵 NBS 指标目录树并拍平。"""
    summary: dict[str, Any] = {}
    all_nodes: list[dict[str, Any]] = []
    for code, label in (("1", "monthly"), ("2", "quarterly"), ("3", "annual")):
        tree = nbs_client.get_catalogs_and_index_tree(code=code)
        _dump(f"nbs_tree_{code}.json", tree)
        nodes: list[dict[str, Any]] = []
        _flatten(tree, [], nodes)
        for n in nodes:
            n["catalog_code"] = code
            n["catalog_label"] = label
        leaves = [n for n in nodes if n.get("type") == "indicator"]
        all_nodes.extend(nodes)
        summary[label] = {"nodes": len(nodes), "leaves": len(leaves)}
        _log(f"[nbs] code={code} ({label}): 节点 {len(nodes)}，指标叶 {len(leaves)}")
    p = _dump("nbs_index.json", all_nodes)
    _log(f"[nbs] 索引已写 {p}（{len(all_nodes)} 条）")
    return summary


def search_nbs(keywords: list[str], limit: int = 12) -> None:
    """在已拍平的 NBS 索引里按关键词打印候选（读本地文件，不联网）。

    一行一个命中：``名称 | _id | cid | root_id | 目录 | 父链``。
    压成一行是为了能直接 grep / 贴进报告，不必在几百行里翻。
    """
    idx = json.loads((OUT / "nbs_index.json").read_text(encoding="utf-8"))
    leaves = [n for n in idx if n.get("type") == "indicator"]
    for kw in keywords:
        hits = [n for n in leaves if kw in (n.get("name") or "")]
        print(f"\n### NBS {kw}  -> {len(hits)} 个指标叶")
        for n in hits[:limit]:
            print(f"  {n['name']} | {n['_id']} | cid={n.get('cid')} | "
                  f"root={n.get('root_id')} | {n['catalog_label']} | {n['path']}")


#: 18 个目标指标在 NBS 目录树里的检索关键词（一个指标可给多个词，取并集人工判读）。
NBS_KEYWORDS: list[str] = [
    "国内生产总值", "人均国内生产总值", "居民消费价格指数", "失业率",
    "人口", "出口", "进口", "外商直接投资", "实际使用外资",
    "货币供应量", "工业增加值", "社会消费品零售总额", "固定资产投资",
    "外汇储备", "政府债务", "汇率", "国债",
]


def stage_nbs_search() -> dict[str, Any]:
    """（离线）从已落盘的原始树重建**带 root_id/cid 的**索引，并按关键词检索。"""
    all_nodes: list[dict[str, Any]] = []
    for code, label in (("1", "monthly"), ("2", "quarterly"), ("3", "annual")):
        tree = json.loads((OUT / f"nbs_tree_{code}.json").read_text(encoding="utf-8"))
        nodes: list[dict[str, Any]] = []
        _flatten(tree, [], nodes)
        for n in nodes:
            n["catalog_code"] = code
            n["catalog_label"] = label
        all_nodes.extend(nodes)
        _log(f"[nbs] code={code} ({label}): 节点 {len(nodes)}")
    p = _dump("nbs_index.json", all_nodes)
    _log(f"[nbs] 索引（含 root_id/cid）已写 {p}（{len(all_nodes)} 条）")
    search_nbs(NBS_KEYWORDS)
    return {"n_nodes": len(all_nodes),
            "n_leaves": sum(1 for n in all_nodes if n.get("type") == "indicator")}


# --------------------------------------------------------------------------- #
# World Bank
# --------------------------------------------------------------------------- #

def stage_wb() -> dict[str, Any]:
    """拉全量 World Bank 指标目录（2 页 × 20000），只留 id + name。"""
    catalog = worldbank_client._fetch_all_indicators(  # noqa: SLF001
        True, worldbank_client.PARSED_DIR)
    idx = [{"id": str(e.get("id") or ""), "name": str(e.get("name") or "")}
           for e in catalog if isinstance(e, dict)]
    _dump("wb_index.json", idx)
    _log(f"[wb] 全量目录 {len(idx)} 条 -> {OUT / 'wb_index.json'}")
    return {"n_indicators": len(idx)}


def search_wb(keywords: list[str], limit: int = 8) -> None:
    """在本地 WB 索引里按**代码前缀/名称**打印候选。"""
    idx = json.loads((OUT / "wb_index.json").read_text(encoding="utf-8"))
    for kw in keywords:
        k = kw.lower()
        hits = [e for e in idx if k in e["name"].lower() or k in e["id"].lower()]
        print(f"\n=== World Bank 关键词 {kw!r}：{len(hits)} 条命中 ===")
        for e in hits[:limit]:
            print(f"  {e['id']:<32} {e['name']}")


#: 18 个目标指标在 World Bank 目录里的检索关键词。
WB_KEYWORDS: list[str] = [
    "GDP (current US$)", "GDP growth (annual %)", "GDP per capita (current US$)",
    "Inflation, consumer prices (annual %)", "Unemployment, total (% of total labor force)",
    "Population, total", "Net trade in goods and services", "Merchandise exports",
    "Merchandise imports", "Foreign direct investment, net inflows",
    "Broad money", "Industry (including construction), value added",
    "Retail trade", "Gross fixed capital formation", "Total reserves",
    "Central government debt", "Official exchange rate",
]


def stage_wb_search() -> dict[str, Any]:
    """（离线）在 WB 全量目录里按 18 个目标指标的关键词检索。"""
    search_wb(WB_KEYWORDS)
    idx = json.loads((OUT / "wb_index.json").read_text(encoding="utf-8"))
    return {"n_indicators": len(idx)}


# --------------------------------------------------------------------------- #
# IMF
# --------------------------------------------------------------------------- #

def stage_imf() -> dict[str, Any]:
    """拉 IMF DataMapper 目录（132 条）并全量打印。"""
    inds = imf_client.list_indicators()
    idx = [{"id": str(e.get("id") or ""), "name": str(e.get("name") or ""),
            "unit": str(e.get("unit") or "")} for e in inds if isinstance(e, dict)]
    _dump("imf_index.json", idx)
    print(f"=== IMF DataMapper 目录：{len(idx)} 条 ===")
    for e in idx:
        print(f"  {e['id']:<14} {e['name']}  [{e['unit']}]")
    return {"n_indicators": len(idx)}


# --------------------------------------------------------------------------- #
# BIS
# --------------------------------------------------------------------------- #

#: BIS 候选探测：dataset + 必须给满三位的 FREQ.REF_AREA.UNIT_MEASURE 密钥。
BIS_CANDIDATES: list[tuple[str, str, str]] = [
    ("WS_LONG_CPI", "M.CN.771", "已知可用：中国 CPI 同比（月度）"),
    ("WS_LONG_CPI", "M.CN.628", "已知可用：中国 CPI 指数 2010=100（月度）"),
    ("WS_LONG_CPI", "A.CN.771", "同一序列的**年度**粒度"),
    ("WS_LONG_PPI", "M.CN.771", "生产者价格指数？待验"),
    ("WS_LONG_RPPI", "M.CN.771", "住宅物业价格？待验"),
    ("WS_EER", "M.CN.771", "名义有效汇率？待验"),
    ("WS_EER", "M.CN", "两位密钥是否可行（对照：应为 404）"),
    ("WS_DSR", "M.CN.771", "债务偿还率？待验"),
    ("WS_CREDIT_GAP", "M.CN.771", "信贷缺口？待验"),
    ("WS_SPP", "M.CN.771", "住宅物业价格（另一种 dataflow 名）"),
    ("WS_LONG_NAT_ACCT", "M.CN.771", "国民账户？待验"),
    ("WS_XRU", "M.CN.USD", "汇率（XRU）？待验"),
]


def stage_bis() -> dict[str, Any]:
    """列 BIS 全部 dataflow，并逐条探测候选密钥的可达性。"""
    flows = bis_client.list_dataflows()
    idx = [{"id": str(f.get("id") or ""), "name": str(f.get("name") or ""),
            "version": str(f.get("version") or "")} for f in flows if isinstance(f, dict)]
    _dump("bis_dataflows.json", idx)
    print(f"=== BIS dataflow：{len(idx)} 条 ===")
    for f in idx:
        print(f"  {f['id']:<24} {f['name']}")

    results: list[dict[str, Any]] = []
    print("\n=== BIS 候选密钥逐条探测 ===")
    for dataset, key, note in BIS_CANDIDATES:
        entry: dict[str, Any] = {"dataset": dataset, "key": key, "note": note}
        try:
            rows = bis_client.fetch_series(dataset, key)
            entry["ok"] = True
            entry["n_rows"] = len(rows)
            entry["first"] = rows[0] if rows else None
            entry["last"] = rows[-1] if rows else None
        except Exception as exc:  # noqa: BLE001 —— 探测脚本，任何异常都只是"这一条不可用"
            entry["ok"] = False
            entry["error"] = f"{type(exc).__name__}: {exc}"
        results.append(entry)
        verdict = (f"OK  {entry['n_rows']} 行 {entry['first']} .. {entry['last']}"
                   if entry["ok"] else f"FAIL {entry['error'][:110]}")
        print(f"  {dataset:<20} {key:<12} {verdict}")
    _dump("bis_probe.json", results)
    return {"n_dataflows": len(idx),
            "n_ok": sum(1 for r in results if r["ok"])}


# --------------------------------------------------------------------------- #
# FRED
# --------------------------------------------------------------------------- #

#: FRED 没有免密钥搜索接口（list_search 恒空），只能按候选 ID 逐条试。
FRED_CANDIDATES: list[tuple[str, str]] = [
    ("CHNCPIALLMINMEI", "已知可用：中国 CPI 全项指数（月度，2015=100）"),
    ("MKTGDPCNA646NWDB", "GDP 现价美元（WDI 镜像）"),
    ("NYGDPPCAPCDCHN", "人均 GDP 现价美元（WDI 镜像）"),
    ("POPTOTCHA647NWDB", "人口总数（WDI 镜像）"),
    ("CHNGDPNQDSMEI", "GDP 名义季度（OECD MEI 镜像）"),
    ("XTEXVA01CNM667S", "出口额（OECD MEI 镜像）"),
    ("XTIMVA01CNM667S", "进口额（OECD MEI 镜像）"),
    ("CPALTT01CNM659N", "CPI 同比（OECD MEI 镜像）"),
    ("LRUNTTTTCHA156S", "失业率（OECD）"),
    ("CHNB6BLTT02STSAQ", "BIS 口径？待验"),
    ("MYAGM2CNM189N", "M2 货币供应（IMF IFS 镜像）"),
    ("TRESEGCNM052N", "外汇储备（IMF IFS 镜像）"),
]


def stage_fred() -> dict[str, Any]:
    """逐条探测 FRED 候选序列 ID 的可达性（真跑取数）。"""
    results: list[dict[str, Any]] = []
    print("=== FRED 候选序列逐条探测 ===")
    for sid, note in FRED_CANDIDATES:
        entry: dict[str, Any] = {"series_id": sid, "note": note}
        try:
            rows = fred_client.fetch_series(sid)
            entry["ok"] = True
            entry["n_rows"] = len(rows)
            entry["first"] = rows[0] if rows else None
            entry["last"] = rows[-1] if rows else None
            entry["frequency"] = fred_client.last_meta().get("frequency")
        except Exception as exc:  # noqa: BLE001
            entry["ok"] = False
            entry["error"] = f"{type(exc).__name__}: {exc}"
        results.append(entry)
        verdict = (f"OK  {entry['n_rows']} 行 freq={entry.get('frequency')} "
                   f"{entry['first']} .. {entry['last']}"
                   if entry["ok"] else f"FAIL {entry['error'][:110]}")
        print(f"  {sid:<20} {verdict}")
    _dump("fred_probe.json", results)
    return {"n_ok": sum(1 for r in results if r["ok"]), "n_total": len(results)}


# --------------------------------------------------------------------------- #
# NBS 定标：为 18 个目标指标解析 (cid, indicator_id, root_id) 三元组
# --------------------------------------------------------------------------- #

#: ``root_id`` **不是**指标的一级祖先，而是「该目录的第一条 level-2 类目」的 `_id` ——
#: 三个目录各有一个常量（实测对照见 data/raw/_probe_catalog/nbs_search.txt）：
#:   code=1 月度 -> 3c9c459384c74f578f3541b2198aac70（价格指数）
#:   code=2 季度 -> 1b1ce0cfd03646dc9e15103ea4c570f6（国民经济核算）
#:   code=3 年度 -> 71d41888d5a44bb2a67402ef4e60003e（综合）
#: 与 scan-missing.py 里已跑通的 NBS_GDP / NBS_REG（71d41888…）和
#: NBS_SUR（3c9c4593…）逐个对上，所以这里是**复核**而不是新猜想。
NBS_ROOT_BY_CODE: dict[str, str] = {
    "1": "3c9c459384c74f578f3541b2198aac70",
    "2": "1b1ce0cfd03646dc9e15103ea4c570f6",
    "3": "71d41888d5a44bb2a67402ef4e60003e",
}

#: (指标键, 目录 code, 名称正则) —— 正则故意写严，宁可不命中也不要错配到别的口径。
#: **结尾一律用 ``\\s*$`` 而不是 ``$``**：NBS 树里的 name 大量带尾随空格，
#: 用 ``$`` 会一个都匹配不到（第一次跑就是这么全空的）。
NBS_TARGETS: list[tuple[str, str, str]] = [
    ("GDP", "3", r"^国内生产总值 \(亿元\)\s*$"),
    ("GDP_GROWTH", "3", r"^国内生产总值指数 \(上年=100\)\s*$"),
    ("GDP_PER_CAPITA", "3", r"^人均国内生产总值 \(元\)\s*$"),
    ("CPI", "3", r"居民消费价格指数 \(上年=100\)\s*$"),
    ("CPI", "1", r"^居民消费价格指数 \(上年同月=100\)\s*$"),
    ("POPULATION", "3", r"^年末总人口"),
    ("EXPORTS", "3", r"^出口总额 \(人民币\)"),
    ("EXPORTS", "1", r"^出口总值当期值"),
    ("IMPORTS", "3", r"^进口总额 \(人民币\)"),
    ("IMPORTS", "1", r"^进口总值当期值"),
    ("TRADE_BALANCE", "3", r"进出口差额"),
    ("TRADE_BALANCE", "1", r"进出口差额|贸易差额"),
    ("M2", "3", r"^货币和准货币 \(M2\) 供应量 \(亿元\)\s*$"),
    ("FIXED_ASSET_INVESTMENT", "3", r"^全社会固定资产投资 \(亿元\)\s*$"),
    ("FIXED_ASSET_INVESTMENT", "1", r"^固定资产投资 \(不含农户\)"),
    ("FX_RESERVES", "3", r"^外汇储备 \(亿美元\)\s*$"),
    ("EXCHANGE_RATE", "3", r"^人民币对美元汇率"),
    ("INDUSTRIAL_PRODUCTION", "1", r"^规上工业增加值同比增长"),
    ("RETAIL_SALES", "1", r"^社会消费品零售总额当期值"),
    ("UNEMPLOYMENT", "1", r"^全国城镇调查失业率"),
    ("UNEMPLOYMENT", "3", r"^城镇登记失业率"),
    ("FDI", "1", r"^实际利用外商直接投资金额累计值"),
    ("FDI", "3", r"外商直接投资"),
]


def stage_nbs_targets() -> dict[str, Any]:
    """（离线）为 18 个目标指标在 NBS 索引里列候选三元组。"""
    idx = json.loads((OUT / "nbs_index.json").read_text(encoding="utf-8"))
    leaves = [n for n in idx if n.get("type") == "indicator"]
    found: dict[str, Any] = {}
    for key, code, pattern in NBS_TARGETS:
        rx = re.compile(pattern)
        hits = [n for n in leaves
                if n.get("catalog_code") == code and rx.search(n.get("name") or "")]
        found[key] = [{"name": n["name"], "indicator_id": n["_id"], "cid": n.get("cid"),
                       "root_id": NBS_ROOT_BY_CODE[code], "catalog": n["catalog_label"],
                       "path": n["path"]} for n in hits[:10]]
        print(f"\n### {key}  (code={code})  -> {len(hits)} 个候选")
        for h in found[key]:
            print(f"  {h['name']} | id={h['indicator_id']} | cid={h['cid']} | "
                  f"root={h['root_id']} | {h['path']}")
    _dump("nbs_targets.json", found)
    return {"n_keys": len(NBS_TARGETS),
            "n_with_hits": sum(1 for v in found.values() if v)}


# --------------------------------------------------------------------------- #
# 终验：把候选映射逐条**真跑取数**
# --------------------------------------------------------------------------- #

#: NBS 三元组（cid / indicator_id / root_id）—— 全部由 nbs_targets 阶段从目录树解析得到。
#: 键是 catalog_data.yaml 里的源内标识。
NBS_SPECS: dict[str, dict[str, Any]] = {
    "nbs|gdp|annual": {"cid": "f7fd25aaad184414875632cf2327da60",
                       "indicator_id": "7dc6a2ee6c614960b7059991e0cc4d96",
                       "root_id": NBS_ROOT_BY_CODE["3"], "frequency": "annual"},
    "nbs|gdp_index|annual": {"cid": "489888799f8d470786bc01a4057efc38",
                             "indicator_id": "93dd15c8a3a3400ea89f8dceec7ab2b3",
                             "root_id": NBS_ROOT_BY_CODE["3"], "frequency": "annual"},
    "nbs|gdp_per_capita|annual": {"cid": "f7fd25aaad184414875632cf2327da60",
                                  "indicator_id": "eb4d93f19d57495c89f98875e03e01be",
                                  "root_id": NBS_ROOT_BY_CODE["3"], "frequency": "annual"},
    "nbs|cpi|annual": {"cid": "e5f37eced5de4d4c815f7ac5f59fc6c2",
                       "indicator_id": "5e3053f110074dcdbcfa3c8428dd1367",
                       "root_id": NBS_ROOT_BY_CODE["3"], "frequency": "annual"},
    "nbs|cpi|monthly": {"cid": "5c7452825c7c4dcba391db5ca7f335c5",
                        "indicator_id": "53180dfb9c14411ba4b762307c85920c",
                        "root_id": NBS_ROOT_BY_CODE["1"], "frequency": "monthly"},
    "nbs|population|annual": {"cid": "6331ad868e8b4f55b8e9b6e765609ce1",
                              "indicator_id": "806083491dbe46a08995783945a30b9d",
                              "root_id": NBS_ROOT_BY_CODE["3"], "frequency": "annual"},
    "nbs|exports|annual": {"cid": "333332810f574043bc40553902c2f385",
                           "indicator_id": "3529a0abf1324839942a3ec3a7059bcc",
                           "root_id": NBS_ROOT_BY_CODE["3"], "frequency": "annual"},
    "nbs|imports|annual": {"cid": "333332810f574043bc40553902c2f385",
                           "indicator_id": "752ae251a0494227b593779a098417c7",
                           "root_id": NBS_ROOT_BY_CODE["3"], "frequency": "annual"},
    "nbs|trade_balance|annual": {"cid": "333332810f574043bc40553902c2f385",
                                 "indicator_id": "d277c986002b459b9c551499dd0a309d",
                                 "root_id": NBS_ROOT_BY_CODE["3"], "frequency": "annual"},
    "nbs|m2|annual": {"cid": "b4654f05bda2472a9acf7488086971c3",
                      "indicator_id": "f7fe1af1ef3c4046a62fefd5f0c2be6b",
                      "root_id": NBS_ROOT_BY_CODE["3"], "frequency": "annual"},
    "nbs|fai|annual": {"cid": "607a7cd4b50e4553894472475d1f4273",
                       "indicator_id": "ba8a0fbdae644f44be836143bbc63f4e",
                       "root_id": NBS_ROOT_BY_CODE["3"], "frequency": "annual"},
    "nbs|fx_reserves|annual": {"cid": "09ac2b6323854202bdd7e6baffcf7222",
                               "indicator_id": "90b9649778434698a8ccabd35a895a8f",
                               "root_id": NBS_ROOT_BY_CODE["3"], "frequency": "annual"},
    "nbs|exchange_rate|annual": {"cid": "844094d7c9b842f8b84b2e7aebbaf97d",
                                 "indicator_id": "cd3b99ca016e4e0b94cd4c5647228df6",
                                 "root_id": NBS_ROOT_BY_CODE["3"], "frequency": "annual"},
    "nbs|industrial_production|monthly": {"cid": "3f2e14f0542348ed9fe02476eca3450b",
                                          "indicator_id": "ef1b1765960d45a29b4d7c4ca91be916",
                                          "root_id": NBS_ROOT_BY_CODE["1"],
                                          "frequency": "monthly"},
    "nbs|retail_sales|monthly": {"cid": "d0cb882c7f27443ab6b3ef9421901961",
                                 "indicator_id": "1142a3a03e9045959e606a21822641ac",
                                 "root_id": NBS_ROOT_BY_CODE["1"], "frequency": "monthly"},
    "nbs|surveyed_unemployment|monthly": {"cid": "ee3b7046b390415b9b7745e3d16f6052",
                                          "indicator_id": "3888eac6062945a79c8a27e5f13d4953",
                                          "root_id": NBS_ROOT_BY_CODE["1"],
                                          "frequency": "monthly"},
    "nbs|registered_unemployment|annual": {"cid": "19839dbd8e82481b9524f031c6d816c5",
                                           "indicator_id": "456ef2f3d75f472b930384237bc6100f",
                                           "root_id": NBS_ROOT_BY_CODE["3"],
                                           "frequency": "annual"},
    "nbs|fdi|monthly": {"cid": "aea8dbdf934e4b6ea154e679c8d4a0e9",
                        "indicator_id": "d21fa5bc9975422aaf076bf2e525269d",
                        "root_id": NBS_ROOT_BY_CODE["1"], "frequency": "monthly"},
}

#: 待终验的完整映射：指标 -> 源 -> 该源的参数。
#: 这一份**就是** catalog_data.yaml 的草稿，终验通过后逐字转录。
VERIFY_MAP: dict[str, dict[str, dict[str, Any]]] = {
    "GDP": {
        "nbs": {"nbs_key": "nbs|gdp|annual"},
        "worldbank": {"indicator_code": "NY.GDP.MKTP.CN"},
        "imf": {"indicator_code": "NGDPD"},
        "fred": {"series_id": "MKTGDPCNA646NWDB"},
    },
    "GDP_GROWTH": {
        "nbs": {"nbs_key": "nbs|gdp_index|annual"},
        "worldbank": {"indicator_code": "NY.GDP.MKTP.KD.ZG"},
        "imf": {"indicator_code": "NGDP_RPCH"},
    },
    "GDP_PER_CAPITA": {
        "nbs": {"nbs_key": "nbs|gdp_per_capita|annual"},
        "worldbank": {"indicator_code": "NY.GDP.PCAP.CD"},
        "imf": {"indicator_code": "NGDPDPC"},
    },
    "CPI": {
        "nbs": {"nbs_key": "nbs|cpi|annual"},
        "nbs_monthly": {"nbs_key": "nbs|cpi|monthly"},
        "worldbank": {"indicator_code": "FP.CPI.TOTL"},
        "fred": {"series_id": "CHNCPIALLMINMEI"},
        "bis": {"dataset": "WS_LONG_CPI", "key": "M.CN.628"},
    },
    "INFLATION": {
        "worldbank": {"indicator_code": "FP.CPI.TOTL.ZG"},
        "imf": {"indicator_code": "PCPIPCH"},
        "fred": {"series_id": "CPALTT01CNM659N"},
        "bis": {"dataset": "WS_LONG_CPI", "key": "M.CN.771"},
    },
    "UNEMPLOYMENT": {
        "nbs": {"nbs_key": "nbs|surveyed_unemployment|monthly"},
        "nbs_registered": {"nbs_key": "nbs|registered_unemployment|annual"},
        "worldbank": {"indicator_code": "SL.UEM.TOTL.ZS"},
        "imf": {"indicator_code": "LUR"},
        "fred": {"series_id": "LRUNTTTTCHA156S"},
    },
    "POPULATION": {
        "nbs": {"nbs_key": "nbs|population|annual"},
        "worldbank": {"indicator_code": "SP.POP.TOTL"},
        "imf": {"indicator_code": "LP"},
        "fred": {"series_id": "POPTOTCHA647NWDB"},
    },
    "TRADE_BALANCE": {
        "nbs": {"nbs_key": "nbs|trade_balance|annual"},
        "worldbank": {"indicator_code": "NE.RSB.GNFS.CD"},
    },
    "EXPORTS": {
        "nbs": {"nbs_key": "nbs|exports|annual"},
        "worldbank": {"indicator_code": "NE.EXP.GNFS.CD"},
        "fred": {"series_id": "XTEXVA01CNM667S"},
        # IMF 的 BX_GDP（出口占 GDP 比重）实测对中国返回 **0 行**，故不收录。
        # 保留在 VERIFY_MAP 里是为了让证据文件记下"探过、不可用"，而不是悄悄不留痕。
        "imf": {"indicator_code": "BX_GDP", "expect_unavailable": True},
    },
    "IMPORTS": {
        "nbs": {"nbs_key": "nbs|imports|annual"},
        "worldbank": {"indicator_code": "NE.IMP.GNFS.CD"},
        "fred": {"series_id": "XTIMVA01CNM667S"},
        "imf": {"indicator_code": "BM_GDP", "expect_unavailable": True},
    },
    "FDI": {
        # NBS 的这条序列实测**停更于 2019-11**（见 diag_range：2020-01 起全是占位行），
        # 所以验证窗口收到 2015-2019 —— 这段才是它真有的数据。
        "nbs": {"nbs_key": "nbs|fdi|monthly", "verify_years": [2015, 2019]},
        "worldbank": {"indicator_code": "BX.KLT.DINV.CD.WD"},
        "imf": {"indicator_code": "DirectIn"},
    },
    "M2": {
        "nbs": {"nbs_key": "nbs|m2|annual"},
        "worldbank": {"indicator_code": "FM.LBL.BMNY.CN"},
        "fred": {"series_id": "MYAGM2CNM189N"},
    },
    "INDUSTRIAL_PRODUCTION": {
        "nbs": {"nbs_key": "nbs|industrial_production|monthly"},
        "worldbank": {"indicator_code": "NV.IND.TOTL.ZS"},
    },
    "RETAIL_SALES": {
        "nbs": {"nbs_key": "nbs|retail_sales|monthly"},
    },
    "FIXED_ASSET_INVESTMENT": {
        "nbs": {"nbs_key": "nbs|fai|annual"},
        "worldbank": {"indicator_code": "NE.GDI.FTOT.CN"},
    },
    "FX_RESERVES": {
        "nbs": {"nbs_key": "nbs|fx_reserves|annual"},
        "worldbank": {"indicator_code": "FI.RES.TOTL.CD"},
        "fred": {"series_id": "TRESEGCNM052N"},
    },
    "GOVERNMENT_DEBT": {
        # 世界银行的中央政府债务对中国**全窗口 0 非空**（1960-2025 共 66 行全 null，
        # 见 diag_wb.json），不是"窗口选窄了"，所以不收录。
        "worldbank": {"indicator_code": "GC.DOD.TOTL.GD.ZS", "expect_unavailable": True},
        "imf": {"indicator_code": "GGXWDG_NGDP"},
    },
    "EXCHANGE_RATE": {
        "nbs": {"nbs_key": "nbs|exchange_rate|annual"},
        "worldbank": {"indicator_code": "PA.NUS.FCRF"},
    },
}

#: 终验用的默认窗口：年度 2015-2024（10 期）；月度也走这个年份区间（12 期/年）。
#: 用固定窗口而不是"不限"，是为了让每个源的**返回行数可比**。
#: 需要别的区间时用映射里的 ``verify_years``（闭区间）覆盖，别改这两个常量。
VERIFY_YEARS: list[str] = [str(y) for y in range(2015, 2025)]

#: 只给诊断阶段用的"2024 全年"月度窗口（对照实验要固定看同一年）。
VERIFY_MONTHS: list[str] = [f"2024{m:02d}" for m in range(1, 13)]


def _verify_nbs(spec: dict[str, Any]) -> dict[str, Any]:
    """真跑一次 NBS 取数（POST getEsDataByIndicatorIdAndDa）。

    **必须数"非空值"而不是"行数"**：NBS 对某些期会回**占位行**（``dt_name`` 有值、
    ``v`` 是空串 —— 例如 1 月不单独发布的社会消费品零售总额、以及已停更的序列）。
    只数行数会把这类"有行无值"当成通过（第一版终验就这么放过了一条）。

    spec 里可选 ``verify_years``：用于**已停更**的序列（NBS 的外商直接投资止于
    2019-11），把验证窗口收到它真有数据的年份，否则"窗口内全空"会被误读成
    "源不提供这个指标"——那是两回事，见 ``notes``。
    """
    p = NBS_SPECS[spec["nbs_key"]]
    freq = p["frequency"]
    # verify_years 是**闭区间** (起, 止)，不是"只验这两年"。
    # 第一版直接 `for y in spec["verify_years"]`，于是 [2015, 2019] 只取了 2015 与 2019
    # 两年（24 期），却在报告里看着像覆盖了 2015-2019 五年 —— 证据的窗口必须和它
    # 声称的一致，否则读的人会高估验证范围。
    if spec.get("verify_years"):
        y0, y1 = spec["verify_years"]
        years = [str(y) for y in range(int(y0), int(y1) + 1)]
    else:
        years = VERIFY_YEARS
    dts = ([f"{y}YY" for y in years] if freq == "annual"
           else [f"{y}{m:02d}MM" for y in years for m in range(1, 13)])
    raw = nbs_client.fetch_indicator_data(
        cid=p["cid"], indicator_id=p["indicator_id"], root_id=p["root_id"],
        da="000000000000", dts=dts)
    filled = [r for r in raw if str(r.get("v") or "").strip()]
    return {"kind": "nbs", "frequency": freq, "window": years,
            "n_rows": len(raw), "n_nonempty": len(filled),
            "first_nonempty": filled[0] if filled else None,
            "last_nonempty": filled[-1] if filled else None,
            "sample": raw[0] if raw else None}


def _verify_wb(spec: dict[str, Any]) -> dict[str, Any]:
    """真跑一次 World Bank 取数。"""
    rows = worldbank_client.fetch_indicator("CHN", spec["indicator_code"], "2015:2024")
    vals = [r for r in rows if r.get("value") is not None]
    return {"kind": "worldbank",
            "n_rows": len(rows), "n_non_null": len(vals),
            "first": {"date": rows[0]["date"], "value": rows[0]["value"]} if rows else None,
            "last": {"date": rows[-1]["date"], "value": rows[-1]["value"]} if rows else None,
            "unit_hint": (rows[0].get("indicator") or {}).get("value") if rows else None}


def _verify_imf(spec: dict[str, Any]) -> dict[str, Any]:
    """真跑一次 IMF DataMapper 取数。"""
    rows = imf_client.fetch_indicator(spec["indicator_code"], "CHN")
    years = [r["period"] for r in rows if r.get("value") is not None]
    real = [y for y in years if y <= "2024"]
    return {"kind": "imf", "n_rows": len(rows),
            "n_non_null": len(years),
            "n_actual_to_2024": len(real),
            "first": rows[0] if rows else None,
            "last": rows[-1] if rows else None,
            "note": "IMF 含预测值，实际值需自己截年份"}


def _verify_fred(spec: dict[str, Any]) -> dict[str, Any]:
    """真跑一次 FRED 取数。"""
    rows = fred_client.fetch_series(spec["series_id"])
    vals = [r for r in rows if r.get("value") is not None]
    return {"kind": "fred", "frequency": fred_client.last_meta().get("frequency"),
            "n_rows": len(rows), "n_non_null": len(vals),
            "first": rows[0] if rows else None, "last": rows[-1] if rows else None}


def _verify_bis(spec: dict[str, Any]) -> dict[str, Any]:
    """真跑一次 BIS SDMX 取数。"""
    rows = bis_client.fetch_series(spec["dataset"], spec["key"])
    vals = [r for r in rows if r.get("value") is not None]
    return {"kind": "bis", "frequency": bis_client.last_meta().get("frequency"),
            "n_rows": len(rows), "n_non_null": len(vals),
            "first": rows[0] if rows else None, "last": rows[-1] if rows else None}


_VERIFIERS = {"nbs": _verify_nbs, "worldbank": _verify_wb, "imf": _verify_imf,
              "fred": _verify_fred, "bis": _verify_bis}


def stage_verify() -> dict[str, Any]:
    """把 VERIFY_MAP 里每一条映射**真跑一次取数**，产出 catalog 的证据表。

    判定标准是「**拿到了多少个非空值**」，不是「回了多少行」。这两者在本项目里
    实测会分叉：NBS 对未发布/已停更的期会回占位行（行数正常、值全空），
    World Bank 也可能回 ``value: null`` 的行。只数行数会得到假通过。
    """
    report: dict[str, Any] = {}
    n_ok = n_fail = n_na = 0
    for indicator, sources in VERIFY_MAP.items():
        report[indicator] = {}
        for source, spec in sources.items():
            kind = "nbs" if source.startswith("nbs") else source
            # expect_unavailable：**探过并且确认不可用**的映射。留在表里是为了让证据
            # 文件记下"探过、结论是不可用"，而不是查不到痕迹 —— 后人不会重复探一遍。
            expect_na = bool(spec.get("expect_unavailable"))
            try:
                info = _VERIFIERS[kind](spec)
                info["ok"] = True
                info["spec"] = spec
                n_data = int(info.get("n_nonempty") or info.get("n_non_null") or 0)
                info["n_data"] = n_data
                if n_data == 0:
                    info["ok"] = False
                    if expect_na:
                        info["expected_unavailable"] = True
                        info["error"] = "探过：该源对中国没有这个指标的数据（已确认，未收录）"
                        n_na += 1
                        print(f"  N/A  {indicator:<22} {source:<16} "
                              f"行={info['n_rows']} 非空=0  <- 已确认不可用，未收录")
                    else:
                        info["error"] = "取数成功但**没有任何非空值**（占位行/序列停更）"
                        n_fail += 1
                        print(f"  EMPTY {indicator:<22} {source:<16} "
                              f"行={info['n_rows']} 非空=0  <- 不可用")
                else:
                    if expect_na:
                        info["unexpectedly_available"] = True
                    n_ok += 1
                    head = info.get("first_nonempty") or info.get("first") or info.get("sample")
                    print(f"  OK   {indicator:<22} {source:<16} "
                          f"行={info['n_rows']:<5} 非空={n_data:<5} {head}")
            except Exception as exc:  # noqa: BLE001 —— 探测脚本：失败就是"这条映射不可用"
                info = {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                        "spec": spec, "kind": kind, "n_data": 0}
                n_fail += 1
                print(f"  FAIL {indicator:<22} {source:<16} {info['error'][:100]}")
            report[indicator][source] = info
    _dump("verified.json", report)
    print(f"\n=== 终验：{n_ok} 条可取数 / {n_na} 条确认不可用（未收录） / {n_fail} 条意外失败 ===")
    return {"n_ok": n_ok, "n_na": n_na, "n_fail": n_fail, "n_indicators": len(VERIFY_MAP)}


# --------------------------------------------------------------------------- #
# 诊断：三个 NBS 月度指标回了 12 行**空值**
# --------------------------------------------------------------------------- #

#: 终验里出现「有行无值」的三个 NBS 月度指标（行数 12，但 v/i/du 全是空串）。
#: 这三个是**假通过** —— 行数看着对，内容为空。必须查清，不能就这么写进清单。
NBS_EMPTY_SUSPECTS: dict[str, str] = {
    "nbs|fdi|monthly": "实际利用外商直接投资金额累计值 (百万美元)",
    "nbs|industrial_production|monthly": "规上工业增加值同比增长 (%)",
    "nbs|retail_sales|monthly": "社会消费品零售总额当期值 (亿元)",
}


def stage_diag_nbs() -> dict[str, Any]:
    """对「有行无值」的三个指标做对照实验：换 dt / 换 dts / 看数据表目录。"""
    out: dict[str, Any] = {}
    for key, label in NBS_EMPTY_SUSPECTS.items():
        p = NBS_SPECS[key]
        dts = [f"{m}MM" for m in VERIFY_MONTHS]
        print(f"\n{'=' * 78}\n### {key}  {label}\n{'=' * 78}")
        # (a) 数据表目录：cid 上的「指标 -> 数据表」树
        try:
            tree = nbs_client.get_catalog_tree(p["cid"])
            print(f"  [a] get_catalog_tree(cid={p['cid'][:12]}…) -> {len(tree)} 个节点")
            for node in tree[:4]:
                print(f"      {json.dumps(node, ensure_ascii=False)[:220]}")
        except Exception as exc:  # noqa: BLE001
            print(f"  [a] get_catalog_tree 失败: {type(exc).__name__}: {exc}")
            tree = []

        # (b) 把数据表 id 当 dt 再取一次
        dt_candidates: list[str] = []
        for node in tree:
            for field in ("publicrelease_web_dacatalog_id", "_id"):
                v = node.get(field)
                if v:
                    dt_candidates.append(str(v))
        tried: list[dict[str, Any]] = []
        for dt in ["", *dt_candidates[:3]]:
            try:
                raw = nbs_client.fetch_indicator_data(
                    cid=p["cid"], indicator_id=p["indicator_id"], root_id=p["root_id"],
                    da="000000000000", dts=dts, dt=dt)
                nonempty = sum(1 for r in raw if str(r.get("v") or "").strip())
                tried.append({"dt": dt, "n_rows": len(raw), "n_nonempty": nonempty,
                              "sample": raw[0] if raw else None})
                print(f"  [b] dt={dt!r:<34} 行={len(raw):<4} 非空={nonempty}")
            except Exception as exc:  # noqa: BLE001
                tried.append({"dt": dt, "error": f"{type(exc).__name__}: {exc}"})
                print(f"  [b] dt={dt!r:<34} 失败 {type(exc).__name__}: {str(exc)[:60]}")
        out[key] = {"label": label, "spec": p, "dt_trials": tried,
                    "catalog_tree_n": len(tree)}
        # (c) 年份扫描：空值可能是「该期本来就不发布」，也可能是「整条序列停更」。
        #     只看一年分不清，扫几个年份才分得清。
        sweep: list[dict[str, Any]] = []
        for year in (2015, 2018, 2021, 2024):
            dts_y = [f"{year}{m:02d}MM" for m in range(1, 13)]
            try:
                raw = nbs_client.fetch_indicator_data(
                    cid=p["cid"], indicator_id=p["indicator_id"], root_id=p["root_id"],
                    da="000000000000", dts=dts_y)
                nonempty = sum(1 for r in raw if str(r.get("v") or "").strip())
                sweep.append({"year": year, "n_rows": len(raw), "n_nonempty": nonempty})
                print(f"  [c] {year} 全年 12 期 -> 行={len(raw):<4} 非空={nonempty}")
            except Exception as exc:  # noqa: BLE001
                sweep.append({"year": year, "error": f"{type(exc).__name__}: {exc}"})
        out[key]["year_sweep"] = sweep
    _dump("diag_nbs.json", out)
    return {"n_keys": len(out)}


def stage_diag_range() -> dict[str, Any]:
    """精确定位「有行无值」序列的**最后有值期**。

    年份扫描只能框到「2018 有、2021 无」，清单里要写清楚就得给准日子：
    一条请求把 2014-01..2026-12 全要回来，直接看最后一个非空期的标签。
    """
    out: dict[str, Any] = {}
    for key, label in NBS_EMPTY_SUSPECTS.items():
        p = NBS_SPECS[key]
        dts = [f"{y}{m:02d}MM" for y in range(2014, 2027) for m in range(1, 13)]
        raw = nbs_client.fetch_indicator_data(
            cid=p["cid"], indicator_id=p["indicator_id"], root_id=p["root_id"],
            da="000000000000", dts=dts)
        filled = [r for r in raw if str(r.get("v") or "").strip()]
        n_empty = len(raw) - len(filled)
        info = {"label": label, "n_requested": len(dts), "n_rows": len(raw),
                "n_nonempty": len(filled), "n_placeholder": n_empty,
                "first_nonempty": (filled[0].get("dt_name") if filled else None),
                "last_nonempty": (filled[-1].get("dt_name") if filled else None),
                "last_value": (filled[-1].get("v") if filled else None)}
        out[key] = info
        print(f"  {key:<38} 请求 {len(dts)} 期 -> 行 {len(raw)}、非空 {len(filled)}、"
              f"占位 {n_empty}")
        print(f"      首个有值期={info['first_nonempty']}  最后有值期={info['last_nonempty']}"
              f"  末值={info['last_value']}")
    _dump("diag_range.json", out)
    return {"n_keys": len(out)}


def stage_diag_wb() -> dict[str, Any]:
    """对"窗口内全 null"的 World Bank 指标换更宽的年份再看一次。

    2015-2024 窗口里全空有两种可能：① 这个源根本没有中国的数据；
    ② 有数据但不在这个窗口里。**这两者对清单的含义完全不同**，
    必须扫宽窗口分清楚，不能拿一个窗口的空结果就下结论。
    """
    candidates = [
        ("GC.DOD.TOTL.GD.ZS", "中央政府债务 (% of GDP)"),
        ("NE.RSB.GNFS.CD", "货物和服务净出口 (现价美元)"),
    ]
    out: dict[str, Any] = {}
    for code, label in candidates:
        rows = worldbank_client.fetch_indicator("CHN", code, "1960:2025")
        filled = [r for r in rows if r.get("value") is not None]
        years = sorted(r["date"] for r in filled)
        info = {"label": label, "n_rows": len(rows), "n_non_null": len(filled),
                "first_year": years[0] if years else None,
                "last_year": years[-1] if years else None,
                "sample": {"date": filled[-1]["date"], "value": filled[-1]["value"]}
                if filled else None}
        out[code] = info
        print(f"  {code:<20} 行={len(rows):<4} 非空={len(filled):<4} "
              f"范围={info['first_year']}..{info['last_year']}  末值={info['sample']}")
    _dump("diag_wb.json", out)
    return {"n_keys": len(out)}


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

STAGES = {
    "nbs": stage_nbs,
    "nbs_search": stage_nbs_search,
    "nbs_targets": stage_nbs_targets,
    "wb": stage_wb,
    "wb_search": stage_wb_search,
    "imf": stage_imf,
    "bis": stage_bis,
    "fred": stage_fred,
    "verify": stage_verify,
    "diag_nbs": stage_diag_nbs,
    "diag_range": stage_diag_range,
    "diag_wb": stage_diag_wb,
}


def main(argv: list[str]) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    which = (argv[0] if argv else "all").strip().lower()
    names = list(STAGES) if which == "all" else [which]
    out: dict[str, Any] = {}
    for name in names:
        fn = STAGES.get(name)
        if fn is None:
            print(f"未知阶段 {name!r}；可用：{', '.join(STAGES)} | all")
            return 2
        _log(f"\n########## stage {name} ##########")
        out[name] = fn()
    _log("\n" + json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
