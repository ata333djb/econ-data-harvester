import sys, re, gzip, hashlib, json
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]  # 项目根：从本文件位置推导，不写死机器路径
sys.path.insert(0, str(ROOT / "python"))
from econ_core.http_client import CACHE_DIR, get_text, cache_path_for

BASE = "https://data.stats.gov.cn"
PAGE = f"{BASE}/dg/website/page.html"
def cached(rel):
    _u = BASE + '/dg/website/' + rel
    p = cache_path_for(_u, "GET")[0]
    if not p.exists():   # 兼容缓存键修复前（sha256(url)[:16]）的旧存档命名
        p = CACHE_DIR / f"{hashlib.sha256(_u.encode()).hexdigest()[:16]}.bin"
    if not p.exists(): return None
    raw = p.read_bytes()
    return (gzip.decompress(raw) if raw[:2]==b"\x1f\x8b" else raw).decode("utf-8","replace")

print("=== 1. 从 subPage.js 提取 chunk 名映射表 ===")
t = cached("static/js/project/subPage.js")
i = t.find('l.p+"static/js/project/"')
seg = t[i:i+6000]
pairs = re.findall(r'"([^"]{2,300})"\s*:\s*"([^"]{0,300})"', seg)
names = []
for k, v in pairs:
    val = v if v else k
    if val not in names:
        names.append(val)
print(f"  提取到 {len(names)} 个 chunk 名：")
for n in names:
    print("   -", n)

print()
print("=== 2. 下载这些 chunk 并搜索 API 端点 ===")
found = {}
for n in names:
    for cand in [f"static/js/project/{n}.js", f"static/js/project/{n}.js?_t=1775124916746"]:
        url = f"{BASE}/dg/website/{cand.split('?')[0]}"
        try:
            r = get_text(url + ("?"+cand.split("?")[1] if "?" in cand else ""), referer=PAGE, save=True)
        except Exception as e:
            print(f"  FAIL {n}: {e}"); break
        if "页面不存在" in r.content or r.status != 200:
            continue
        body = r.content
        eps = sorted(set(re.findall(r"""["'`](/?(?:publicrelease|datascreen|api)[A-Za-z0-9_\-./?=&{}]{2,140})["'`]""", body)))
        calls = sorted(set(re.findall(r"""\$http\.(?:get|post|put|delete)\(\s*["'`]([^"'`]{2,160})["'`]""", body)))
        print(f"\n  [{r.status}] {cand}  {len(body):,} chars  cache={r.cache_path.name if r.cache_path else None}")
        for e in eps:  print(f"        EP  {e}")
        for c in calls: print(f"        CALL {c}")
        if eps or calls:
            found[n] = {"chunk": cand, "endpoints": eps, "calls": calls,
                        "cache": r.cache_path.name if r.cache_path else None}
        break

print()
print("=== 3. 用真实 chunk 里的端点回测后端 ===")
XHR = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json, text/plain, */*"}
tested = set()
for n, info in found.items():
    for ep in info["endpoints"]:
        if "app/" in ep or ep in tested or ep.startswith("/api/"): continue
        tested.add(ep)
        u = BASE + (ep if ep.startswith("/") else "/"+ep)
        try:
            r = get_text(u, referer=PAGE, headers=XHR, save=True)
            tag = "SPRING-JSON" if r.content.startswith("{") else ("SPA-404" if "页面不存在" in r.content else "OTHER")
            print(f"  {r.status}  {tag:<12} {len(r.content):>6}B  {ep}")
            if r.status == 200 or (r.content.startswith("{") and '"status":404' not in r.content):
                print(f"        >>> {r.content[:300]}")
        except Exception as e:
            print(f"  FAIL {ep}: {type(e).__name__}: {e}")

Path("data/parsed/nbs_chunks_scan.json").write_text(
    json.dumps({"chunks": names, "found": found, "tested": sorted(tested)},
               ensure_ascii=False, indent=2), encoding="utf-8")
print("\n[报告 -> data/parsed/nbs_chunks_scan.json]")
