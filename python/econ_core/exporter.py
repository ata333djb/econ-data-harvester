#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""exporter.py —— 把 `edh fetch` 的产物落进 **validated 层**，接上验证链路。

为什么需要它
------------
`edh fetch` 把数据打给用户就结束了：CSV 到 stdout、简报到 stderr，**不落任何一层 data/**。
于是用户拿到的数据和系统的画像 / 判定 / 评分 / 报告链路是**断开的** ——
那些下游全都读 `data/validated/`，而 fetch 从不往那里写。

本模块补上这一环：**取数 -> 转长表 -> 落 validated**，下游（`scan-missing` ->
`fill_strategy` -> `credibility` -> `report`）就能自动看到新序列。

两套键的映射靠 `catalog_data.yaml` 的 `kb_series_key`
----------------------------------------------------
目录层的键是人话（`CPI` / `GDP_GROWTH`），知识库的键是规范键
（`nbs|cpi|全国居民消费价格指数（上年=100） (%)`）。不打通，`source_profiler` 就抛 KeyError。
`kb_series_key` 就是这条映射，**只有"同一个源 + 同一个统计口径"才算对上**；
对不上的（45/57 处）是 `null` —— 那些行**不落盘**，只进报告的 `unmapped` 列表。

落盘形状：**与 `materialize-validated.py` 完全一致**
----------------------------------------------------
信封 = `normalize.write_validated()` 的 5 个键（`name` / `row_count` / `written_at` /
`columns` / `rows`）**再加** `series_key` 与 `expected_periods`。行的序列化与 columns
交给 `normalize`（**单一事实来源**），本模块不自己拼 —— 这样两边永远不会漂移。
（`materialize-validated.py` 的 docstring 明确说这是刻意设计，此处沿用，不复用它的代码。）

⚠️ 两处与任务书字面不同的地方（都是为了"下游真能读"）
----------------------------------------------------
1. **`expected_periods` 写成列表，不是字符串**。任务书写的是 `"2015-2024"`，
   但 `fill_strategy._scan_validated_series()` 把它当**期间网格**用
   （`missing` 靠它判 series_start）。写成字符串，头部缺口就永远判不出来 ——
   下游要的是 `["2015", "2016", ...]`。另附一个 `expected_periods_label`（字符串）
   满足人读的需要。
2. **默认拒绝"缩水覆盖"**。`edh export CPI --from 2020 --to 2024` 会命中
   `data/validated/bis/bis_WS_LONG_CPI_M.CN.628.json`（该文件有 380 期，1995 起）。
   直接覆盖 = 把 380 期砍成 60 期，**静默毁掉证据链**。所以默认跳过并报
   `skipped_shrink`，要覆盖得显式 `--force`。

已知缺口
--------
`raw_cache` / `fetched_at` **无法回填**：`fetcher` 的统一 9 字段行里没有它们
（那是 `http_client` 存档层的信息）。所以落盘时 `raw_cache=""`、
`fetched_at` 记的是**导出时刻**，并在 `raw_fields` 里注明 —— 不假装有原始存档链接。

用法
----
    .\\.venv\\Scripts\\python.exe -m econ_core.exporter --test
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from econ_core import catalog, fetcher, http_client, normalize  # type: ignore[no-redef]
else:
    from . import catalog, fetcher, http_client, normalize

__all__ = [
    "ExporterError",
    "VALIDATED_DIR",
    "export_rows",
    "export_indicator",
    "refresh_profiles",
    "last_report",
    "group_rows",
    "resolve_kb_key",
]

PROJECT_ROOT: Path = http_client.PROJECT_ROOT
VALIDATED_DIR: Path = PROJECT_ROOT / "data" / "validated"

#: 长表 14 列（顺序固定，见 PROJECT_STATE §2.3）
LONG_COLUMNS: tuple[str, ...] = (
    "region_code", "region_name", "indicator_id", "tree_node_id", "indicator_name",
    "period", "period_type", "value", "unit", "source", "fetched_at", "raw_cache",
    "row_sha16", "raw_fields",
)

#: 各源的地区码/地区名约定（**照抄现有 validated 文件的实测值**，不自己发明）：
#: nbs / bis / fred 用 12 位全国码，imf / worldbank 用 ISO3。
_REGION_BY_SOURCE: dict[str, tuple[str, str]] = {
    "nbs": ("000000000000", "全国"),
    "bis": ("000000000000", "中国"),
    "fred": ("000000000000", "全国"),
    "imf": ("CHN", "China"),
    "worldbank": ("CHN", "China"),
}

#: Windows 非法文件名字符（保留中文）—— 与 materialize-validated.py 同一规则
_FORBIDDEN = r'[\\/:*?"<>|\x00-\x1f]+'

_REPORT: dict[str, Any] = {}


class ExporterError(RuntimeError):
    """导出层可预期的失败（指标不存在、行不是 fetcher 产物等）。"""


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #

def _safe(name: str) -> str:
    """series_key / source -> 安全文件名（只替换 Windows 非法字符，保留中文）。"""
    return re.sub(_FORBIDDEN, "_", str(name)).strip(" ._") or "unknown"


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _month_window(y0: int, m0: int, y1: int, m1: int) -> list[str]:
    """闭区间 ``YYYY-MM`` 月度窗口。"""
    out: list[str] = []
    y, m = y0, m0
    while (y, m) <= (y1, m1):
        out.append(f"{y}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def window_grid(lo: int, hi: int, period_type: str) -> list[str]:
    """把年份区间展开成**期望期间网格**（下游判缺口要用它）。"""
    if period_type == "monthly":
        return _month_window(lo, 1, hi, 12)
    if period_type == "quarterly":
        return [f"{y}-Q{q}" for y in range(lo, hi + 1) for q in range(1, 5)]
    return [str(y) for y in range(lo, hi + 1)]


# --------------------------------------------------------------------------- #
# kb_series_key 解析
# --------------------------------------------------------------------------- #

def resolve_kb_key(indicator: str, source: str,
                   frequency: Optional[str] = None) -> tuple[Optional[str], str]:
    """找出行应该用的知识库键。

    解析顺序：**同频率的变体优先** -> 主映射。为什么要看频率：
    `UNEMPLOYMENT.nbs` 主映射是月度（调查失业率），变体 `registered` 是年度（登记失业率）
    —— 两者在知识库里是**两条不同的序列**（`nbs|surveyed_unemployment` vs
    `nbs|registered_unemployment`），只按 source 取会把登记口径的键安到调查口径的行上。

    :returns: `(kb_series_key 或 None, 命中的口径说明)`
    """
    spec = catalog.get_source_config(indicator, source)
    if frequency:
        want = str(frequency).lower()
        for var in spec.get("variants") or []:
            if str(var.get("frequency") or "").lower() == want:
                return var.get("kb_series_key"), f"variant:{var.get('variant')}"
        if str(spec.get("frequency") or "").lower() != want:
            # 频率对不上任何一份 -> 仍用主映射（让调用方按 null 处理）
            return spec.get("kb_series_key"), "primary(freq-mismatch)"
    return spec.get("kb_series_key"), "primary"


def group_rows(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """按 `(source, kb_series_key)` 分组。

    :returns: `{"groups": {(source, kb): {"rows": [...], "frequency": ...}},
               "unmapped": [{"source", "indicator", "n_rows", "reason"}]}`
    """
    groups: dict[tuple[str, Optional[str]], dict[str, Any]] = {}
    unmapped: dict[str, dict[str, Any]] = {}

    for r in rows:
        if not isinstance(r, dict) or "period" not in r or "source" not in r:
            raise ExporterError(f"行不是 fetcher 产物（缺 period/source）: {str(r)[:80]}")
        indicator = str(r.get("indicator") or "")
        source = str(r["source"]).lower()
        freq = str(r.get("frequency") or r.get("period_type") or "") or None
        try:
            kb, how = resolve_kb_key(indicator, source, freq)
        except (KeyError, catalog.CatalogError) as exc:
            kb, how = None, f"lookup-failed: {exc}"

        if not kb:
            key = f"{indicator}.{source}"
            ent = unmapped.setdefault(key, {"indicator": indicator, "source": source,
                                            "n_rows": 0, "reason": how,
                                            "frequency": freq})
            ent["n_rows"] += 1
            continue

        g = groups.setdefault((source, kb), {"source": source, "kb_series_key": kb,
                                             "indicator": indicator,
                                             "frequency": freq, "rows": []})
        g["rows"].append(r)

    for g in groups.values():
        g["rows"].sort(key=lambda x: str(x["period"]))
    return {"groups": groups, "unmapped": sorted(unmapped.values(),
                                                 key=lambda x: (x["source"], x["indicator"]))}


# --------------------------------------------------------------------------- #
# 行转换：fetcher 9 字段 -> validated 14 列
# --------------------------------------------------------------------------- #

def to_long_rows(rows: list[dict[str, Any]], source: str,
                 indicator_id: str, series_key: str,
                 fetched_at: str) -> list[dict[str, Any]]:
    """把 fetcher 的统一行转成 validated 长表行（14 列 [+ 可选列]）。

    `value` 保持 **float 或 None**（与现有 validated 文件实测一致）；
    `raw_cache` 留空并在 `raw_fields` 里注明原因（见模块 docstring 的"已知缺口"）。
    """
    region_code, region_name = _REGION_BY_SOURCE.get(source, ("CHN", "China"))
    out: list[dict[str, Any]] = []
    for r in rows:
        raw_fields: dict[str, Any] = {
            "exported_by": "edh export",
            "catalog_indicator": r.get("indicator"),
            "series_name": r.get("series_name"),
            "raw_cache_note": "edh export 不回填 raw_cache（fetcher 的统一行不含它）",
        }
        if r.get("aggregated_from_months") is not None:
            raw_fields["aggregated_from_months"] = r["aggregated_from_months"]
        if r.get("n_non_null_months") is not None:
            raw_fields["n_non_null_months"] = r["n_non_null_months"]

        values: dict[str, Any] = {
            "region_code": region_code,
            "region_name": region_name,
            "indicator_id": indicator_id,
            "tree_node_id": "",
            "indicator_name": r.get("series_name") or series_key,
            "period": str(r["period"]),
            "period_type": r.get("period_type") or "annual",
            "value": r.get("value"),
            "unit": r.get("unit") or "",
            "source": source,
            "fetched_at": fetched_at,
            "raw_cache": "",
            "raw_fields": json.dumps(raw_fields, ensure_ascii=False, sort_keys=True),
        }
        # 必须按 LONG_COLUMNS 的顺序建 dict：`normalize.write_validated` 用
        # `list(rows[0].keys())` 当 columns，而现有 validated 文件里
        # **row_sha16 在 raw_fields 之前**。
        # 注意两步不能合并：`_row_sha16` 会把**除 row_sha16 之外的所有字段**（含
        # raw_fields）一起哈希，所以要先在完整 13 字段上算指纹，再按规范顺序把
        # 指纹插到第 13 个位置。第一版"先建 13 字段再 append 指纹"把顺序弄错了；
        # 第二版"边建边算"又会让指纹漏掉 raw_fields —— 都被自检 [3] 拦下。
        sha = normalize._row_sha16(values)  # noqa: SLF001 —— 与 normalize 同源
        row: dict[str, Any] = {k: (sha if k == "row_sha16" else values[k])
                               for k in LONG_COLUMNS}
        if r.get("aggregated_from_months") is not None:
            row["aggregated_from_months"] = r["aggregated_from_months"]
        out.append(row)
    return out


def _indicator_id_for(indicator: str, source: str) -> str:
    """该源上这条序列的 `indicator_id`（照抄现有 validated 文件的口径）。"""
    spec = catalog.get_source_config(indicator, source)
    if source == "bis":
        return f"{spec.get('dataset')}|{spec.get('key')}"
    if source == "fred":
        return str(spec.get("series_id") or "")
    if source in ("imf", "worldbank"):
        return str(spec.get("indicator_code") or "")
    if source == "nbs":
        return str(spec.get("indicator_id") or "")
    return ""


def _existing_series(path: Path) -> Optional[tuple[set[str], str]]:
    """读已有落盘文件的 `(期间集合, period_type)`；不存在或结构不符返回 None。"""
    if not path.is_file():
        return None
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    rows = obj.get("rows") if isinstance(obj, dict) else None
    if not isinstance(rows, list) or not rows:
        return None
    periods = {str(r.get("period")) for r in rows if isinstance(r, dict) and r.get("period")}
    ptype = str(rows[0].get("period_type") or "") if isinstance(rows[0], dict) else ""
    return periods, ptype


# --------------------------------------------------------------------------- #
# 公开接口
# --------------------------------------------------------------------------- #

def export_rows(rows: list[dict[str, Any]], to_validated: bool = True,
                dry_run: bool = False, force: bool = False,
                out_dir: Optional[Path] = None,
                window: Optional[tuple[int, int]] = None) -> dict[str, Any]:
    """把 fetcher 的行按 `(source, kb_series_key)` 分组落盘到 validated 层。

    :param rows: `fetcher.fetch_indicator()` / `fetch_all_sources()` 的行。
    :param to_validated: `False` 时只分组不落盘（等价于 dry_run）。
    :param dry_run: `True` 时走完全部判断但**不写文件**。
    :param force: 覆盖"缩水"文件（新导出的期间是已有文件的真子集）。默认拒绝。
    :param out_dir: 覆盖输出根目录（自检用，避免污染真的 validated 层）。
    :param window: `(起始年, 结束年)`，用来算 `expected_periods` 网格。
    :returns: `{"written": [...], "skipped": [...], "unmapped": [...],
                "n_written", "n_rows_written", "dry_run", "out_dir"}`
    """
    root = Path(out_dir) if out_dir else VALIDATED_DIR
    now = _utc_now()
    grouped = group_rows(rows)

    written: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for (source, kb_key), g in sorted(grouped["groups"].items(), key=lambda kv: kv[0][1]):
        grows = g["rows"]
        series_key = str(kb_key)
        freq = str(g.get("frequency") or "annual")
        ptype = str(grows[0].get("period_type") or "annual")

        out_sub = root / _safe(source)
        path = out_sub / f"{_safe(series_key.replace('|', '_'))}.json"

        # --- 覆盖保护：绝不用"更窄"或"另一种粒度"的序列覆盖已有文件 ---
        # 两种要拦的情况（第一版只拦了第一种，还把第二种误报成"少 N 期"）：
        #   ① 同粒度但期间是真子集  -> skipped_shrink（例：已有 380 期月度，本次 60 期）
        #   ② 粒度不同              -> shape_conflict（例：已有 10 期**年度**，本次 60 期**月度**）
        #      第二版实测把 fred 的 10 期年度 vs 60 期月度算成"少 10 期" —— 拿年度标签
        #      去减月度标签，得到的差毫无意义。
        prev = _existing_series(path)
        new_periods = {str(r["period"]) for r in grows}
        if prev and not force:
            prev_periods, prev_ptype = prev
            if prev_ptype and ptype and prev_ptype != ptype:
                skipped.append({
                    "series_key": series_key, "source": source, "reason": "shape_conflict",
                    "path": str(path.relative_to(PROJECT_ROOT)) if root == VALIDATED_DIR else str(path),
                    "existing_periods": len(prev_periods), "new_periods": len(new_periods),
                    "detail": (f"已有文件是 {prev_ptype}（{len(prev_periods)} 期），"
                               f"本次是 {ptype}（{len(new_periods)} 期）—— 粒度不同，"
                               f"覆盖会换掉整条序列的形状，需 --force"),
                })
                continue
            missing = prev_periods - new_periods
            if missing:
                skipped.append({
                    "series_key": series_key, "source": source, "reason": "skipped_shrink",
                    "path": str(path.relative_to(PROJECT_ROOT)) if root == VALIDATED_DIR else str(path),
                    "existing_periods": len(prev_periods), "new_periods": len(new_periods),
                    "detail": (f"已有文件覆盖 {len(prev_periods)} 期，本次只有 {len(new_periods)} 期"
                               f"（少 {len(missing)} 期）—— 覆盖会缩水，需 --force"),
                })
                continue

        indent = g.get("indicator") or ""
        long_rows = to_long_rows(grows, source, _indicator_id_for(str(indent), source),
                                 series_key, now)

        grid: list[str] = []
        if window:
            grid = window_grid(int(window[0]), int(window[1]), ptype)

        entry = {
            "series_key": series_key, "source": source, "indicator": indent,
            "n_rows": len(long_rows), "rows": len(grows),
            "periods": f"{long_rows[0]['period']}..{long_rows[-1]['period']}" if long_rows else "",
            "frequency": freq, "period_type": ptype,
        }

        if dry_run or not to_validated:
            entry["action"] = "would-write"
            entry["grid_n"] = len(grid)
            written.append(entry)
            continue

        # 1) 行序列化与 columns 交给 normalize（单一事实来源）
        p = normalize.write_validated(long_rows, path.stem, out_dir=out_sub)

        # 2) 追加 series_key / expected_periods（write_validated 的信封里没有）
        obj = json.loads(p.read_text(encoding="utf-8"))
        obj["series_key"] = series_key
        obj["expected_periods"] = grid
        obj["expected_periods_label"] = (f"{grid[0]}..{grid[-1]}" if grid else "")
        obj["source"] = "edh export"
        p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")

        # 3) 回读校验（materialize-validated.py 同款做法）
        back = json.loads(p.read_text(encoding="utf-8"))
        if back.get("series_key") != series_key or not isinstance(back.get("expected_periods"), list):
            raise ExporterError(f"{series_key}: 回读校验失败（series_key/expected_periods 未落盘）")

        entry["action"] = "written"
        entry["path"] = (str(p.relative_to(PROJECT_ROOT)) if root == VALIDATED_DIR else str(p))
        entry["bytes"] = p.stat().st_size
        written.append(entry)

    report = {
        "written": written,
        "skipped": skipped,
        "unmapped": grouped["unmapped"],
        "n_written": sum(1 for e in written if e.get("action") == "written"),
        "n_would_write": sum(1 for e in written if e.get("action") == "would-write"),
        "n_skipped": len(skipped),
        "n_rows_written": sum(e["n_rows"] for e in written if e.get("action") == "written"),
        "n_rows_total": sum(e["n_rows"] for e in written),
        "dry_run": bool(dry_run or not to_validated),
        "out_dir": str(root),
        "exported_at": now,
    }
    _REPORT.clear()
    _REPORT.update(report)
    for u in report["unmapped"]:
        print(f"[警告] 跳过 {u['n_rows']} 行：{u['indicator']}.{u['source']} "
              f"在知识库里没有条目（kb_series_key=null，{u['reason']}）", file=sys.stderr)
    return report


def export_indicator(indicator: str, **fetch_kwargs: Any) -> dict[str, Any]:
    """便捷封装：取数 + 落盘一步到位。

    透传 `fetch_kwargs` 给 `fetcher.fetch_indicator`（`source` / `from_year` /
    `to_year` / `frequency` / `region` / `allow_forecast`），外加两个本层参数：
    `dry_run` / `force` / `out_dir`。

    :raises KeyError: 指标或源不存在（由 fetcher 抛出）。
    """
    dry_run = bool(fetch_kwargs.pop("dry_run", False))
    force = bool(fetch_kwargs.pop("force", False))
    out_dir = fetch_kwargs.pop("out_dir", None)

    rows = fetcher.fetch_indicator(indicator, **fetch_kwargs)
    lo, hi = fetcher.default_window(fetch_kwargs.get("from_year"),
                                    fetch_kwargs.get("to_year"))
    return export_rows(rows, dry_run=dry_run, force=force, out_dir=out_dir,
                       window=(lo, hi))


def refresh_profiles(series_keys: Iterable[str]) -> list[dict[str, Any]]:
    """确认画像层能不能读到刚落盘的序列（`--refresh` 用）。

    只做一件事：对每个 series_key 调 `source_profiler.profile_series()`，
    把 KeyError 与成功分开报 —— **不重跑门禁**（那太重，见任务 D 的简化版说明）。
    """
    try:
        from . import source_profiler  # 局部导入：本模块其余部分不依赖它
    except ImportError:  # 直接以脚本运行时
        from econ_core import source_profiler  # type: ignore[no-redef]

    out: list[dict[str, Any]] = []
    for key in series_keys:
        try:
            prof = source_profiler.profile_series(str(key))
            out.append({"series_key": key, "ok": True,
                        "display_name": (prof.get("indicator") or {}).get("display_name"),
                        "publisher": (prof.get("publisher") or {}).get("key")})
        except Exception as exc:  # noqa: BLE001 —— KeyError 及一切画像层异常
            out.append({"series_key": key, "ok": False,
                        "error": f"{type(exc).__name__}: {exc}"})
    return out


def last_report() -> dict[str, Any]:
    """最近一次 `export_rows` 的报告。"""
    return dict(_REPORT)


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #

def _selftest() -> int:
    """自检。**会联网**（要真取 CPI 数据），但**不污染真的 validated 层** ——
    全部写进项目根的 `.exporter-selftest/` 并在结束时删掉。

    为什么坚持不写真的 validated：那里的文件会被 `scan-missing` / `fill_strategy` /
    `credibility` / `report` 扫到，自检往里面留东西会**改变下游的数字**
    （现有那条 selftest 残留就让 `n_series` 从 12 变成 13，见 PROJECT_STATE §1.3）。

    为什么不用 `tempfile.mkdtemp()`：本沙箱把 `os.mkdir(path, 0o700)` 当**禁止性 ACL**，
    mkdtemp 建出来的目录**连创建者自己都写不进去**（实测 `PermissionError: [WinError 5]`，
    见 PROJECT_STATE §3.7 与 `pip_sandbox_install.py` 的 docstring）。
    普通 `Path.mkdir()` 没这个问题，所以这里用一个固定的工作区目录。
    """
    import shutil

    failures: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            failures.append(msg)

    print("=" * 92)
    print("self-test: exporter（分组 / null 跳过 / 落盘格式 / 真数据 / dry-run）")
    print("=" * 92)

    tmp = PROJECT_ROOT / ".exporter-selftest"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        # 1) 分组：同一 source + kb_series_key 归到一起
        fake = [
            {"indicator": "GDP", "source": "nbs", "period": "2020", "value": 1.0,
             "frequency": "annual", "period_type": "annual", "unit": "亿元",
             "series_name": "x"},
            {"indicator": "GDP", "source": "nbs", "period": "2021", "value": 2.0,
             "frequency": "annual", "period_type": "annual", "unit": "亿元",
             "series_name": "x"},
            {"indicator": "GDP", "source": "imf", "period": "2020", "value": 3.0,
             "frequency": "annual", "period_type": "annual", "unit": "十亿美元",
             "series_name": "NGDPD"},
        ]
        g = group_rows(fake)
        print(f"\n[1] group_rows(3 行) -> {len(g['groups'])} 组，"
              f"unmapped {len(g['unmapped'])} 条")
        for (src, kb), ent in sorted(g["groups"].items()):
            print(f"      {src:<10} {kb:<28} {len(ent['rows'])} 行")
        check(len(g["groups"]) == 2, f"1: 期望 2 组，实际 {len(g['groups'])}")
        nbs_key = ("nbs", "nbs|gdp|cny_100m")
        check(nbs_key in g["groups"] and len(g["groups"][nbs_key]["rows"]) == 2,
              "1: 同 source+kb_key 的两行应归到同一组")
        check(not g["unmapped"], "1: 这三行都能映射到知识库，不该有 unmapped")

        # 2) kb_series_key=null 的行被跳过并记录
        nul = [{"indicator": "POPULATION", "source": "nbs", "period": "2020",
                "value": 1.0, "frequency": "annual", "period_type": "annual",
                "unit": "万人", "series_name": "年末总人口"},
               {"indicator": "GDP", "source": "nbs", "period": "2020", "value": 1.0,
                "frequency": "annual", "period_type": "annual", "unit": "亿元",
                "series_name": "x"}]
        rep2 = export_rows(nul, dry_run=True, out_dir=tmp)
        print(f"\n[2] 含 1 条 kb=null -> unmapped {rep2['unmapped']}")
        check(len(rep2["unmapped"]) == 1, f"2: 期望 1 条 unmapped，实际 {len(rep2['unmapped'])}")
        check(rep2["unmapped"][0]["indicator"] == "POPULATION", "2: unmapped 应是 POPULATION")
        check(rep2["unmapped"][0]["n_rows"] == 1, "2: unmapped 行数应为 1")
        check(rep2["n_would_write"] == 1, f"2: 应只剩 1 组可写，实际 {rep2['n_would_write']}")

        # 3) 落盘格式与 materialize-validated.py 一致
        rep3 = export_rows(fake, out_dir=tmp, window=(2020, 2021))
        print(f"\n[3] 落盘 {rep3['n_written']} 个文件 -> {rep3['written'][0].get('path')}")
        files = sorted(tmp.rglob("*.json"))
        check(len(files) == 2, f"3: 期望 2 个文件，实际 {len(files)}")
        obj = json.loads(files[0].read_text(encoding="utf-8"))
        for k in ("name", "row_count", "written_at", "columns", "rows",
                  "series_key", "expected_periods"):
            check(k in obj, f"3: 信封缺 {k}")
        check(isinstance(obj["expected_periods"], list),
              "3: expected_periods 必须是列表（fill_strategy 当网格用）")
        check(obj["expected_periods"] == ["2020", "2021"],
              f"3: 网格期望 ['2020','2021']，实际 {obj['expected_periods']}")
        check(list(obj["rows"][0].keys())[:14] == list(LONG_COLUMNS),
              f"3: 前 14 列顺序不符：{list(obj['rows'][0].keys())[:14]}")
        check(all(r.get("row_sha16") for r in obj["rows"]), "3: 每行都要有 row_sha16")
        check(obj.get("source") == "edh export", "3: 信封 source 应为 'edh export'")
        print(f"      信封键: {list(obj.keys())}")
        print(f"      网格  : {obj['expected_periods']}")

        # 4) 真数据：CPI（2020-2024）
        rep4 = export_indicator("CPI", from_year=2020, to_year=2024, out_dir=tmp)
        print(f"\n[4] export_indicator('CPI', 2020-2024) -> 落盘 {rep4['n_written']} 个、"
              f"跳过 {rep4['n_skipped']} 个、unmapped {len(rep4['unmapped'])} 条")
        for e in rep4["written"]:
            print(f"      [{e['action']}] {e['source']:<10} {e['series_key'][:46]:<48} "
                  f"{e['n_rows']:>4} 行  {e.get('periods','')}")
        check(rep4["n_written"] >= 3,
              f"4: CPI 至少应落 3 个文件（nbs/fred/bis），实际 {rep4['n_written']}")
        for e in rep4["written"]:
            check(e["n_rows"] > 0, f"4: {e['series_key']} 落盘 0 行")
        # unmapped 里不该出现已映射的三个源
        bad = [u for u in rep4["unmapped"] if u["source"] in ("nbs", "fred", "bis")]
        check(not bad, f"4: 已映射的源不该进 unmapped: {bad}")

        # 5) dry-run 不写文件
        before = sorted(str(p) for p in tmp.rglob("*.json"))
        rep5 = export_indicator("CPI", from_year=2020, to_year=2024, dry_run=True,
                                out_dir=tmp, force=True)
        after = sorted(str(p) for p in tmp.rglob("*.json"))
        print(f"\n[5] dry-run -> would-write {rep5['n_would_write']}，文件数 {len(before)} -> {len(after)}")
        check(rep5["dry_run"] is True, "5: dry_run 标志应为 True")
        check(rep5["n_written"] == 0, f"5: dry-run 不该真写，n_written={rep5['n_written']}")
        check(before == after, "5: dry-run 改变了文件集合")

        # 6) 覆盖保护：更窄的窗口 / 不同的粒度，都必须被拦下
        rep6 = export_indicator("CPI", from_year=2024, to_year=2024, out_dir=tmp)
        print(f"\n[6] 缩水保护：2024 only -> 跳过 {rep6['n_skipped']} 个")
        for s in rep6["skipped"]:
            print(f"      [{s['reason']}] {s['source']:<10} {s['detail']}")
        check(rep6["n_skipped"] >= 1, "6: 更窄的窗口应被缩水保护拦下")
        check(all(s["reason"] == "skipped_shrink" for s in rep6["skipped"]),
              f"6: 同粒度子集应报 skipped_shrink，实际 {[s['reason'] for s in rep6['skipped']]}")
        rep6f = export_indicator("CPI", from_year=2024, to_year=2024, out_dir=tmp, force=True)
        check(rep6f["n_written"] >= 1, "6: --force 应能覆盖")

        # 6b) 粒度冲突：已有年度文件，再导月度 -> shape_conflict（不是"少 N 期"）
        shape_dir = tmp / "shape"
        ann = [{"indicator": "GDP", "source": "nbs", "period": str(y), "value": 1.0,
                "frequency": "annual", "period_type": "annual", "unit": "亿元",
                "series_name": "s"} for y in (2020, 2021)]
        export_rows(ann, out_dir=shape_dir, window=(2020, 2021))
        mon = [{"indicator": "GDP", "source": "nbs", "period": f"2020-{m:02d}", "value": 1.0,
                "frequency": "monthly", "period_type": "monthly", "unit": "亿元",
                "series_name": "s"} for m in range(1, 13)]
        rep6b = export_rows(mon, out_dir=shape_dir)
        print(f"\n[6b] 粒度冲突：年度文件 + 月度导出 -> {[s['reason'] for s in rep6b['skipped']]}")
        check(len(rep6b["skipped"]) == 1 and rep6b["skipped"][0]["reason"] == "shape_conflict",
              f"6b: 期望 1 条 shape_conflict，实际 {rep6b['skipped']}")
        check(rep6b["n_written"] == 0, "6b: 粒度冲突时不该写文件")

        # 7) 错误路径
        try:
            export_indicator("NONEXIST")
            failures.append("7: 不存在的指标应抛错")
        except KeyError:
            print("\n[7] 不存在的指标 -> KeyError")
        try:
            export_rows([{"period": "2020"}])
            failures.append("7: 缺 source 的行应抛 ExporterError")
        except ExporterError:
            print("    缺 source 的行 -> ExporterError")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        print(f"\n[清理] 自检目录已删除: {tmp}")

    print("-" * 92)
    if failures:
        print(f"FAIL: {len(failures)} 项")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS: exporter 自检全部通过")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    parser = argparse.ArgumentParser(
        prog="econ_core.exporter",
        description="把 edh fetch 的产物落进 validated 层（自检**会联网**）。")
    parser.add_argument("--test", action="store_true", help="跑自检")
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.test:
        return _selftest()
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
