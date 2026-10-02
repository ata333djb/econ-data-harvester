import gzip, re, json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]  # 项目根：从本文件位置推导，不写死机器路径
CACHE = ROOT / "data" / "raw" / "_http_cache"
sys.path.insert(0, str(ROOT / "python"))
from econ_core.http_client import get_text

def load(d):
    raw = (CACHE / f"{d}.bin").read_bytes()
    return (gzip.decompress(raw) if raw[:2]==b"\x1f\x8b" else raw).decode("utf-8","replace")

def show(txt, key, before, after, label, occ=0):
    pos = [m.start() for m in re.finditer(re.escape(key), txt)]
    print(f"\n### {label}  (@{pos[occ] if pos else 'none'})")
    if not pos: print("   (无命中)"); return
    s = pos[occ]; print(txt[max(0,s-before):s+after])

T   = load("0aa788b698ee8e07")   # DataPage.js
SP  = load("731e751d92117fb3")   # DataPage~ReportPage~SearchPage.js
MP  = load("b0fba8c81ec41316")   # MapPage.js

print("="*78); print("A. returnCode 定义"); print("="*78)
show(SP, "returnCode:function", 80, 700, "returnCode [shared chunk]")
show(MP, "returnCode:function", 80, 700, "returnCode [MapPage]")

print(); print("="*78); print("B. getTargetList 定义（指标从哪来）"); print("="*78)
show(T, "getTargetList:function", 150, 1600, "getTargetList")
show(T, "queryIndicatorsByCid", 900, 500, "queryIndicatorsByCid 调用点")
show(T, "getTargetListParams:", 200, 400, "getTargetListParams 数据属性")

print(); print("="*78); print("C. getCatalogsAndIndexTree 用法（MapPage）"); print("="*78)
show(MP, "getCatalogsAndIndexTree", 700, 500, "getCatalogsAndIndexTree")

print(); print("="*78); print("D. indicatorIds 结构（DataPage）"); print("="*78)
show(T, "indicatorIds", 700, 500, "indicatorIds @116850", 0)
