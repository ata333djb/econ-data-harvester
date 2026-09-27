#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""国家统计局新版 SPA (DSF 平台) bundle 逆向扫描。

目标：从 /dg/website/page.html 加载的 JS bundle 中提取 API 端点常量，
      找出数据发布接口的真实 base path。

用法：
    .\\.venv\\Scripts\\python.exe python\\probe_nbs_bundles.py
"""
import json, re, sys, os
from pathlib import Path

PROJ = Path(__file__).resolve().parents[2]   # <repo>/python/_probes/x.py -> <repo>
sys.path.insert(0, str(PROJ / "python"))
from econ_core.http_client import get_text, CACHE_DIR

BASE = "https://data.stats.gov.cn"
PAGE = f"{BASE}/dg/website/page.html"
ROOT = f"{BASE}/dg/website/"

BUNDLES = [
    "common/config_project.js",
    "common/global_project.js",
    "modules/core/js/dsf-core.js",
    "modules/platform/js/dsf-platform.common.js",
    "modules/platform/js/dsf-platform.pc.js",
    "static/js/project/dsf-pc.js",
    "static/js/project/subPage.js",
    "static/js/project/dsf-vendors.js",
    "modules/dataview/js/dsf-dataview.common.js",
    "modules/dataview/js/dsf-dataview.pc.js",
    "static/js/libs/index.js",
]

# 关注的关键词
KEYWORDS = ["publicrelease", "easyquery", "external", "baseURL", "apiUrl", "API_URL",
            "requestUrl", "serviceUrl", "queryData", "hgnd", "tree" ]

# 提取疑似端点字面量
ENDPOINT_RE = re.compile(
    r"""["'`]((?:https?://[^"'`\s]+|/[A-Za-z0-9_\-./{}]{3,120}))["'`]"""
)
ENDISH = re.compile(r"(publicrelease|external|easyquery|/dg/|/api|queryData|getChildren|tree)", re.I)

report = {"page": PAGE, "bundles": [], "hits": {}, "endpoints": []}
pages = {}
print("=" * 78)
print("STEP A: 下载 bundle")
print("=" * 78)
for rel in BUNDLES:
    url = ROOT + rel
    try:
        r = get_text(url, referer=PAGE, save=True)
    except Exception as e:
        print(f"  [FAIL] {rel}: {type(e).__name__}: {e}")
        report["bundles"].append({"rel": rel, "error": str(e)})
        continue
    pages[rel] = r.content
    print(f"  {r.status}  {len(r.content):>9,} chars  {rel}")
    report["bundles"].append({"rel": rel, "status": r.status, "chars": len(r.content),
                              "cache": r.cache_path.name if r.cache_path else None})

print()
print("=" * 78)
print("STEP B: 关键词命中")
print("=" * 78)
for kw in KEYWORDS:
    hits = []
    for rel, txt in pages.items():
        for m in re.finditer(re.escape(kw), txt):
            s = max(0, m.start() - 90)
            frag = txt[s:m.start() + 120].replace("\n", " ")
            hits.append((rel, frag))
    # 去重
    seen, uniq = set(), []
    for rel, frag in hits:
        if frag not in seen:
            seen.add(frag)
            uniq.append((rel, frag))
    report["hits"][kw] = [{"bundle": r, "context": f} for r, f in uniq[:8]]
    print(f"\n--- keyword: {kw!r}  -> {len(hits)} 命中, {len(uniq)} 去重片段 ---")
    for rel, frag in uniq[:8]:
        print(f"  [{rel}]")
        print(f"     ...{frag}...")

print()
print("=" * 78)
print("STEP C: 疑似端点字面量（含外部/api 特征）")
print("=" * 78)
seen = set()
for rel, txt in pages.items():
    for m in ENDPOINT_RE.finditer(txt):
        v = m.group(1)
        if ENDISH.search(v) and v not in seen:
            seen.add(v)
            report["endpoints"].append({"bundle": rel, "endpoint": v})
            print(f"  [{rel}]  {v}")

out = PROJ / "data/parsed/nbs_bundle_scan.json"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print()
print(f"[报告已存 -> {out}]  共 {len(report['endpoints'])} 个候选端点")
