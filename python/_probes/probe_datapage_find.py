import gzip, re, json
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]  # 项目根：从本文件位置推导，不写死机器路径
CACHE = ROOT / "data" / "raw" / "_http_cache"

# 建立 digest -> url 索引
urls = {}
for mp in CACHE.glob("*.meta.json"):
    try: urls[mp.stem.replace(".meta","")] = json.loads(mp.read_text(encoding="utf-8"))["url"]
    except Exception: pass

def load(d):
    raw = (CACHE / f"{d}.bin").read_bytes()
    return (gzip.decompress(raw) if raw[:2]==b"\x1f\x8b" else raw).decode("utf-8","replace")

DP = "0aa788b698ee8e07"
t = load(DP)

print("="*78); print("A. POST payload（精确锚点 targets:this.targetList）"); print("="*78)
for i, m in enumerate(re.finditer(r"targets:this\.targetList", t)):
    s = m.start()
    print(f"\n--- payload #{i+1} @{s} ---")
    print(t[max(0,s-420):s+520])

print()
print("="*78); print("B. 跨全部 chunk 搜索关键定义"); print("="*78)
PATS = ["returnCode:function", "returnCode =function", "getTargetList:function",
        "getTargetListParams:", "getCatalogsAndIndexTree", "queryIndicatorsByCid",
        "indicators:", "indicatorIds"]
for d, url in sorted(urls.items()):
    try: txt = load(d)
    except Exception: continue
    hits = {p: [m.start() for m in re.finditer(re.escape(p), txt)] for p in PATS}
    hits = {k:v for k,v in hits.items() if v}
    if hits:
        print(f"\n  [{(CACHE/f'{d}.bin').name}]  {url.split('/')[-1]}  ({len(txt):,} chars)")
        for k,v in hits.items():
            print(f"      {k}: {len(v)} 次 @ {v[:6]}")
