import gzip, re, sys, hashlib
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(r"D:\universe\econ-data-harvester")
CACHE = ROOT / "data" / "raw" / "_http_cache"
raw = (CACHE/"0aa788b698ee8e07.bin").read_bytes()
T = (gzip.decompress(raw) if raw[:2]==b"\x1f\x8b" else raw).decode("utf-8","replace")

print("="*78); print("A. integrationChartRequest 的调用者（da 从哪来）"); print("="*78)
for m in list(re.finditer(r"integrationChartRequest\s*\(", T)):
    s = m.start()
    print(f"\n--- @{s} ---")
    print(T[max(0,s-700):s+300])

print(); print("="*78); print("B. getDasByDaCatalogId 调用点"); print("="*78)
for i, m in enumerate(list(re.finditer(r"getDasByDaCatalogId", T))[:3]):
    s = m.start()
    print(f"\n--- hit#{i+1} @{s} ---")
    print(T[max(0,s-800):s+400])

print(); print("="*78); print("C. 缓存键隐患验证：不同 POST body 是否覆盖同一文件"); print("="*78)
for d in ["61724f350a067b2d"]:
    meta = (CACHE/f"{d}.meta.json")
    import json
    j = json.loads(meta.read_text(encoding="utf-8"))
    print(f"  {d}.bin  fetch_count={j.get('fetch_count')}  最近几次:")
    for f in j.get("fetches", [])[-4:]:
        print(f"     status={f['status']} bytes={f['bytes']} sha256[:12]={f['sha256'][:12]} at={f['fetched_at_utc']}")
    print(f"  -> 同一 URL 的 {j.get('fetch_count')} 次不同 body 请求，全部落到同一个 .bin")
