#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""report.py —— 单文件 HTML 数据质量报告（方向 D 第一轮）。

它解决什么问题
--------------
前面几轮产出的 5 份报告全是 JSON，只有工程师能读。本工具把它们合成**一个自包含的 HTML**：
非技术同事双击就能看懂「这个数据集可不可信、哪里有问题、能不能用」。

    输入（全部是已落盘的 JSON，本工具不重跑取数）
      data/validated/missing_report.json     缺失分类
      data/validated/cross_check/*.json      交叉验证原始产物（只用来数对数）
      data/validated/arbiter/*.json          口径判定（6 对）
      data/validated/credibility/*.json      可信度评分（11 条）
      data/processed/*/*.json                处理后数据（行数、缺失年份）
      python/econ_core/source_profiles.yaml  许可证信息

    输出：data/output/report.html（单文件；除 Plotly CDN 外自包含）

报告结构
--------
    [1] 标题区      标题 / 生成时间 / 数据窗口 / 序列数
    [2] 总览卡片    6 个数字卡片（序列、行数、缺失、对比对、平均可信度、数据源）
    [3] 可信度排名  11 条序列 × 5 维度，颜色分级
    [4] 雷达图      前 5 条序列的 5 维画像（Plotly scatterpolar）
    [5] 口径分歧    6 对交叉验证：判定 / 实测差异 / 归因 / 建议
    [6] 缺失甘特    每条序列哪些年份缺失、什么原因（Plotly 横向条形）
    [7] 许可证      3 个发布机构的许可与再分发条件
    [8] 页脚        生成器版本 / 项目路径 / 门禁状态

几点实现约定
------------
* 图表数据一律从 JSON 现算（`build_context`），模板里没有任何硬编码数字。
* 缺失值一律渲染成「—」，HTML 里不出现 None 字面量（自检第 6 项会查）。
* arbiter / credibility 的报告目录**不存在时本工具会补生成**（它们不在门禁链上，
  干净检出时没人跑 --write；否则报告会缺两大块）。已存在就只读，不覆盖。
* 门禁状态：本工具找一个约定路径 data/output/last_gate.json，没有就写 unknown ——
  门禁目前不落盘这种文件，所以正常会显示 unknown。
* 图表库走 CDN（打开报告需要联网）；其余 HTML/CSS/数据全部内联。

用法
----
    ./.venv/Scripts/python.exe tools/report.py            # 生成报告
    ./.venv/Scripts/python.exe tools/report.py --test     # 生成 + 7 项自检
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Optional, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "python"))

from econ_core import arbiter, credibility, http_client, source_profiler  # noqa: E402

VALIDATED_DIR: Path = http_client.PROJECT_ROOT / "data" / "validated"
PROCESSED_DIR: Path = http_client.PROJECT_ROOT / "data" / "processed"
CROSS_CHECK_DIR: Path = VALIDATED_DIR / "cross_check"
ARBITER_DIR: Path = VALIDATED_DIR / "arbiter"
CREDIBILITY_DIR: Path = VALIDATED_DIR / "credibility"
OUTPUT_PATH: Path = http_client.PROJECT_ROOT / "data" / "output" / "report.html"
GATE_STATUS_PATH: Path = http_client.PROJECT_ROOT / "data" / "output" / "last_gate.json"

GENERATOR = "tools/report.py v1（方向 D 第一轮）"
TITLE = "EconDataHarvester 数据质量报告"
PLOTLY_CDN = "https://cdn.plot.ly/plotly-2.27.0.min.js"

