import gzip, hashlib, json, re, sys
from pathlib import Path
ROOT = Path(r"D:\universe\econ-data-harvester")
CACHE = ROOT / "data" / "raw" / "_http_cache"
sys.path.insert(0, str(ROOT / "python"))
from econ_core.http_client import get_text

TARGET = "DataPage.js"

# ---------- 1. 定位缓存文件 ----------
print("=" * 78)
print("STEP 1: 定位 DataPage.js 缓存")
print("=" * 78)
hit = None
for meta_path in sorted(CACHE.glob("*.meta.json")):
    try:
        m = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        continue
    url = m.get("url", "")
    if url.endswith("/" + TARGET):
        hit = (meta_path, m, url)
        break
if not hit:
    print("  !! 未找到"); sys.exit(1)
meta_path, meta, url = hit
bin_path = CACHE / (meta_path.stem.replace(".meta", "") + ".bin")
raw = bin_path.read_bytes()
txt = (gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw).decode("utf-8", "replace")
last = meta["last_fetch"]
print(f"  URL          : {url}")
print(f"  存档文件     : {bin_path}")
print(f"  元数据       : {meta_path}")
print(f"  网线字节     : {len(raw):,} B   (gzip={raw[:2]==b'\x1f\x8b'})")
print(f"  解压后字符   : {len(txt):,}")
print(f"  sha256(网线) : {last['sha256']}")
print(f"  sha256(解压) : {hashlib.sha256(txt.encode()).hexdigest()}")
print(f"  HTTP 状态    : {last['status']}   ctype={last['headers'].get('Content-Type')}")
print(f"  抓取时间(UTC): {last['fetched_at_utc']}")

# 检查是否有 sourcemap（有的话可直接拿原始源码）
print("\n  --- 尝试 sourcemap ---")
for sm in [f"{url}.map", url.replace(".js", ".js.map")]:
    try:
        r = get_text(sm, referer="https://data.stats.gov.cn/dg/website/page.html", save=True)
        kind = "SPA-404" if "页面不存在" in r.content else ("SPRING-404" if '"status":404' in r.content else "REAL")
        print(f"    {r.status}  {kind:<10} {len(r.content):>8,} chars  {sm}")
        if kind == "REAL":
            Path(ROOT / "data/raw/nbs_sources").mkdir(parents=True, exist_ok=True)
            p = ROOT / "data/raw/nbs_sources" / "DataPage.js.map"
            p.write_text(r.content, encoding="utf-8")
            print(f"    [sourcemap 已存 -> {p}]")
    except Exception as e:
        print(f"    FAIL {sm}: {e}")

# ---------- 2. 关键词上下文 ----------
print()
print("=" * 78)
print("STEP 2: 关键词上下文（前后各 200 字符）")
print("=" * 78)
KEYWORDS = ["getEsDataByIndicatorIdAndDa", "getDefaultIndicData", "queryMacroecData",
            "getDaCatalogTreeByIndicatorCid", "getCatalogsAndIndexTree"]
for kw in KEYWORDS:
    pos = [m.start() for m in re.finditer(re.escape(kw), txt)]
    print(f"\n{'#'*78}\n### {kw}  —— 命中 {len(pos)} 次 @ {pos}\n{'#'*78}")
    for i, s in enumerate(pos):
        lo, hi = max(0, s - 200), min(len(txt), s + 200)
        print(f"\n  --- hit #{i+1} (offset {s}) ---")
        print(f"  [{max(0,200-s)} before] ...{txt[lo:s]}<<<{kw}>>>{txt[s+len(kw):hi]}...")
