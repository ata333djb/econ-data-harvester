import gzip, re, json, sys
from pathlib import Path
ROOT = Path(r"D:\universe\econ-data-harvester")
CACHE = ROOT / "data" / "raw" / "_http_cache"
sys.path.insert(0, str(ROOT / "python"))
from econ_core.http_client import get_text

def load(d):
    raw = (CACHE / f"{d}.bin").read_bytes()
    return (gzip.decompress(raw) if raw[:2]==b"\x1f\x8b" else raw).decode("utf-8","replace")

SP = load("731e751d92117fb3")
print("="*78); print("A. tabsCode 全表"); print("="*78)
i = SP.index("tabsCode")
seg = SP[max(0,i-2000):i+200]
for m in re.finditer(r'\{\s*label:"([^"]*)",\s*name:"([^"]*)",\s*code:"([^"]*)"\s*\}', seg):
    print(f"  code={m.group(3):>3}   name={m.group(2):<22} label={m.group(1)}")

API  = "https://data.stats.gov.cn/dg/website/publicrelease/web/external"
PAGE = "https://data.stats.gov.cn/dg/website/page.html"
H = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json, text/plain, */*"}

print(); print("="*78); print("B. 扫描 getCatalogsAndIndexTree?code=N"); print("="*78)
good = []
for code in range(1, 16):
    u = f"{API}/getCatalogsAndIndexTree?code={code}"
    r = get_text(u, referer=PAGE, headers=H, save=True)
    b = r.content
    if b.lstrip().startswith("{"):
        try:
            j = json.loads(b)
        except Exception:
            print(f"  code={code:>3}  {r.status}  JSON 解析失败"); continue
        data = j.get("data")
        n = len(data) if isinstance(data, list) else "?"
        print(f"  code={code:>3}  {r.status}  success={j.get('success')}  nodes={n}")
        if n: good.append((code, j, r.cache_path.name))
    else:
        print(f"  code={code:>3}  {r.status}  HTML/其他 ({len(b)}B)")

print(); print("="*78); print("C. 有效 code 的树结构"); print("="*78)
for code, j, cache in good[:2]:
    data = j["data"]
    print(f"\n--- code={code}  cache={cache}  顶层 {len(data)} 个节点 ---")
    print(json.dumps(data[:2], ensure_ascii=False, indent=2)[:2000])
