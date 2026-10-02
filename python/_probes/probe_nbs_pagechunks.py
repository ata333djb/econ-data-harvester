import sys, re, json
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]  # 项目根：从本文件位置推导，不写死机器路径
sys.path.insert(0, str(ROOT / "python"))
from econ_core.http_client import get_text

BASE = "https://data.stats.gov.cn"
PAGE = f"{BASE}/dg/website/page.html"
CANDS = ["project_national_datapage","project_national_home","DataPage","ReportPage",
         "SearchPage","MapPage","project_national_page","DataViewPage","dataPage"]

hits = {}
print("=== 逐个取页面 chunk 并提取端点 ===")
for name in CANDS:
    url = f"{BASE}/dg/website/static/js/project/{name}.js"
    try:
        r = get_text(url, referer=PAGE, save=True)
    except Exception as e:
        print(f"  FAIL {name}: {e}"); continue
    if r.status != 200 or "页面不存在" in r.content:
        print(f"  {r.status}  miss  {name}")
        continue
    b = r.content
    eps  = sorted(set(re.findall(r"""["'`](/[A-Za-z0-9_\-./]{3,140})["'`]""", b)))
    eps  = [e for e in eps if re.search(r"(publicrelease|query|tree|data|indic|external|easyquery|search|catalog|report)", e, re.I)]
    calls = sorted(set(re.findall(r"""\$http\.(?:get|post|put|delete)\(\s*["'`]([^"'`]{2,180})["'`]""", b)))
    print(f"\n  [200] {name}.js  {len(b):,} chars  cache={r.cache_path.name}")
    for c in calls: print(f"      CALL  {c}")
    for e in eps[:40]: print(f"      EP    {e}")
    hits[name] = {"chars": len(b), "calls": calls, "endpoints": eps,
                  "cache": r.cache_path.name if r.cache_path else None}

print()
print("=== 回测后端（XHR 头） ===")
XHR = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json, text/plain, */*"}
tested = []
for name, info in hits.items():
    for c in info["calls"]:
        path = c.split("?")[0]
        if not path.startswith("/") or path in tested: continue
        tested.append(path)
        u = BASE + path
        try:
            r = get_text(u, referer=PAGE, headers=XHR, save=True)
            is404 = '"status":404' in r.content
            tag = "SPA-404" if "页面不存在" in r.content else ("SPRING-404" if is404 else "RESPONSE")
            print(f"  {r.status}  {tag:<12} {len(r.content):>6}B  {path}")
            if not is404 and "页面不存在" not in r.content:
                print(f"        >>> {r.content[:400]}")
        except Exception as e:
            print(f"  FAIL {path}: {type(e).__name__}: {e}")

Path("data/parsed/nbs_pagechunks_scan.json").write_text(
    json.dumps({"hits": hits, "tested": tested}, ensure_ascii=False, indent=2), encoding="utf-8")
print("\n[报告 -> data/parsed/nbs_pagechunks_scan.json]")
