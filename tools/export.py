#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""export.py —— 输出层：processed -> CSV（主）+ SQLite（查询）+ Markdown 数据字典。

输入
----
data/processed/**/*.json（由 tools/run-fill-strategy.py 产出），信封结构::

    {"rows": [...], "missing_report": {...}, "decisions": [...], "processed_at": "..."}

**按规范键去重**：同一条序列可能有多个 processed 文件——声明式规范键一份、
normalize 自动推导的 alias 键一份（例如 `nbs|gdp|cny_100m` 与
`NBS|000000000000|db8e...`，后者在知识库里带 alias_of）。本工具用
`econ_core.series_key.canonical_key()` 归一化后只保留**规范形式**的那一份，
alias 副本丢弃并打印出来；否则 CSV 会多出行、序列数会虚高。

输出
----
1. `data/output/econ_data.csv`     统一长表（所有序列所有行）
2. `data/output/econ_data.db`      SQLite：表 observations + 索引 + 视图 series_summary
3. `data/output/data_dictionary.md` 数据字典（逐序列画像 + 全局统计）

字段名的定案
------------
缺失元数据三列定为 `missing_classification` / `missing_action` / `missing_evidence`
（与 fill_strategy 写入时一致）。未来加 `fill_method` / `fill_from` 时直接追加列。

关于 value=None
---------------
**保留**，不丢行。缺失行的 `value` 在 CSV 里是空串、在 SQLite 里是 NULL，
原因写在 `missing_action`（leave_null / wait / manual_review）与 `missing_classification` 里。

实现约束
--------
纯标准库（csv / json / sqlite3 / pathlib），不用 pandas。

用法
----
    .\\.venv\\Scripts\\python.exe tools\\export.py
"""

from __future__ import annotations

import csv
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(PROJECT_ROOT / "python"))

from econ_core.series_key import canonical_key, is_alias_key  # noqa: E402

PROCESSED_DIR = PROJECT_ROOT / "data" / "processed"
OUT_DIR = PROJECT_ROOT / "data" / "output"

CSV_PATH = OUT_DIR / "econ_data.csv"
DB_PATH = OUT_DIR / "econ_data.db"
MD_PATH = OUT_DIR / "data_dictionary.md"

#: 统一长表的列顺序（定案，别随手改）
COLUMNS: tuple[str, ...] = (
    "source", "series_key", "region_code", "indicator_id", "indicator_name",
    "period", "period_type", "value", "unit",
    "missing_classification", "missing_action", "missing_evidence",
    "raw_cache", "row_sha16",
)

#: raw_fields 里可以回填上面某些列的候选键
_RAW_FIELD_KEYS: dict[str, tuple[str, ...]] = {
    "indicator_name": ("i_name",),
    "unit": ("du_name", "unit"),
    "indicator_id": ("i",),
    "region_code": ("code",),
}


def _envelope_key(obj: dict[str, Any]) -> str:
    """信封里记的 series_key（缺失时给 "?"）。"""
    return str((obj.get("missing_report") or {}).get("series_key") or "?")


def _load_processed() -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """读 data/processed/ 下所有 processed 信封，并按**规范键**去重。

    同一条序列可能有多个文件（规范键一份 + alias 键一份）。保留「键本身就是规范形式」
    的那份（即 is_alias_key 为 False），丢弃 alias 副本，并把丢弃的记进第二个返回值。
    归一化规则与 tools/report.py 共用 econ_core.series_key，不重复实现。
    """
    kept: dict[str, dict[str, Any]] = {}
    dropped: list[dict[str, str]] = []
    if not PROCESSED_DIR.is_dir():
        return [], []
    for f in sorted(PROCESSED_DIR.rglob("*.json")):
        try:
            obj = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[warn] 跳过 {f}: {exc}", file=sys.stderr)
            continue
        if not isinstance(obj, dict) or not isinstance(obj.get("rows"), list):
            continue
        rel = str(f.relative_to(PROJECT_ROOT))
        obj["_file"] = rel
        raw_key = _envelope_key(obj)
        canon = canonical_key(raw_key)
        prev = kept.get(canon)
        if prev is None:
            kept[canon] = obj
            continue
        if is_alias_key(_envelope_key(prev)) and not is_alias_key(raw_key):
            dropped.append({"file": str(prev.get("_file") or ""),
                            "series_key": _envelope_key(prev),
                            "kept": raw_key})
            kept[canon] = obj
        else:
            dropped.append({"file": rel, "series_key": raw_key,
                            "kept": _envelope_key(prev)})
    return list(kept.values()), dropped


def _raw_fields(row: dict[str, Any]) -> dict[str, Any]:
    s = row.get("raw_fields")
    if not isinstance(s, str) or not s.strip():
        return {}
    try:
        obj = json.loads(s)
    except json.JSONDecodeError:
        return {}
    return obj if isinstance(obj, dict) else {}


def _fill_from_raw_fields(row: dict[str, Any], col: str) -> Any:
    """列空时，尝试从 raw_fields 里回填（数据字典要的是"自动提取"）。"""
    v = row.get(col)
    if v not in (None, ""):
        return v
    rf = _raw_fields(row)
    for k in _RAW_FIELD_KEYS.get(col, ()):
        if rf.get(k) not in (None, ""):
            return rf[k]
    return ""


def _records(envelopes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把 processed 信封展平成统一长表记录。"""
    recs: list[dict[str, Any]] = []
    for obj in envelopes:
        mr = obj.get("missing_report") or {}
        key = str(mr.get("series_key") or "?")
        source = key.split("|")[0] if "|" in key else str(mr.get("source_file") or "?")
        for r in obj["rows"]:
            if not isinstance(r, dict):
                continue
            rec = {"source": source, "series_key": key}
            for col in ("region_code", "indicator_id", "indicator_name",
                        "period_type", "unit"):
                rec[col] = _fill_from_raw_fields(r, col)
            rec["period"] = r.get("period")
            v = r.get("value")
            rec["value"] = v if isinstance(v, (int, float)) and not isinstance(v, bool) else None
            for col in ("missing_classification", "missing_action", "missing_evidence",
                        "raw_cache", "row_sha16"):
                rec[col] = r.get(col, "")
            recs.append(rec)
    return recs


