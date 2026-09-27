#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""指标目录（catalog）—— 从「用户说得出名字的指标」到「哪个源能取、要什么参数」。

为什么需要它
------------
在此之前这个项目只有**开发者工具**：要知道 `nbs_client.fetch_indicator_data` 需要
`cid` / `indicator_id` / `root_id` 三个 UUID，还要自己翻目录树把 UUID 找出来。
那三个 UUID 是**上游实现细节**，用户不该看见，也不该记。

本模块是**用户产品的第一层**：一份人类可读的指标清单（`catalog_data.yaml`），
把「GDP 增速」这样的说法映射到各源真正需要的参数。第二层是 `tools/edh.py`
（`edh list` / `edh info`），再下一轮才是 `edh fetch`。

三个接口
--------
`list_indicators()`                     全部指标（可直接渲染成表格）
`get_indicator(name)`                   单个指标；name 可以是规范名 / 别名 / 中文
`get_source_config(indicator, source)`  某指标在某源上的取数参数

清单是**人工编纂 + 机器验证**的
------------------------------
`catalog_data.yaml` 里的每一个源映射都由 `python/_probes/probe_catalog_sources.py`
的 `verify` 阶段**真跑过一次取数**确认过（证据：`data/raw/_probe_catalog/verified.json`）。
本模块只负责**读**它，不联网、不猜测 —— 所以 `--test` 是纯离线的。

为什么缺源是「键不存在」而不是「值为 null」
------------------------------------------
与信封契约（PROJECT_STATE §2.2）同一原则：**没有就说没有，不要写 null**。
`indicators.GOVERNMENT_DEBT.sources` 里没有 `nbs` 键，就表示 NBS 不提供这个指标
（实测：NBS 目录树里「政府债务」0 命中，「国债发行额」是**发行量**不是债务余额，
口径不同不能顶替）。调用方用 `sources_for()` 取可用源列表，不要用 `.get()` 后判 None。

别名必须全局唯一
----------------
别名冲突在加载时就报错（`_build_alias_index`）。这条不是洁癖：`edh info` 靠别名
反查规范名，两个指标共用一个别名会让查询结果**取决于字典顺序**，是静默错答。
实测踩到过一次：需求清单里 `CPI` 与 `INFLATION` 都想要 `inflation` 这个别名，
已改为 `CPI: [cpi, 消费价格, consumer price index]` /
`INFLATION: [inflation, 通胀, 通胀率]`。

用法
----
    .\\.venv\\Scripts\\python.exe -m econ_core.catalog --test
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 注意：本模块**不导入 http_client**。目录层是纯离线的（只读 YAML），
# 没有任何网络访问，因此不需要 venv 校验，也不想让 `import catalog` 牵出网络栈。

__all__ = [
    "CatalogError",
    "CATALOG_PATH",
    "SOURCES",
    "REQUIRED_KEYS",
    "load_catalog",
    "list_indicators",
    "list_sources",
    "get_indicator",
    "get_source_config",
    "sources_for",
    "resolve_name",
    "catalog_summary",
]

#: 指标清单的位置（与 `source_profiles.yaml` 并列，都是人工编纂的知识文件）。
CATALOG_PATH: Path = Path(__file__).resolve().parent / "catalog_data.yaml"

#: 支持的五个源（顺序即展示顺序）。
SOURCES: tuple[str, ...] = ("nbs", "worldbank", "imf", "fred", "bis")

#: 每个源的映射**必须**带的键。缺键在加载时报错，而不是等到取数时才炸
#: （取数时往往已经过了好几秒的网络往返，报错还看不出是清单写错了）。
REQUIRED_KEYS: dict[str, tuple[str, ...]] = {
    "nbs": ("cid", "indicator_id", "root_id", "frequency"),
    "worldbank": ("indicator_code",),
    "imf": ("indicator_code",),
    "fred": ("series_id",),
    "bis": ("dataset", "key"),
}

#: 指标级必须带的键。
_INDICATOR_KEYS: tuple[str, ...] = ("display_name", "description", "aliases", "frequency",
                                    "sources")

_CACHE: dict[str, dict[str, Any]] = {}


class CatalogError(RuntimeError):
    """目录层可预期的失败（清单缺失 / 结构不符 / 别名冲突 / 查不到指标）。"""


# --------------------------------------------------------------------------- #
# 加载与校验
# --------------------------------------------------------------------------- #

