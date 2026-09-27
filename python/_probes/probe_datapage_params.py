import gzip, re, json
from pathlib import Path
ROOT = Path(r"D:\universe\econ-data-harvester")
CACHE = ROOT / "data" / "raw" / "_http_cache"
p = CACHE / "0aa788b698ee8e07.bin"
raw = p.read_bytes()
txt = (gzip.decompress(raw) if raw[:2]==b"\x1f\x8b" else raw).decode("utf-8","replace")
print(f"DataPage.js 解压长度 {len(txt):,}\n")

# ---- 1. 定位包含 POST 调用的函数体 ----
POST = "getEsDataByIndicatorIdAndDa"
s = txt.index(POST)
# 向前找最近的函数定义起点
start = txt.rfind("function", 0, s)
back = max(0, s - 2600)
print("="*78); print("A. POST 调用点上游 2600 字符（找入参 e 的来源）"); print("="*78)
print(txt[back:s+700])

# ---- 2. 该方法的调用者 ----
print(); print("="*78); print("B. 方法名与调用者"); print("="*78)
seg = txt[max(0,s-4000):s+200]
names = re.findall(r"([A-Za-z_$][\w$]{2,40})\s*:\s*function\s*\(\s*([A-Za-z_$][\w$]*)\s*\)", seg)
print("  就近的 method 定义（倒序）:")
for n, arg in reversed(names[-8:]):
    print(f"    {n}({arg})")
for n,_ in names[-4:]:
    callers = [m.start() for m in re.finditer(r"\.\s*"+re.escape(n)+r"\s*\(", txt)]
    print(f"  调用 .{n}() 的位置: {callers[:10]}")

# ---- 3. 参数名线索 ----
print(); print("="*78); print("C. 参数名 / 数据结构线索"); print("="*78)
for pat in [r"indicators", r"['\"]da['\"]", r"daCid", r"dbcode", r"returnCode", r"datePage"]:
    pos = [m.start() for m in re.finditer(pat, txt)]
    print(f"\n  --- {pat}  命中 {len(pos)} 次 @ {pos[:12]}")
    for s2 in pos[:3]:
        lo, hi = max(0,s2-160), min(len(txt), s2+200)
        print(f"     ...{txt[lo:hi]}...".replace("\n"," "))
