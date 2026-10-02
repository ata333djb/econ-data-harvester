#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从已存档 bundle 中枚举端点，并对修正后的 base 做实地连通性验证。

Part 1 完全离线：直接读 data/raw/_http_cache/*.bin（gzip 解压），不发网络请求。
Part 2 联网：测试 /publicrelease/web/external/* 端点。
"""
import gzip, hashlib, json, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # 项目根：从本文件位置推导，不写死机器路径
sys.path.insert(0, str(ROOT / "python"))
from econ_core.http_client import get_text, get_json, CACHE_DIR, cache_path_for

BASE = "https://data.stats.gov.cn"
PAGE = f"{BASE}/dg/website/page.html"
CACHE = CACHE_DIR

def cached(url: str) -> str | None:
    p = cache_path_for(url, "GET")[0]
    if not p.exists():   # 兼容缓存键修复前（sha256(url)[:16]）的旧存档命名
        p = CACHE / f"{hashlib.sha256(url.encode()).hexdigest()[:16]}.bin"
    if not p.exists():
        return None
    raw = p.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return raw.decode("utf-8", errors="replace")

print("=" * 78); print("PART 1: 离线枚举端点（读 raw 存档，零网络请求）"); print("=" * 78)
BUNDLES = ["static/js/project/dsf-pc.js", "static/js/project/subPage.js",
           "common/config_project.js", "common/global_project.js",
           "modules/core/js/dsf-core.js",
           "modules/platform/js/dsf-platform.common.js",
           "modules/platform/js/dsf-platform.pc.js",
           "modules/dataview/js/dsf-dataview.common.js",
           "modules/dataview/js/dsf-dataview.pc.js",
           "static/js/libs/index.js", "static/js/project/dsf-vendors.js"]

CALL_RE = re.compile(r"""\$http\.(get|post|put|delete|request)\(\s*["'`]([^"'`]{2,160})["'`]""")
REL_RE  = re.compile(r"""["'`](/?(?:publicrelease|datascreen)[A-Za-z0-9_\-./?=&{}]*?)["'`]""")

found = {}
for rel in BUNDLES:
    txt = cached(f"{BASE}/dg/website/{rel}")
    if txt is None:
        print(f"  [miss cache] {rel}"); continue
    for m in CALL_RE.finditer(txt):
        found.setdefault(m.group(2), set()).add(f"{rel} ($http.{m.group(1)})")
    for m in REL_RE.finditer(txt):
        ep = m.group(1)
        if "app/" not in ep:
            found.setdefault(ep, set()).add(rel)

for ep in sorted(found):
    srcs = sorted(found[ep])
    print(f"  {ep}")
    for s in srcs[:3]:
        print(f"       <- {s}")
print(f"\n  共 {len(found)} 个候选端点/路径")

print()
print("=" * 78); print("PART 2: 修正 base 后的实地验证"); print("=" * 78)
TESTS = [
    ("A 外部接口根", f"{BASE}/publicrelease/web/external"),
    ("B tree/getChildren?pid=root", f"{BASE}/publicrelease/web/external/tree/getChildren?pid=root"),
    ("C tree/root", f"{BASE}/publicrelease/web/external/tree/root"),
    ("D queryCMSArticles", f"{BASE}/publicrelease/web/external/new/queryCMSArticles?code=zxfb&pagenum=1&pageSize=10"),
    ("E queryAgendaByDate", f"{BASE}/publicrelease/web/external/queryAgendaByDate?startDate=2026-09-27&endDate=2026-09-28"),
    ("F 对照: 旧前缀 /dg/website/publicrelease/...", f"{BASE}/dg/website/publicrelease/web/external/tree/root"),
]
results = []
for label, url in TESTS:
    try:
        r = get_text(url, referer=PAGE, save=True)
        ctype = r.headers.get("Content-Type", "")
        body = r.content
        print(f"\n[{label}] {r.status}  {len(body)} chars  {ctype}")
        print(f"    {url}")
        print(f"    cache: {r.cache_path.name if r.cache_path else None}")
        print("    body[:400]: " + body[:400].replace("\n", " "))
        results.append({"label": label, "url": url, "status": r.status,
                        "chars": len(body), "ctype": ctype,
                        "cache": r.cache_path.name if r.cache_path else None,
                        "body_head": body[:600]})
    except Exception as e:
        print(f"\n[{label}] FAIL {type(e).__name__}: {e}")
        results.append({"label": label, "url": url, "error": str(e)})

out = ROOT / "data/parsed/nbs_endpoint_probe.json"
out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"\n[报告 -> {out}]")