def _norm(text: str) -> str:
    """别名/规范名的比较用归一化：小写 + 去掉空格与 ``_``/``-``。

    这样 ``GDP_PER_CAPITA`` / ``gdp per capita`` / ``gdp-per-capita`` 是同一个键。
    **不动中文**（中文里的空格有意义，例如「居民消费价格指数（上年=100）」）。
    """
    out = []
    for ch in str(text).strip().lower():
        if ch in " _-":
            continue
        out.append(ch)
    return "".join(out)


def load_catalog(path: Optional[Path] = None) -> dict[str, Any]:
    """加载 `catalog_data.yaml`（带进程内缓存 + 结构校验）。

    :raises CatalogError: 缺 PyYAML、文件不存在、结构不符、别名冲突、源缺必需键。
    """
    p = Path(path) if path else CATALOG_PATH
    cache_key = str(p)
    if cache_key in _CACHE:
        return _CACHE[cache_key]

    try:
        import yaml  # 局部导入：缺依赖时报错更清楚
    except ImportError as exc:  # pragma: no cover - 环境问题
        raise CatalogError(
            "加载指标目录需要 PyYAML。请先安装："
            ".\\.venv\\Scripts\\python.exe pip_sandbox_install.py install pyyaml"
        ) from exc

    if not p.is_file():
        raise CatalogError(f"指标目录文件不存在: {p}")
    try:
        obj = yaml.safe_load(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - yaml 的异常类型多变
        raise CatalogError(f"指标目录 YAML 解析失败 {p}: {exc}") from exc

    if not isinstance(obj, dict):
        raise CatalogError(f"指标目录顶层不是 mapping: {type(obj).__name__}")
    indicators = obj.get("indicators")
    if not isinstance(indicators, dict) or not indicators:
        raise CatalogError("指标目录缺少 indicators 段（或为空）")

    for name, entry in indicators.items():
        if not isinstance(entry, dict):
            raise CatalogError(f"{name}: 条目不是 mapping")
        for key in _INDICATOR_KEYS:
            if key not in entry:
                raise CatalogError(f"{name}: 缺少必需键 {key!r}")
        if not isinstance(entry["aliases"], list):
            raise CatalogError(f"{name}: aliases 必须是列表")
        if not isinstance(entry["frequency"], list) or not entry["frequency"]:
            raise CatalogError(f"{name}: frequency 必须是非空列表")
        srcs = entry["sources"]
        if not isinstance(srcs, dict) or not srcs:
            raise CatalogError(f"{name}: sources 必须是非空 mapping")
        for source, spec in srcs.items():
            if source not in SOURCES:
                raise CatalogError(
                    f"{name}: 未知源 {source!r}（只支持 {', '.join(SOURCES)}）")
            if not isinstance(spec, dict):
                raise CatalogError(f"{name}.{source}: 不是 mapping")
            for key in REQUIRED_KEYS[source]:
                if key not in spec:
                    raise CatalogError(
                        f"{name}.{source}: 缺少必需键 {key!r}"
                        f"（{source} 需要 {', '.join(REQUIRED_KEYS[source])}）")
            # variants：同一源上的**另一种频率/口径**（例如 NBS CPI 年度 + 月度）。
            # 每个 variant 也必须带齐该源的必需键，否则后面 fetch 时才炸。
            variants = spec.get("variants")
            if variants is not None:
                if not isinstance(variants, list) or not variants:
                    raise CatalogError(f"{name}.{source}.variants 必须是非空列表")
                for i, var in enumerate(variants):
                    if not isinstance(var, dict):
                        raise CatalogError(f"{name}.{source}.variants[{i}]: 不是 mapping")
                    if "variant" not in var:
                        raise CatalogError(f"{name}.{source}.variants[{i}]: 缺少 variant 名")
                    for key in REQUIRED_KEYS[source]:
                        if key not in var:
                            raise CatalogError(
                                f"{name}.{source}.variants[{i}] ({var.get('variant')}): "
                                f"缺少必需键 {key!r}")

    obj["_alias_index"] = _build_alias_index(indicators)
    obj["_indicator_names"] = sorted(indicators)
    _CACHE[cache_key] = obj
    return obj


def _build_alias_index(indicators: dict[str, Any]) -> dict[str, str]:
    """构造 归一化键 -> 规范名 的反查表。

    可检索的键有三类：规范名（``CPI``）、**display_name**（``居民消费价格指数``）、
    以及 aliases。第三类容易想到，第二类最容易漏 —— 但中文用户打的就是 display_name，
    所以它必须可检索（第一次自检就是在这里失败的：`居民消费价格指数` 查不到）。

    :raises CatalogError: 同一别名指向两个指标（**不做"后来者覆盖"** —— 那会静默错答）。
    """
    index: dict[str, str] = {}
    for name, entry in indicators.items():
        keys = [name, entry.get("display_name"), *_as_list(entry.get("aliases"))]
        for raw in keys:
            if not str(raw or "").strip():
                continue
            key = _norm(str(raw))
            other = index.get(key)
            if other is not None and other != name:
                raise CatalogError(
                    f"别名冲突: {raw!r} 同时指向 {other!r} 与 {name!r}。"
                    "别名必须全局唯一，否则查询结果取决于字典顺序。")
            index[key] = name
    return index


def _as_list(value: Any) -> list[Any]:
    """把可能是 None / 标量 / 列表的字段统一成列表。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


# --------------------------------------------------------------------------- #
# 查询
# --------------------------------------------------------------------------- #

def resolve_name(name: str) -> str:
    """把任意写法解析成规范指标名。

    匹配顺序：① 精确规范名 -> ② 归一化别名 -> ③ **唯一的**子串命中。

    :raises CatalogError: 查不到；或子串命中多个（错误信息里列出候选，绝不猜）。
    """
    cat = load_catalog()
    indicators = cat["indicators"]
    raw = str(name or "").strip()
    if not raw:
        raise CatalogError("指标名不能为空")
    if raw in indicators:
        return raw

    key = _norm(raw)
    hit = cat["_alias_index"].get(key)
    if hit:
        return hit

    # 子串兜底：只在**唯一**命中时接受
    partial = [n for n in cat["_indicator_names"]
               if key in _norm(n)
               or key in _norm(indicators[n].get("display_name") or "")
               or any(key in _norm(a) for a in _as_list(indicators[n].get("aliases")))]
    if len(partial) == 1:
        return partial[0]
    if not partial:
        avail = ", ".join(cat["_indicator_names"][:12])
        raise CatalogError(
            f"目录里没有指标 {name!r}。可用指标（前 12 个）: {avail} …"
            f"（共 {len(indicators)} 个，跑 `edh list` 看全量）")
    raise CatalogError(
        f"{name!r} 命中多个指标，无法确定: {', '.join(sorted(partial))}。请写得更具体。")


def list_indicators() -> list[dict[str, Any]]:
    """返回全部指标（按规范名排序），每个元素含 display_name / aliases / sources 等。

    每个元素都带一个可直接展示的 `available_sources` 列表（顺序同 :data:`SOURCES`）。
    """
    cat = load_catalog()
    out: list[dict[str, Any]] = []
    for name in cat["_indicator_names"]:
        entry = cat["indicators"][name]
        out.append({
            "name": name,
            "display_name": entry["display_name"],
            "description": entry["description"],
            "aliases": list(_as_list(entry.get("aliases"))),
            "frequency": list(entry["frequency"]),
            "sources": entry["sources"],
            "available_sources": sources_for(name),
            "n_sources": len(entry["sources"]),
        })
    return out


def list_sources() -> list[str]:
    """返回支持的源（固定顺序）。"""
    return list(SOURCES)


def sources_for(name: str) -> list[str]:
    """该指标有哪些源能取到数（缺的源**不会**出现在结果里）。"""
    cat = load_catalog()
    entry = cat["indicators"][resolve_name(name)]
    return [s for s in SOURCES if s in entry["sources"]]


def get_indicator(name: str) -> dict[str, Any]:
    """取单个指标的完整信息；`name` 可以是规范名 / 别名 / 中文名。

    :raises CatalogError: 查不到或命中多个。
    """
    cat = load_catalog()
    canon = resolve_name(name)
    entry = cat["indicators"][canon]
    return {
        "name": canon,
        "display_name": entry["display_name"],
        "description": entry["description"],
        "aliases": list(_as_list(entry.get("aliases"))),
        "frequency": list(entry["frequency"]),
        "sources": entry["sources"],
        "available_sources": sources_for(canon),
        "n_sources": len(entry["sources"]),
        "resolved_from": str(name).strip(),
        "is_alias": str(name).strip() != canon,
        "notes": entry.get("notes"),
    }


def get_source_config(indicator: str, source: str) -> dict[str, Any]:
    """取某指标在某源上的取数参数。

    :returns: 该源的参数字典，外加 `source` / `indicator` 两个溯源字段。
    :raises CatalogError: 源名非法，或该源不提供这个指标（错误信息里给可用源）。
    """
    src = str(source or "").strip().lower()
    if src not in SOURCES:
        raise CatalogError(f"未知源 {source!r}（只支持 {', '.join(SOURCES)}）")
    entry = get_indicator(indicator)
    spec = entry["sources"].get(src)
    if spec is None:
        raise CatalogError(
            f"{entry['name']} 在 {src} 上没有映射。可用源: "
            f"{', '.join(entry['available_sources']) or '(无)'}")
    return {**spec, "source": src, "indicator": entry["name"]}


def catalog_summary() -> dict[str, Any]:
    """目录总览：指标数、源映射数、每个源覆盖多少指标。"""
    cat = load_catalog()
    per_source: dict[str, list[str]] = {s: [] for s in SOURCES}
    n_map = 0
    for name in cat["_indicator_names"]:
        for source in cat["indicators"][name]["sources"]:
            per_source[source].append(name)
            n_map += 1
    return {
        "n_indicators": len(cat["_indicator_names"]),
        "n_source_mappings": n_map,
        "per_source": {s: {"n": len(v), "indicators": v} for s, v in per_source.items()},
        "indicators": list(cat["_indicator_names"]),
        "path": str(CATALOG_PATH),
    }


# --------------------------------------------------------------------------- #
# 自检（纯离线：只读 YAML，不联网）
# --------------------------------------------------------------------------- #

def _selftest() -> int:
    failures: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            failures.append(msg)

    print("=" * 92)
    print("self-test: catalog（指标目录：加载 / 别名 / 查询 / 结构不变量）")
    print("=" * 92)

    # 1) 加载与结构
    cat = load_catalog()
    inds = list_indicators()
    summ = catalog_summary()
    print(f"[1] 加载 {CATALOG_PATH.name}: {summ['n_indicators']} 个指标、"
          f"{summ['n_source_mappings']} 条源映射")
    for s in SOURCES:
        print(f"      {s:<10} {summ['per_source'][s]['n']:>2} 个指标")
    check(summ["n_indicators"] == 18, f"1: 期望 18 个指标，实际 {summ['n_indicators']}")
    check(len(inds) == summ["n_indicators"], "1: list_indicators 与 summary 数量不一致")
    check([i["name"] for i in inds] == sorted(i["name"] for i in inds),
          "1: list_indicators 未按规范名排序")
    for i in inds:
        check(bool(i["display_name"]), f"1: {i['name']} 缺 display_name")
        check(bool(i["aliases"]), f"1: {i['name']} 无别名")
        check(i["n_sources"] == len(i["available_sources"]),
              f"1: {i['name']} n_sources 与 available_sources 不符")

    # 2) 规范名 / 别名 / 中文 三种写法都解析到同一条
    for probe, expect in (("CPI", "CPI"), ("cpi", "CPI"), ("居民消费价格指数", "CPI"),
                          ("consumer price index", "CPI")):
        got = get_indicator(probe)["name"]
        print(f"[2] get_indicator({probe!r}) -> {got}")
        check(got == expect, f"2: {probe!r} 期望 {expect}，实际 {got}")

    # 3) 下划线/空格/连字符写法等价
    for probe in ("gdp_per_capita", "GDP PER CAPITA", "gdp-per-capita"):
        got = get_indicator(probe)["name"]
        check(got == "GDP_PER_CAPITA", f"3: {probe!r} 期望 GDP_PER_CAPITA，实际 {got}")
    print("[3] GDP_PER_CAPITA 的三种分隔符写法均解析到规范名")

    # 4) 别名全局唯一（冲突会在加载时抛，这里做正向确认）
    seen: dict[str, str] = {}
    for i in inds:
        for alias in [i["name"], *i["aliases"]]:
            k = _norm(alias)
            check(k not in seen or seen[k] == i["name"],
                  f"4: 别名 {alias!r} 冲突（{seen.get(k)} vs {i['name']}）")
            seen[k] = i["name"]
    print(f"[4] 别名唯一性：{len(seen)} 个可检索键，无冲突")
    # 需求清单里 CPI 与 INFLATION 都想要 inflation，这里确认已拆开
    check(get_indicator("inflation")["name"] == "INFLATION",
          "4: 'inflation' 应解析到 INFLATION")
    check(get_indicator("通胀")["name"] == "INFLATION", "4: '通胀' 应解析到 INFLATION")

    # 5) get_source_config 正例
    cfg = get_source_config("GDP", "worldbank")
    print(f"[5] get_source_config('GDP','worldbank') -> {json.dumps(cfg, ensure_ascii=False)}")
    check(cfg["indicator_code"] == "NY.GDP.MKTP.CN",
          f"5: WB GDP 代码期望 NY.GDP.MKTP.CN，实际 {cfg.get('indicator_code')}")
    check(cfg["source"] == "worldbank" and cfg["indicator"] == "GDP", "5: 溯源字段缺失")
    cfg_nbs = get_source_config("GDP", "nbs")
    check(cfg_nbs["frequency"] == "annual", "5: NBS GDP 频率应为 annual")
    check(all(k in cfg_nbs for k in REQUIRED_KEYS["nbs"]),
          "5: NBS 配置缺必需键")

    # 6) 取不到的源必须**明确报错**，不能返回 None 或空 dict
    try:
        get_source_config("GOVERNMENT_DEBT", "nbs")
        failures.append("6: GOVERNMENT_DEBT 在 nbs 上应报错（NBS 无政府债务口径）")
    except CatalogError as exc:
        print(f"[6] get_source_config('GOVERNMENT_DEBT','nbs') 正确报错: {str(exc)[:70]}…")
    try:
        get_source_config("GDP", "oecd")
        failures.append("6: 未知源应报错")
    except CatalogError:
        print("[6] 未知源 'oecd' 正确报错")

    # 7) 查不到的指标必须报错并给候选
    try:
        get_indicator("不存在的指标")
        failures.append("7: 不存在的指标应报错")
    except CatalogError as exc:
        check("可用指标" in str(exc), "7: 报错信息应列出可用指标")
        print(f"[7] 查不到时正确报错并给候选: {str(exc)[:70]}…")

    # 8) 每个源的必需键都在（结构不变量，防止 YAML 手改漏字段）
    n_checked = 0
    n_variants = 0
    for i in inds:
        for source, spec in i["sources"].items():
            for key in REQUIRED_KEYS[source]:
                check(key in spec, f"8: {i['name']}.{source} 缺 {key}")
                n_checked += 1
            for var in spec.get("variants") or []:
                n_variants += 1
                for key in REQUIRED_KEYS[source]:
                    check(key in var, f"8: {i['name']}.{source}.{var.get('variant')} 缺 {key}")
                    n_checked += 1
    print(f"[8] 必需键检查：{n_checked} 个键全部存在（含 {n_variants} 个 variant）")

    # 9) sources_for 与 sources 的键集合一致，且顺序稳定
    for i in inds:
        check(i["available_sources"] == [s for s in SOURCES if s in i["sources"]],
              f"9: {i['name']} available_sources 顺序不对")
    print("[9] available_sources 顺序稳定（同 SOURCES）")

    # 10) 计数不变量：18 个指标 / 51 条源映射（与 meta 段自述一致）
    check(summ["n_source_mappings"] == 51,
          f"10: 期望 51 条源映射，实际 {summ['n_source_mappings']}")
    meta = cat.get("meta") or {}
    check(meta.get("n_indicators") == summ["n_indicators"],
          "10: meta.n_indicators 与实际不符")
    check(meta.get("n_source_mappings") == summ["n_source_mappings"],
          "10: meta.n_source_mappings 与实际不符")
    print(f"[10] 计数自洽：{summ['n_indicators']} 指标 / {summ['n_source_mappings']} 源映射 / "
          f"{n_variants} variant，与 meta 段一致")

    # 11) variant 是「同源另一种频率/口径」，必须真的能取出来
    cpi_nbs_vars = [v["variant"] for v in
                    get_source_config("CPI", "nbs").get("variants") or []]
    check("monthly" in cpi_nbs_vars, f"11: CPI nbs 应有 monthly variant，实际 {cpi_nbs_vars}")
    unemp_nbs_vars = [v["variant"] for v in
                      get_source_config("UNEMPLOYMENT", "nbs").get("variants") or []]
    check("registered" in unemp_nbs_vars,
          f"11: UNEMPLOYMENT nbs 应有 registered variant，实际 {unemp_nbs_vars}")
    check(get_source_config("FDI", "nbs").get("data_until") == "2019-11",
          "11: FDI nbs 应记 data_until=2019-11（实测停更期）")
    print("[11] variant / data_until 标注就位（CPI monthly、失业率 registered、FDI 停更期）")

    print("-" * 92)
    if failures:
        print(f"FAIL: {len(failures)} 项")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS: catalog 自检全部通过")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    parser = argparse.ArgumentParser(
        prog="econ_core.catalog",
        description="指标目录：加载 catalog_data.yaml 并提供查询接口（纯离线）。")
    parser.add_argument("--test", action="store_true", help="跑自检（不联网）")
    parser.add_argument("--summary", action="store_true", help="打印目录总览 JSON")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.summary:
        print(json.dumps(catalog_summary(), ensure_ascii=False, indent=2))
        return 0
    if args.test:
        return _selftest()
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
