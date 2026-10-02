import gzip, re
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]  # 项目根：从本文件位置推导，不写死机器路径
CACHE = ROOT / "data" / "raw" / "_http_cache"
def load(name):
    raw = (CACHE / f"{name}.bin").read_bytes()
    return (gzip.decompress(raw) if raw[:2]==b"\x1f\x8b" else raw).decode("utf-8","replace")
t = load("0aa788b698ee8e07")          # DataPage.js
sh = load("1a7381fbec3cc20f")         # 共享 chunk: DataPage~MapPage~...
print(f"DataPage.js={len(t):,}  shared_chunk={len(sh):,}\n")

def win(txt, key, before, after, label, occurrence=0):
    pos = [m.start() for m in re.finditer(re.escape(key), txt)]
    if not pos: print(f"### {label}\n  (无命中)\n"); return
    s = pos[occurrence]
    print(f"### {label}  @offset {s}")
    print(txt[max(0,s-before):s+after])
    print()

print("="*78); print("A. POST payload 完整对象字面量"); print("="*78)
win(t, "isRemoveEmptyRowAndCol", 1500, 400, "site-1 (120526 附近)", 0)
win(t, "isRemoveEmptyRowAndCol", 1500, 400, "site-2 (186770 附近)", 1)

print("="*78); print("B. returnCode 定义"); print("="*78)
win(t, "returnCode:function", 100, 500, "returnCode")

print("="*78); print("C. datePage 取值域（year/quarter/month pages）"); print("="*78)
for key in ["yearPages:", "quarterPages:", "monthPages:", "areaPages:"]:
    win(t, key, 60, 320, key)

print("="*78); print("D. targetList 来源"); print("="*78)
for m in list(re.finditer(r"targetList\s*=", t))[:3]:
    s = m.start(); print(f"  @{s}: ...{t[max(0,s-500):s+400]}...\n")

print("="*78); print("E. area / defaultArea / dateNum 初始化"); print("="*78)
for m in list(re.finditer(r"defaultArea\s*[:=]", t))[:3]:
    s = m.start(); print(f"  @{s}: ...{t[max(0,s-300):s+300]}...\n")

print("="*78); print("F. 共享 chunk 里的树加载（getCatalogsAndIndexTree / queryIndicatorsByCid）"); print("="*78)
for key in ["getCatalogsAndIndexTree", "queryIndicatorsByCid", "treeinfo_globalid"]:
    win(sh, key, 600, 600, f"[shared] {key}")