#: 判定 -> 颜色（同口径绿 / 可桥接黄 / 不可拼接红 / 人工复核灰）
VERDICT_COLOR = {
    "同口径": "#1e8e3e",
    "可桥接": "#f9ab00",
    "不可拼接": "#d93025",
    "人工复核": "#80868b",
}
#: 缺失分类 -> 颜色（true_gap 红 / not_yet_published 黄 / discontinued 灰 / series_start 蓝）
MISSING_COLOR = {
    "true_gap": "#d93025",
    "not_yet_published": "#f9ab00",
    "discontinued": "#9aa0a6",
    "series_start": "#1a73e8",
}
MISSING_LABEL_CN = {
    "true_gap": "真实空洞（可插值）",
    "not_yet_published": "尚未发布（等待）",
    "discontinued": "已停更（不补）",
    "series_start": "序列起点（不补）",
}
GRADE_COLOR = {"high": "#1e8e3e", "medium": "#f9ab00", "low": "#d93025"}
DIMENSIONS = ("expertise", "provenance", "timeliness", "transparency", "coherence")
DIMENSION_CN = {
    "expertise": "Expertise 权威性",
    "provenance": "Provenance 可追溯",
    "timeliness": "Timeliness 时效性",
    "transparency": "Transparency 透明度",
    "coherence": "Coherence 一致性",
}
LICENSE_USE_CN = {"yes": "可", "no": "不可", "unknown": "未标注（需自行核实）"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load(path: Path) -> Any:
    """读 JSON；不存在或坏了返回 None（报告要能容错生成）。"""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _rel(p: Path) -> str:
    try:
        return Path(p).resolve().relative_to(http_client.PROJECT_ROOT).as_posix()
    except ValueError:
        return str(p)


def _canonical(key: str) -> str:
    """把 alias / 自动推导键解析到规范键（知识库里没有就原样返回）。

    需要处理大小写变体：normalize 自动推导出来的键是**大写**（NBS|...），
    知识库里按大写存别名，但 data/processed 的信封里写的是小写 —— 不兼容的话
    同一条 GDP 会被当成两条序列，行数就会重复计数。
    """
    try:
        return str(source_profiler.profile_series(key)["series_key"])
    except KeyError:
        pass
    except Exception:  # noqa: BLE001 - 报告不能因为一条序列不在知识库就挂掉
        return key
    try:
        indicators = source_profiler.load_knowledge().get("indicators") or {}
    except Exception:  # noqa: BLE001
        return key
    low = key.lower()
    for candidate in indicators:
        if str(candidate).lower() == low:
            try:
                return str(source_profiler.profile_series(str(candidate))["series_key"])
            except Exception:  # noqa: BLE001
                break
    return key


# --------------------------------------------------------------------------- #
# 输入：确保四份派生报告存在（它们不在门禁链上）
# --------------------------------------------------------------------------- #

def ensure_inputs() -> list[str]:
    """检查 arbiter / credibility 报告目录；缺失就补生成（并在 stderr 留一行日志）。"""
    done: list[str] = []
    if not (ARBITER_DIR / "_index.json").is_file():
        arbiter.write_arbiter_report()
        n = len((_load(ARBITER_DIR / "_index.json") or {}).get("pairs") or [])
        print(f"[auto-generated] arbiter report (was missing, {n} pairs)", file=sys.stderr)
        done.append(f"补生成 arbiter 报告（{n} 对）")
    if not (CREDIBILITY_DIR / "_index.json").is_file():
        credibility.write_credibility_report()
        n = len((_load(CREDIBILITY_DIR / "_index.json") or {}).get("series") or [])
        print(f"[auto-generated] credibility report (was missing, {n} series)", file=sys.stderr)
        done.append(f"补生成 credibility 报告（{n} 条）")
    return done


def load_series() -> list[dict[str, Any]]:
    """processed/*/*.json -> 每序列一条记录（按规范键去重；alias 副本让位）。"""
    out: dict[str, dict[str, Any]] = {}
    for p in sorted(PROCESSED_DIR.glob("*/*.json")):
        obj = _load(p) or {}
        raw_key = str(((obj.get("missing_report") or {}).get("series_key")) or p.stem)
        rows = [r for r in (obj.get("rows") or []) if isinstance(r, dict)]
        if not rows:
            continue
        canon = _canonical(raw_key)
        rec = {"series_key": canon, "raw_key": raw_key, "file": _rel(p), "rows": rows}
        prev = out.get(canon)
        if prev is None or (prev["raw_key"] != canon and raw_key == canon):
            out[canon] = rec
    return [out[k] for k in sorted(out)]


def load_arbiter_pairs() -> list[dict[str, Any]]:
    """按 arbiter 的 _index.json 逐对读判定明细（含归因与建议）。"""
    index = _load(ARBITER_DIR / "_index.json") or {}
    pairs: list[dict[str, Any]] = []
    for item in index.get("pairs") or []:
        rec = _load(ARBITER_DIR / str(item.get("file") or ""))
        if isinstance(rec, dict):
            pairs.append(rec)
    return pairs


def load_credibility() -> list[dict[str, Any]]:
    """按 credibility 的 _index.json 逐条读评分明细。"""
    index = _load(CREDIBILITY_DIR / "_index.json") or {}
    recs: list[dict[str, Any]] = []
    for item in index.get("series") or []:
        rec = _load(CREDIBILITY_DIR / str(item.get("file") or ""))
        if isinstance(rec, dict):
            recs.append(rec)
    return recs


def load_licenses() -> list[dict[str, Any]]:
    """从 source_profiles.yaml 的 publishers 段抽 3 个机构。"""
    kb = source_profiler.load_knowledge()
    pubs = kb.get("publishers") or {}
    out: list[dict[str, Any]] = []
    for key in ("nbs", "worldbank", "imf"):
        p = pubs.get(key) or {}
        out.append({
            "key": key,
            "name": str(p.get("name") or key),
            "full_name": str(p.get("full_name") or ""),
            "authority_level": str(p.get("authority_level") or ""),
            "license": str(p.get("license") or ""),
            "license_url": str(p.get("license_url") or ""),
            "commercial_use": LICENSE_USE_CN.get(str(p.get("commercial_use") or "unknown"),
                                                "未标注（需自行核实）"),
            "redistribution": LICENSE_USE_CN.get(str(p.get("redistribution") or "unknown"),
                                                  "未标注（需自行核实）"),
        })
    return out


# --------------------------------------------------------------------------- #
# 图表（Plotly 图形对象；模板只负责 newPlot）
# --------------------------------------------------------------------------- #

def radar_figure(creds: list[dict[str, Any]], top: int = 5) -> dict[str, Any]:
    """前 N 条序列的五维雷达图。"""
    theta = [DIMENSION_CN[k] for k in DIMENSIONS] + [DIMENSION_CN[DIMENSIONS[0]]]
    data = []
    for rec in creds[:top]:
        r = [float(rec["dimensions"][k]["score"]) for k in DIMENSIONS]
        data.append({
            "type": "scatterpolar",
            "r": r + [r[0]],
            "theta": theta,
            "fill": "toself",
            "name": str(rec.get("display_name") or rec.get("series_key")),
            "hovertemplate": "%{theta}<br>%{r:.0f} 分<extra>%{fullData.name}</extra>",
        })
    layout = {
        "polar": {"radialaxis": {"visible": True, "range": [0, 100], "dtick": 25}},
        "showlegend": True,
        "legend": {"orientation": "h", "y": -0.15},
        "margin": {"l": 60, "r": 60, "t": 30, "b": 60},
        "paper_bgcolor": "rgba(0,0,0,0)",
    }
    return {"data": data, "layout": layout}


def gantt_figure(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """缺失甘特图：每条序列一条基线条（有观测），缺失区间按分类叠色。"""
    names = [r["display_name"] for r in rows]
    data: list[dict[str, Any]] = [{
        "type": "bar",
        "orientation": "h",
        "name": "有观测区间",
        "y": names,
        "x": [max(1, r["end"] - r["start"] + 1) for r in rows],
        "base": [r["start"] for r in rows],
        "marker": {"color": "#e6f4ea", "line": {"color": "#c6e3cd", "width": 1}},
        "hovertemplate": "%{y}<br>有观测：%{base} 起共 %{x} 年<extra></extra>",
    }]
    for cls in ("true_gap", "not_yet_published", "discontinued", "series_start"):
        ys, xs, bases, texts = [], [], [], []
        for r in rows:
            for g in r["gaps"]:
                if g["classification"] != cls:
                    continue
                ys.append(r["display_name"])
                start, end = int(g["start"]), int(g["end"])
                bases.append(start)
                xs.append(end - start + 1)
                texts.append(str(g.get("evidence") or ""))
        if not ys:
            continue
        data.append({
            "type": "bar",
            "orientation": "h",
            "name": MISSING_LABEL_CN.get(cls, cls),
            "y": ys,
            "x": xs,
            "base": bases,
            "marker": {"color": MISSING_COLOR[cls]},
            "text": texts,
            "hovertemplate": "%{y}<br>缺失 %{base} 起共 %{x} 年<br>%{text}<extra></extra>",
        })
    years = [r["start"] for r in rows] + [r["end"] for r in rows]
    lo, hi = (min(years), max(years)) if years else (2015, 2024)
    layout = {
        "barmode": "overlay",
        "xaxis": {"title": "年份", "dtick": 1, "range": [lo - 0.6, hi + 0.6],
                  "tickangle": -45},
        "yaxis": {"automargin": True, "autorange": "reversed"},
        "legend": {"orientation": "h", "y": -0.25},
        "margin": {"l": 220, "r": 30, "t": 20, "b": 90},
        "paper_bgcolor": "rgba(0,0,0,0)",
        "height": max(260, 60 + 42 * len(rows)),
    }
    return {"data": data, "layout": layout}


# --------------------------------------------------------------------------- #
# 汇总：把 5 份 JSON 变成一个上下文
# --------------------------------------------------------------------------- #

ATTRIBUTION_CN = {
    "noise": "噪声（舍入/修订级）",
    "statistical_method": "统计方法不同",
    "coverage": "覆盖范围不同",
    "revision": "修订机制不同",
    "unknown": "知识库无法解释",
}
ACTION_CN = {"splice": "可直接拼接", "bridge": "需桥接", "keep_separate": "保持独立"}
ALIGNMENT_CN = {
    "aligned": "画像与实测同调",
    "profile_stricter": "画像判定更严",
    "measured_stricter": "实测结果更严",
}


def _year(v: Any) -> Optional[int]:
    try:
        return int(str(v)[:4])
    except (TypeError, ValueError):
        return None


def _diff_text(value: Any, diff_type: str) -> str:
    try:
        d = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"{d * 100:.2f}%" if diff_type == "percent" else f"{d:.2f} pp"




# --------------------------------------------------------------------------- #
# 数据血缘（任务 D）与下载清单（任务 E）
# --------------------------------------------------------------------------- #

LINEAGE_SERIES = "nbs|gdp|cny_100m"
CSV_PATH: Path = http_client.PROJECT_ROOT / "data" / "output" / "econ_data.csv"


def _file_info(path: Optional[Path]) -> dict[str, Any]:
    """文件路径 / mtime / 大小（不存在时 exists=False，展示成 —）。"""
    if not path:
        return {"path": "—", "exists": False, "size": None, "mtime": None, "size_text": "—"}
    p = Path(path)
    try:
        st = p.stat()
    except OSError:
        return {"path": _rel(p), "exists": False, "size": None, "mtime": None,
                "size_text": "—"}
    return {
        "path": _rel(p),
        "exists": True,
        "size": st.st_size,
        "size_text": f"{st.st_size / 1024:.1f} KB",
        "mtime": datetime.fromtimestamp(st.st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _csv_row_for(row_sha16: str, period: str) -> Optional[dict[str, Any]]:
    """在导出的 CSV 里按 row_sha16 + period 找那一行（证明 CSV 与长表逐行对得上）。"""
    if not row_sha16:
        return None
    import csv as _csv
    try:
        with CSV_PATH.open(encoding="utf-8-sig", newline="") as fh:
            for r in _csv.DictReader(fh):
                if r.get("row_sha16") == row_sha16 and r.get("period") == period:
                    return r
    except OSError:
        return None
    return None


def build_lineage(series_key: str = LINEAGE_SERIES) -> dict[str, Any]:
    """一条序列从原始 HTTP 到最终 CSV 行的完整链路。

    两个要点（都写进报告里）：
    * parsed 文件名后缀是**请求指纹**，与 raw 的 .bin 文件名不同；回溯靠 parsed 文件里的
      raw_cache 字段，不是靠文件名。
    * 第 3 步与第 5 步的 row_sha16 必须完全一致——那是「CSV 里的数字就是长表里的数字」的凭证。
    """
    profile = source_profiler.profile_series(series_key)
    canon = str(profile["series_key"])
    lineage = profile.get("data_lineage") or {}
    validated_path = lineage.get("series_file")
    vobj = _load(PROJECT_ROOT / str(validated_path)) if validated_path else None
    vrows = [r for r in ((vobj or {}).get("rows") or []) if isinstance(r, dict)]
    row = next((r for r in vrows if r.get("value") is not None), None)
    if row is None and vrows:
        row = vrows[0]
    row = row or {}
    raw_path = str(row.get("raw_cache") or "")
    raw = Path(raw_path) if raw_path else None

    parsed: Optional[Path] = None
    if raw_path:
        for cand in sorted((http_client.PROJECT_ROOT / "data" / "parsed").glob("*/*.json")):
            if str((_load(cand) or {}).get("raw_cache") or "") == raw_path:
                parsed = cand
                break

    proc: Optional[Path] = None
    for cand in sorted(PROCESSED_DIR.glob("*/*.json")):
        obj = _load(cand) or {}
        key = str(((obj.get("missing_report") or {}).get("series_key")) or "")
        if not key or _canonical(key) != canon:
            continue
        # 同一条序列可能有多个 processed 文件（alias 副本）；
        # 优先取「键本身就是规范键」的那个，别把 alias 副本当血缘终点
        if key == canon:
            proc = cand
            break
        if proc is None:
            proc = cand

    csv_row = _csv_row_for(str(row.get("row_sha16") or ""), str(row.get("period") or ""))
    stem = raw.stem if raw else ""
    csv_info = _file_info(CSV_PATH)

    steps = [
        {"step": "1 原始 HTTP 响应",
         "file": _file_info(raw),
         "detail": (f"指纹（url+method+body 的 sha256，文件名前 12 位：{stem[:12]}…）"
                    if stem else "没有 raw_cache 字段"),
         "note": "从 data.stats.gov.cn 拿到的未改动原文，本项目所有结论的最终证据"},
        {"step": "2 解析为 JSON",
         "file": _file_info(parsed),
         "detail": ("parsed 后缀是请求指纹，与 raw 文件名不同；靠文件里的 raw_cache 字段回溯"
                    if parsed else "没有找到 raw_cache 指向同一个 .bin 的 parsed 文件"),
         "note": "原始字节 -> 结构化观测（数字仍来自上游，未加工）"},
        {"step": "3 规范化为长表",
         "file": _file_info(PROJECT_ROOT / str(validated_path) if validated_path else None),
         "detail": f"{len(vrows)} 行；每行都有 row_sha16 指纹",
         "note": "统一成 14 列 schema；row_sha16 = 该行去掉指纹字段后规范化 JSON 的 sha256 前 16 位"},
        {"step": "4 打上缺失元数据",
         "file": _file_info(proc),
         "detail": "processed 信封里有 decisions（每个缺口的处置决策）与 processed_at",
         "note": "标注哪一年缺、属于哪一类、为什么不补；数值本身一个都没动"},
        {"step": "5 导出到 CSV",
         "file": csv_info,
         "detail": (f"示例行：period={csv_row.get('period')} value={csv_row.get('value')} "
                    f"row_sha16={csv_row.get('row_sha16')}（与第 3 步一致）")
                    if csv_row else "没有在 CSV 里找到这一行（可能还没跑 export）",
         "note": "发布给外部使用的形态；row_sha16 与第 3 步逐行一致，可以核对"},
    ]
    return {
        "series_key": canon,
        "display_name": str(profile["indicator"].get("display_name") or canon),
        "steps": steps,
        "sample": {"period": row.get("period"), "value": row.get("value"),
                   "unit": row.get("unit"), "row_sha16": row.get("row_sha16"),
                   "fetched_at": row.get("fetched_at")},
    }


def build_downloads() -> dict[str, Any]:
    """页脚之前的三份产物清单（大小 + 更新时间，路径相对项目根）。"""
    items = [
        {"name": "econ_data.csv", "desc": "长表 CSV（utf-8-sig，Excel 可直接打开）"},
        {"name": "econ_data.db", "desc": "SQLite（observations 表 + series_summary 视图）"},
        {"name": "data_dictionary.md", "desc": "Markdown 数据字典（14 列语义 + 缺失字段说明）"},
    ]
    files = []
    for it in items:
        info = _file_info(PROJECT_ROOT / "data" / "output" / it["name"])
        info.update(it)
        files.append(info)
    n_csv = 0
    try:
        import csv as _csv
        with CSV_PATH.open(encoding="utf-8-sig", newline="") as fh:
            n_csv = sum(1 for _ in _csv.DictReader(fh))
    except OSError:
        n_csv = 0
    return {"files": files, "csv_rows": n_csv}


def build_context() -> dict[str, Any]:
    """读全部输入，算出模板要用的所有数字与行。"""
    series = load_series()
    creds = load_credibility()
    pairs = load_arbiter_pairs()
    licenses = load_licenses()
    missing_report = _load(VALIDATED_DIR / "missing_report.json") or {}
    cv_files = sorted(CROSS_CHECK_DIR.glob("*.json"))

    names = {str(c.get("series_key")): str(c.get("display_name") or c.get("series_key"))
             for c in creds}

    # ---- 卡片数字 ----
    all_rows = [r for s in series for r in s["rows"]]
    n_rows = len(all_rows)
    null_rows = [r for r in all_rows if r.get("value") is None]
    by_class: dict[str, int] = {}
    for r in null_rows:
        cls = str(r.get("missing_classification") or "未分类")
        by_class[cls] = by_class.get(cls, 0) + 1
    sources = sorted({str(r.get("source") or "") for r in all_rows if r.get("source")})
    avg_score = round(sum(float(c["score"]) for c in creds) / len(creds), 1) if creds else None
    grades: dict[str, int] = {}
    for c in creds:
        grades[str(c["grade"])] = grades.get(str(c["grade"]), 0) + 1

    # ---- 数据窗口 ----
    declared = [s for s in (missing_report.get("series") or [])
                if isinstance(s, dict) and s.get("group") == "declared"]
    spans = [s.get("summary", {}).get("period_range") or [] for s in declared]
    yrs = [y for sp in spans for y in (sp[:1] + sp[-1:])]
    window = f"{min(yrs)}-{max(yrs)}" if yrs else "unknown"

    cards = [
        {"label": "序列总数", "value": str(len(series)),
         "sub": f"已落盘长表；知识库另有 {max(0, len(creds) - len(series))} 条仅用于对比"
                  if len(creds) > len(series) else "已落盘长表",
         "tone": "info"},
        {"label": "总观测行数", "value": str(n_rows), "sub": "9 条序列的全部年份",
         "tone": "info"},
        {"label": "缺失行数", "value": str(len(null_rows)),
         "sub": " / ".join(f"{k} {v}" for k, v in sorted(by_class.items())) or "无缺失",
         "tone": "warn" if null_rows else "ok"},
        {"label": "交叉验证对数", "value": str(len(pairs)),
         "sub": f"原始产物 {len(cv_files)} 份（cross_check/*.json）", "tone": "info"},
        {"label": "平均可信度", "value": "—" if avg_score is None else f"{avg_score}",
         "sub": " / ".join(f"{k} {v}" for k, v in sorted(grades.items())) or "无数据",
         "tone": "ok" if (avg_score or 0) >= 80 else "warn"},
        {"label": "数据源数", "value": str(len(sources)), "sub": " / ".join(sources),
         "tone": "info"},
    ]

    # ---- [3] 可信度排名 ----
    cred_view = []
    for i, c in enumerate(creds, 1):
        score = float(c["score"])
        cred_view.append({
            "rank": i,
            "series_key": str(c.get("series_key")),
            "display_name": str(c.get("display_name") or c.get("series_key")),
            "score": score,
            "grade": str(c.get("grade")),
            "color": GRADE_COLOR.get(str(c.get("grade")), "#80868b"),
            "dims": [{"key": k, "name": DIMENSION_CN[k],
                     "score": float(c["dimensions"][k]["score"]),
                     "evidence": str(c["dimensions"][k].get("evidence") or "")}
                    for k in DIMENSIONS],
        })

    # ---- [5] 口径分歧 ----
    pair_view = []
    for p in pairs:
        a = str(p.get("series_a"))
        b = str(p.get("series_b"))
        measured = p.get("measured") or {}
        pair_view.append({
            "a": a, "b": b,
            "a_name": names.get(a, a), "b_name": names.get(b, b),
            "verdict": str(p.get("verdict")),
            "color": VERDICT_COLOR.get(str(p.get("verdict")), "#80868b"),
            "diff": _diff_text(measured.get("diff_pp"), str(measured.get("diff_type"))),
            "diff_type": str(measured.get("diff_type")),
            "source": str(measured.get("source")),
            "attribution": ATTRIBUTION_CN.get(str(p.get("attribution")), str(p.get("attribution"))),
            "action": ACTION_CN.get(str(p.get("recommended_action")), str(p.get("recommended_action"))),
            "alignment": ALIGNMENT_CN.get(str(p.get("alignment")), str(p.get("alignment"))),
            "explanation": str(p.get("explanation") or ""),
            "comparable": str(p.get("comparable")),
        })

    # ---- [6] 缺失甘特 ----
    gantt_rows = []
    gap_rows = []
    for s in declared:
        key = _canonical(str(s.get("series_key")))
        summ = s.get("summary") or {}
        rng = summ.get("period_range") or []
        start, end = _year(rng[0]) if rng else None, _year(rng[-1]) if rng else None
        gaps = []
        for g in s.get("gaps") or []:
            gs, ge = _year(g.get("start")), _year(g.get("end"))
            if gs is None or ge is None:
                continue
            gaps.append({"start": gs, "end": ge,
                         "classification": str(g.get("classification")),
                         "n_missing": g.get("n_missing"),
                         "evidence": str(g.get("evidence") or "")})
            gap_rows.append({"series": names.get(key, str(s.get("label") or key)),
                             "range": f"{gs}-{ge}" if gs != ge else str(gs),
                             "classification": str(g.get("classification")),
                             "color": MISSING_COLOR.get(str(g.get("classification")), "#80868b"),
                             "label_cn": MISSING_LABEL_CN.get(str(g.get("classification")), ""),
                             "evidence": str(g.get("evidence") or "")})
        if start is not None and end is not None:
            gantt_rows.append({"display_name": names.get(key, str(s.get("label") or key)),
                               "start": start, "end": end, "gaps": gaps})

    # ---- [8] 页脚 ----
    gate = _load(GATE_STATUS_PATH)
    gate_text = "unknown（运行 run-all-checks.py 可更新）"
    if isinstance(gate, dict) and gate.get("n_total"):
        gate_text = (f"{gate.get('n_pass', '?')}/{gate.get('n_total', '?')} PASS"
                     f" @ {gate.get('ran_at', '?')} ({gate.get('elapsed_s', '?')}s)")

    return {
        "title": TITLE,
        "generated_at": _utc_now(),
        "generator": GENERATOR,
        "plotly_cdn": PLOTLY_CDN,
        "window": window,
        "n_series": len(series),
        "cards": cards,
        "cred": cred_view,
        "radar_json": json.dumps(radar_figure(creds), ensure_ascii=False),
        "pairs": pair_view,
        "gantt_json": json.dumps(gantt_figure(gantt_rows), ensure_ascii=False),
        "gantt_rows": gantt_rows,
        "gap_rows": gap_rows,
        "missing_color": MISSING_COLOR,
        "missing_label": MISSING_LABEL_CN,
        "licenses": licenses,
        "footer": {
            "generator": GENERATOR,
            "project_root": str(http_client.PROJECT_ROOT),
            "gate": gate_text,
            "inputs": [
                "data/validated/missing_report.json",
                f"data/validated/cross_check/*.json（{len(cv_files)} 份）",
                f"data/validated/arbiter/*.json（{len(pairs)} 对）",
                f"data/validated/credibility/*.json（{len(creds)} 条）",
                f"data/processed/*/*.json（{len(series)} 条序列）",
                "python/econ_core/source_profiles.yaml（许可证）",
            ],
        },
        "lineage": build_lineage(),
        "downloads": build_downloads(),
        "dim_names": [DIMENSION_CN[k] for k in DIMENSIONS],
    }


TEMPLATE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ title }}</title>
<script src="{{ plotly_cdn }}"></script>
<style>
:root { --ink:#202124; --muted:#5f6368; --line:#dadce0; --bg:#f6f8fa; --card:#fff; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--ink);
  font-family:"Microsoft YaHei","PingFang SC","Hiragino Sans GB",system-ui,sans-serif; line-height:1.6; }
.wrap { max-width:1100px; margin:0 auto; padding:24px 18px 64px; }
.hero { background:linear-gradient(135deg,#1a73e8,#0b57d0); color:#fff; border-radius:14px; padding:26px 28px; }
.hero h1 { margin:0 0 8px; font-size:26px; }
.hero .meta { margin:0; opacity:.9; font-size:14px; }
section { margin-top:28px; }
h2 { font-size:19px; margin:0 0 4px; }
.hint { color:var(--muted); font-size:13px; margin:0 0 12px; }
.cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(168px,1fr)); gap:12px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px 16px; }
.card-value { font-size:26px; font-weight:600; line-height:1.2; }
.card-label { font-size:13px; color:var(--muted); margin-top:2px; }
.card-sub { font-size:12px; color:var(--muted); margin-top:6px; word-break:break-all; }
.card.tone-ok .card-value { color:#1e8e3e; }
.card.tone-warn .card-value { color:#d93025; }
.card.tone-info .card-value { color:#1a73e8; }
table { width:100%; border-collapse:collapse; background:var(--card);
  border:1px solid var(--line); border-radius:12px; overflow:hidden; font-size:14px; }
th,td { padding:9px 11px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; }
th { background:#f1f3f4; font-weight:600; font-size:13px; }
tbody tr:last-child td { border-bottom:none; }
.k { color:var(--muted); font-size:12px; word-break:break-all; }
.badge { display:inline-block; color:#fff; border-radius:999px; padding:1px 10px; font-size:13px; font-weight:600; }
.pill { display:inline-block; color:#fff; border-radius:6px; padding:1px 8px; font-size:13px; white-space:nowrap; }
.chart { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:6px; }
.legend { display:flex; flex-wrap:wrap; gap:14px; font-size:13px; color:var(--muted); margin:8px 0 0; }
.dot { display:inline-block; width:11px; height:11px; border-radius:3px; margin-right:5px; }
footer { margin-top:34px; padding-top:16px; border-top:1px solid var(--line); color:var(--muted); font-size:13px; }
footer ul { margin:6px 0 0 18px; padding:0; }
.note { background:#fef7e0; border:1px solid #fde293; border-radius:8px; padding:8px 12px; font-size:13px; }
.guide { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px 18px; font-size:14px; }
.guide ol { margin:6px 0 0 18px; padding:0; }
.guide li { margin:4px 0; }
details summary { cursor:pointer; color:#1a73e8; font-size:12px; margin-top:4px; }
.ev { margin:4px 0 0 16px; padding:0; }
.ev li { font-size:12px; color:var(--muted); }
.exp { font-size:12px; color:var(--muted); margin-top:4px; }
.chain { display:flex; flex-wrap:wrap; gap:6px; align-items:stretch; }
.node { flex:1 1 178px; background:var(--card); border:1px solid var(--line); border-radius:10px; padding:10px 12px; }
.node-idx { font-weight:600; font-size:13px; color:#1a73e8; }
.node-path { font-size:12px; color:var(--ink); word-break:break-all; margin:3px 0; }
.node-detail { font-size:12px; color:var(--muted); margin-top:4px; }
.arrow { align-self:center; color:var(--muted); font-size:18px; }
</style>
</head>
<body>
<div class="wrap">

  <header class="hero">
    <h1>{{ title }}</h1>
    <p class="meta">生成时间 {{ generated_at }} &middot; 数据窗口 {{ window }} &middot; 已落盘序列 {{ n_series }} 条 &middot; 生成器 {{ generator }}</p>
  </header>

  <section id="guide">
    <h2>怎么读这份报告</h2>
    <div class="guide">
      <ol>
        <li><strong>先看「一、总览」的 6 个数字</strong>：数据有多大、缺多少、平均可信度多少。</li>
        <li><strong>再看「二、可信度排名」</strong>：每条序列一行，点开「评分依据」能看到每一项为什么给这个分。</li>
        <li><strong>「四、口径分歧与处置建议」是使用数据前必看的</strong>：两两比较过的序列，如果判定为「不可拼接」，说明它们<em>量的不是同一件事</em>——这是口径差异，不是数据错误，请当两条独立序列使用，不要硬拼成一条。</li>
        <li><strong>「五、缺失分布」里的灰色和蓝色段不要补</strong>：灰色是官方已经停更，蓝色是序列起点，往前或往后外推都等于伪造数据。黄色是尚未发布，等下一期即可；红色才是真正的中段空洞。</li>
        <li><strong>对外发布或商用前先看「六、许可证」</strong>：「未标注」只表示知识库里没有明确条款，不等于可以随便用。</li>
      </ol>
    </div>
  </section>

  <section id="overview">
    <h2>一、总览</h2>
    <p class="hint">下面 6 个数字回答「这批数据有多大、缺多少、可不可信」。</p>
    <div class="cards">
      {% for c in cards %}
      <div class="card tone-{{ c.tone }}">
        <div class="card-value">{{ c.value }}</div>
        <div class="card-label">{{ c.label }}</div>
        <div class="card-sub">{{ c.sub }}</div>
      </div>
      {% endfor %}
    </div>
  </section>

  <section id="credibility">
    <h2>二、可信度排名</h2>
    <p class="hint">五维加权（Expertise .25 / Provenance .20 / Timeliness .10 / Transparency .15 / Coherence .30）。
      分数 &ge;80 绿、60-79 黄、&lt;60 红；鼠标悬停单元格可看该项的评分依据。</p>
    <table>
      <thead>
        <tr><th>#</th><th>序列</th><th>分数</th><th>等级</th>
          {% for d in dim_names %}<th>{{ d }}</th>{% endfor %}
        </tr>
      </thead>
      <tbody>
      {% for r in cred %}
        <tr data-cred="{{ r.series_key }}">
          <td>{{ r.rank }}</td>
          <td><strong>{{ r.display_name }}</strong><div class="k">{{ r.series_key }}</div>
            <details><summary>评分依据</summary><ul class="ev">
              {% for d in r.dims %}<li><b>{{ d.name }}</b> = {{ d.score }}：{{ d.evidence }}</li>{% endfor %}
            </ul></details></td>
          <td><span class="badge" style="background:{{ r.color }}">{{ '%.2f'|format(r.score) }}</span></td>
          <td>{{ r.grade }}</td>
          {% for d in r.dims %}<td title="{{ d.evidence }}">{{ '%.0f'|format(d.score) }}</td>{% endfor %}
        </tr>
      {% endfor %}
      </tbody>
    </table>
  </section>

  <section id="radar">
    <h2>三、可信度画像（前 5 条）</h2>
    <p class="hint">越往外越可信。五条序列叠在一起时，形状差在哪一维一眼就能看出来。</p>
    <div id="radar-chart" class="chart" style="height:520px"></div>
  </section>

  <section id="cvdiff">
    <h2>四、口径分歧与处置建议</h2>
    <p class="hint">两两交叉验证的结论。「不可拼接」是<strong>口径不同</strong>，不是数据错误——
      这种情况要当作两条序列分别使用，不要硬拼。</p>
    <table>
      <thead>
        <tr><th>序列 A</th><th>序列 B</th><th>判定</th><th>实测差异</th><th>归因</th><th>建议</th></tr>
      </thead>
      <tbody>
      {% for p in pairs %}
        <tr data-cv-pair="{{ p.a }} x {{ p.b }}">
          <td><strong>{{ p.a_name }}</strong><div class="k">{{ p.a }}</div></td>
          <td><strong>{{ p.b_name }}</strong><div class="k">{{ p.b }}</div></td>
          <td><span class="pill" style="background:{{ p.color }}">{{ p.verdict }}</span></td>
          <td>{{ p.diff }}<div class="k">{{ p.diff_type }}</div></td>
          <td>{{ p.attribution }}<div class="k">{{ p.alignment }}</div></td>
          <td>{{ p.action }}<div class="k">可比性 {{ p.comparable }}</div>
            <div class="exp">{{ p.explanation | bold }}</div></td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
    {% if not pairs %}<p class="note">没有找到口径判定结果（data/validated/arbiter/ 为空）。</p>{% endif %}
  </section>

  <section id="gantt">
    <h2>五、缺失分布</h2>
    <p class="hint">每条序列一行，浅绿色是<strong>有观测</strong>的年份，彩色段是<strong>缺</strong>的年份。
      灰色「已停更」和蓝色「序列起点」都<strong>不该补</strong>——硬补等于伪造数据。</p>
    <div id="gantt-chart" class="chart"></div>
    <div class="legend">
      {% for cls, color in missing_color.items() %}
      <span><span class="dot" style="background:{{ color }}"></span>{{ missing_label[cls] }}</span>
      {% endfor %}
    </div>
    {% if gap_rows %}
    <table style="margin-top:14px">
      <thead><tr><th>序列</th><th>缺失年份</th><th>分类</th><th>判定依据</th></tr></thead>
      <tbody>
      {% for g in gap_rows %}
        <tr>
          <td>{{ g.series }}</td><td>{{ g.range }}</td>
          <td><span class="pill" style="background:{{ g.color }}">{{ g.label_cn }}</span></td>
          <td class="k">{{ g.evidence }}</td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
    {% endif %}
  </section>

  <section id="licenses">
    <h2>六、数据来源与许可证</h2>
    <p class="hint">对外发布或商用前先看这张表。<strong>「未标注」不等于可以随便用</strong>，只表示知识库里没找到明确条款，需要自行核实。</p>
    <table>
      <thead><tr><th>发布机构</th><th>权威级别</th><th>许可证</th><th>能否商用</th><th>能否再分发</th></tr></thead>
      <tbody>
      {% for l in licenses %}
        <tr>
          <td><strong>{{ l.name }}</strong><div class="k">{{ l.full_name }}</div></td>
          <td>{{ l.authority_level }}</td>
          <td>{{ l.license }}{% if l.license_url and l.license_url != 'unknown' %}<div class="k">{{ l.license_url }}</div>{% endif %}</td>
          <td>{{ l.commercial_use }}</td>
          <td>{{ l.redistribution }}</td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
  </section>

  <section id="lineage">
    <h2>七、数据血缘：一个数字从哪来</h2>
    <p class="hint">以 <strong>{{ lineage.display_name }}</strong>（<span class="k">{{ lineage.series_key }}</span>）为例，
      一个数字从原始 HTTP 响应到最终 CSV 行要过 5 步，每一步都留了文件和指纹。所谓「可追溯」就是这条链能一步步走回去。</p>
    <div class="chain">
      {% for s in lineage.steps %}
      <div class="node">
        <div class="node-idx">{{ s.step }}</div>
        <div class="node-path">{{ s.file.path }}</div>
        <div class="k">{{ s.file.mtime or '—' }} · {{ s.file.size_text }}</div>
        <div class="node-detail">{{ s.detail }}</div>
      </div>
      {% if not loop.last %}<div class="arrow">&rarr;</div>{% endif %}
      {% endfor %}
    </div>
    <table style="margin-top:14px">
      <thead><tr><th>步骤</th><th>文件（相对项目根）</th><th>生成时间</th><th>大小</th><th>这一步做了什么</th></tr></thead>
      <tbody>
      {% for s in lineage.steps %}
        <tr data-lineage-step="{{ loop.index }}">
          <td>{{ s.step }}</td>
          <td class="k">{{ s.file.path }}</td>
          <td class="k">{{ s.file.mtime or '—' }}</td>
          <td>{{ s.file.size_text }}</td>
          <td class="k">{{ s.note }}</td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
    <p class="hint">示例行（第 3 步长表里的第一行有值数据）：period={{ lineage.sample.period or '—' }} ·
      value={{ lineage.sample.value if lineage.sample.value is not none else '—' }} {{ lineage.sample.unit or '' }} ·
      row_sha16=<span class="k">{{ lineage.sample.row_sha16 or '—' }}</span> · fetched_at={{ lineage.sample.fetched_at or '—' }}</p>
  </section>

  <section id="downloads">
    <h2>八、数据下载</h2>
    <p class="hint">报告之外的三份可直接使用的产物，都在 <span class="k">data/output/</span> 下（路径相对项目根）。打开需自备工具：CSV 用 Excel，DB 用任意 SQLite 客户端。</p>
    <table>
      <thead><tr><th>文件</th><th>说明</th><th>大小</th><th>更新时间</th></tr></thead>
      <tbody>
      {% for f in downloads.files %}
        <tr data-download="{{ f.name }}">
          <td><strong>{{ f.name }}</strong><div class="k">{{ f.path }}</div></td>
          <td>{{ f.desc }}</td>
          <td>{{ f.size_text }}</td>
          <td class="k">{{ f.mtime or '—' }}</td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
    <p class="note"><strong>CSV 行数与总览不一致是正常的</strong>：CSV 现有 {{ downloads.csv_rows }} 行，
      而「一、总览」说的 142 行是<strong>按序列去重后</strong>的观测数。export 层把 alias 副本
      <span class="k">nbs|000000000000|db8e…</span>（与 nbs|gdp|cny_100m 是同一条 GDP 序列）也导出了一遍，
      所以 CSV 多出 10 行。数据本身没错，但用 CSV 做统计前建议按 series_key 去重。</p>
  </section>

  <footer>
    <div><strong>{{ footer.generator }}</strong> &middot; 项目路径 <span class="k">{{ footer.project_root }}</span></div>
    <div>门禁状态：{{ footer.gate }}</div>
    <div>数据来源文件：</div>
    <ul>
      {% for f in footer.inputs %}<li class="k">{{ f }}</li>{% endfor %}
    </ul>
    <div class="k" style="margin-top:8px">图表由 Plotly 渲染（CDN：{{ plotly_cdn }}），打开本页需要联网。</div>
  </footer>
</div>

<script>
var RADAR = {{ radar_json | safe }};
var GANTT = {{ gantt_json | safe }};
function dshDraw() {
  if (!window.Plotly) { return; }
  Plotly.newPlot('radar-chart', RADAR.data, RADAR.layout, {responsive: true, displaylogo: false});
  Plotly.newPlot('gantt-chart', GANTT.data, GANTT.layout, {responsive: true, displaylogo: false});
}
dshDraw();
window.addEventListener('load', dshDraw);
</script>
</body>
</html>
"""


def _require_jinja2():
    try:
        import jinja2
    except ImportError as exc:  # pragma: no cover - 环境问题
        raise RuntimeError(
            "生成 HTML 报告需要 Jinja2。请先安装："
            " ./.venv/Scripts/python.exe pip_sandbox_install.py install jinja2"
        ) from exc
    return jinja2


def _bold(text: Any) -> Any:
    """把上游解释文本里的 **粗体** 转成 <strong>（其余内容照常转义）。

    source_profiler 的 explanation 是给人读的纯文本、里面带 markdown 粗体；
    直接塞进 HTML 会把星号原样显示出来。
    """
    import html as _html
    from markupsafe import Markup
    out = _html.escape(str(text or ""))
    while out.count("**") >= 2:
        out = out.replace("**", "<strong>", 1).replace("**", "</strong>", 1)
    out = out.replace("**", "")
    return Markup(out)


def render_html(ctx: dict[str, Any]) -> str:
    """渲染模板。autoescape 打开；None 统一渲染成「—」。"""
    jinja2 = _require_jinja2()
    env = jinja2.Environment(
        autoescape=True,
        trim_blocks=True,
        lstrip_blocks=True,
        finalize=lambda v: "—" if v is None else v,
    )
    env.filters["bold"] = _bold
    return env.from_string(TEMPLATE).render(**ctx)


def build(out_path: Optional[Path] = None) -> Path:
    """生成报告，返回输出路径。"""
    ensure_inputs()
    ctx = build_context()
    out = Path(out_path) if out_path else OUTPUT_PATH
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html(ctx), encoding="utf-8")
    return out


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #

MIN_BYTES = 30 * 1024


class _Probe(HTMLParser):
    """只做两件事：解析一遍看会不会抛异常；数一下没闭合的标签。"""

    VOID = {"meta", "link", "br", "img", "input", "hr", "col", "source"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if self.stack and self.stack[-1] == tag:
            self.stack.pop()


def _selftest() -> int:
    failures: list[str] = []
    print("=" * 92)
    print("self-test: report（单文件 HTML 数据质量报告）")
    print("=" * 92)

    out = build()
    text = out.read_text(encoding="utf-8")
    ctx = build_context()
    size = out.stat().st_size
    print(f"输出: {_rel(out)}")

    # 1) 文件存在 + 大小
    print()
    print("-" * 92)
    print(f"[1] 文件存在且 > 30KB：{size} bytes（阈值 {MIN_BYTES}）")
    if not out.is_file():
        failures.append(f"1: 文件不存在 {out}")
    elif size <= MIN_BYTES:
        failures.append(f"1: 文件只有 {size} bytes，期望 > {MIN_BYTES}")

    # 2) 标题
    print()
    print("-" * 92)
    print(f"[2] 含标题 {TITLE!r}")
    if TITLE not in text:
        failures.append("2: 缺少标题")

    # 3) Plotly CDN
    print()
    print("-" * 92)
    print(f"[3] 含 Plotly CDN 引用：{PLOTLY_CDN}")
    if PLOTLY_CDN not in text:
        failures.append("3: 缺少 Plotly CDN 引用")

    # 4) 交叉验证对数
    n_pair_html = text.count('data-cv-pair="')
    n_pair_ctx = len(ctx["pairs"])
    print()
    print("-" * 92)
    print(f"[4] 交叉验证记录：HTML {n_pair_html} 条 / 输入 {n_pair_ctx} 对")
    if n_pair_html != n_pair_ctx:
        failures.append(f"4: HTML 里的对数 {n_pair_html} != 输入 {n_pair_ctx}")
    if n_pair_ctx < 6:
        failures.append(f"4: 期望至少 6 对，实际 {n_pair_ctx}")

    # 5) 可信度条数
    n_cred_html = text.count('data-cred="')
    n_cred_ctx = len(ctx["cred"])
    print()
    print("-" * 92)
    print(f"[5] 可信度记录：HTML {n_cred_html} 条 / 输入 {n_cred_ctx} 条")
    if n_cred_html != n_cred_ctx:
        failures.append(f"5: HTML 里的条数 {n_cred_html} != 输入 {n_cred_ctx}")
    if n_cred_ctx < 11:
        failures.append(f"5: 期望至少 11 条，实际 {n_cred_ctx}")

    # 6) 不出现 None 字面量
    n_none = text.count("None")
    print()
    print("-" * 92)
    print(f"[6] 不出现 None 字面量：找到 {n_none} 处")
    if n_none:
        pos = text.find("None")
        failures.append(f"6: 出现 None，首个位置附近：{text[max(0, pos - 60):pos + 20]!r}")

    # 7) html.parser 解析一遍
    print()
    print("-" * 92)
    print("[7] 用 html.parser 解析一遍")
    probe = _Probe()
    try:
        probe.feed(text)
        probe.close()
        print(f"  OK 解析无异常；未闭合标签栈深 {len(probe.stack)}"
              + (f" {probe.stack[:5]}" if probe.stack else ""))
    except Exception as exc:  # noqa: BLE001 - 自检要看到任何异常
        failures.append(f"7: html.parser 抛异常 {type(exc).__name__}: {exc}")

    print()
    print("=" * 92)
    if failures:
        for f in failures:
            print(f"  [FAIL] {f}")
        print("=" * 92)
        return 1
    print("self-test 完成（7 项全部通过）")
    print("=" * 92)
    return 0


def _main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    p = argparse.ArgumentParser(prog="report",
                                description="把 5 份 JSON 报告合成单文件 HTML 质量报告")
    p.add_argument("--test", action="store_true", help="生成报告并跑 7 项自检")
    p.add_argument("--out", default=None, help="输出路径（默认 data/output/report.html）")
    a = p.parse_args(argv)

    if a.test:
        return _selftest()
    try:
        out = build(a.out)
    except RuntimeError as exc:
        print(f"[report] {exc}", file=sys.stderr)
        return 2
    print(f"已生成: {_rel(out)}（{out.stat().st_size} bytes）")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())