def write_csv(recs: list[dict[str, Any]]) -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # utf-8-sig：带 BOM，Excel 打开中文列名不乱码
    with CSV_PATH.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(COLUMNS), extrasaction="ignore")
        w.writeheader()
        for r in recs:
            w.writerow({c: r.get(c, "") for c in COLUMNS})
    return len(recs)


def write_sqlite(recs: list[dict[str, Any]]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = sqlite3.connect(str(DB_PATH))
    try:
        cols_sql = ", ".join(
            f"{c} REAL" if c == "value" else f"{c} TEXT" for c in COLUMNS)
        conn.execute(f"CREATE TABLE observations ({cols_sql})")
        placeholders = ", ".join("?" for _ in COLUMNS)
        conn.executemany(
            f"INSERT INTO observations ({', '.join(COLUMNS)}) VALUES ({placeholders})",
            [tuple(r.get(c) for c in COLUMNS) for r in recs],
        )
        conn.execute("CREATE INDEX idx_key_period ON observations(series_key, period)")
        conn.execute("""
            CREATE VIEW series_summary AS
            SELECT series_key, source, COUNT(*) AS n_rows,
                   COUNT(value) AS n_values,
                   COUNT(*) - COUNT(value) AS n_missing
            FROM observations
            GROUP BY series_key, source
        """)
        conn.commit()
    finally:
        conn.close()


def write_markdown(recs: list[dict[str, Any]], n_files: int) -> None:
    by_key: dict[str, list[dict[str, Any]]] = {}
    for r in recs:
        by_key.setdefault(str(r["series_key"]), []).append(r)

    n_missing = sum(1 for r in recs if r.get("value") is None)
    cls_dist: dict[str, int] = {}
    act_dist: dict[str, int] = {}
    for r in recs:
        c = str(r.get("missing_classification") or "")
        if c:
            cls_dist[c] = cls_dist.get(c, 0) + 1
        a = str(r.get("missing_action") or "")
        if a:
            act_dist[a] = act_dist.get(a, 0) + 1

    L: list[str] = []
    L.append("# 经济数据字典（自动生成）")
    L.append("")
    L.append("本文件由 `tools/export.py` 从 `data/processed/` 生成，**不要手工编辑**。")
    L.append("")
    L.append("## 全局统计")
    L.append("")
    L.append("| 指标 | 值 |")
    L.append("|---|---|")
    L.append(f"| 序列数 | {len(by_key)} |")
    L.append(f"| processed 文件数 | {n_files} |")
    L.append(f"| 总行数 | {len(recs)} |")
    L.append(f"| 有值行数 | {len(recs) - n_missing} |")
    L.append(f"| 缺失行数 | {n_missing} |")
    L.append("")
    L.append("缺失分类分布：")
    L.append("")
    for k, v in sorted(cls_dist.items()):
        L.append(f"* `{k}`: {v}")
    L.append("")
    L.append("缺失动作分布：")
    L.append("")
    for k, v in sorted(act_dist.items()):
        L.append(f"* `{k}`: {v}")
    L.append("")
    L.append("## 逐序列画像")
    L.append("")
    L.append("| series_key | source | indicator_id | indicator_name | unit | period_type | 观测年数 | 有值 | 缺失 | 缺失分类 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for key in sorted(by_key):
        rows = by_key[key]
        src = rows[0].get("source") or ""
        iid = _first_nonempty(rows, "indicator_id")
        iname = _first_nonempty(rows, "indicator_name")
        unit = _first_nonempty(rows, "unit")
        ptype = _first_nonempty(rows, "period_type")
        n_obs = len({str(r.get("period")) for r in rows})
        n_val = sum(1 for r in rows if r.get("value") is not None)
        n_mis = len(rows) - n_val
        dist: dict[str, int] = {}
        for r in rows:
            c = str(r.get("missing_classification") or "")
            if c:
                dist[c] = dist.get(c, 0) + 1
        dist_s = ", ".join(f"{k}:{v}" for k, v in sorted(dist.items())) or "-"
        L.append(f"| `{key}` | {src} | {iid} | {iname} | {unit} | {ptype} | "
                 f"{n_obs} | {n_val} | {n_mis} | {dist_s} |")
    L.append("")
    L.append("## 字段说明")
    L.append("")
    L.append("| 字段 | 含义 |")
    L.append("|---|---|")
    L.append("| source | 数据源标识（NBS / WorldBank / IMF） |")
    L.append("| series_key | 序列键：`source[:region][:indicator]` |")
    L.append("| value | 数值；缺失行为空（CSV）/ NULL（SQLite），**不做任何插值** |")
    L.append("| missing_classification | true_gap / not_yet_published / discontinued / series_start |")
    L.append("| missing_action | leave_null / wait / interpolate / manual_review |")
    L.append("| missing_evidence | 分类依据（人可读），来自 econ_core.missing |")
    L.append("| raw_cache | 原始响应存档路径（可回溯到 HTTP 原文） |")
    L.append("| row_sha16 | 行级指纹（normalize 层计算） |")
    L.append("")

    text = "\n".join(L)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    MD_PATH.write_text(text, encoding="utf-8")


def _first_nonempty(rows: list[dict[str, Any]], col: str) -> str:
    for r in rows:
        v = r.get(col)
        if v not in (None, ""):
            return str(v)
    return ""


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    print("=" * 96)
    print("export: data/processed -> CSV + SQLite + Markdown 数据字典")
    print("=" * 96)

    envelopes, dropped = _load_processed()
    recs = _records(envelopes)
    n_files = len(list(PROCESSED_DIR.rglob("*.json"))) if PROCESSED_DIR.is_dir() else 0
    print(f"\n  processed 文件数: {n_files}"
          + (f"（去重后 {len(envelopes)}，丢弃 alias 副本 {len(dropped)}）" if dropped else ""))
    for d in dropped:
        print(f"      丢弃 {d['file']}：series_key={d['series_key']} 与 {d['kept']} 是同一条序列")
    print(f"  展平后行数      : {len(recs)}")
    n_missing = sum(1 for r in recs if r.get("value") is None)
    print(f"  其中缺失行      : {n_missing}（value 为空/NULL，保留不丢）")

    n_csv = write_csv(recs)
    write_sqlite(recs)
    write_markdown(recs, len(envelopes))

    print()
    print(f"  [1] CSV     : {CSV_PATH.relative_to(PROJECT_ROOT)}  ({n_csv} 行, "
          f"{CSV_PATH.stat().st_size:,} 字节)")
    print(f"  [2] SQLite  : {DB_PATH.relative_to(PROJECT_ROOT)}  ({DB_PATH.stat().st_size:,} 字节)")

    conn = sqlite3.connect(str(DB_PATH))
    try:
        n_obs = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        n_series = conn.execute("SELECT COUNT(*) FROM series_summary").fetchone()[0]
        print(f"      observations 行数 = {n_obs}；series_summary 视图序列数 = {n_series}")
        print("      series_summary:")
        for row in conn.execute(
                "SELECT series_key, source, n_rows, n_values, n_missing "
                "FROM series_summary ORDER BY series_key"):
            print(f"        {row[0][:46]:<46} {row[1]:<10} rows={row[2]:>3} "
                  f"values={row[3]:>3} missing={row[4]:>2}")
    finally:
        conn.close()
    print(f"  [3] 数据字典: {MD_PATH.relative_to(PROJECT_ROOT)}  ({MD_PATH.stat().st_size:,} 字节)")
    print()
    print("  [PASS] 三个产物已生成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
