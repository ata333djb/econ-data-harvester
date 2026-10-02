import gzip, re, json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]  # 项目根：从本文件位置推导，不写死机器路径
CACHE = ROOT / "data" / "raw" / "_http_cache"
sys.path.insert(0, str(ROOT / "python"))
from econ_core.http_client import get_text, get_json

API  = "https://data.stats.gov.cn/dg/website/publicrelease/web/external"
PAGE = "https://data.stats.gov.cn/dg/website/page.html"
H = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json, text/plain, */*"}
HJ = dict(H); HJ["Content-Type"] = "application/json;charset=UTF-8"

def show(tag, r, n=500):
    b = r.content if isinstance(r.content, str) else r.content.decode("utf-8","replace")
    print(f"\n  [{r.status}] {tag}")
    print(f"      url: {r.url}")
    print(f"      cache: {r.cache_path.name if r.cache_path else None}")
    print(f"      body[:{n}]: {b[:n]}")

# --- 先看 getChartData 的调用点，确认 POST body 怎么拼 ---
raw = (CACHE/"0aa788b698ee8e07.bin").read_bytes()
T = (gzip.decompress(raw) if raw[:2]==b"\x1f\x8b" else raw).decode("utf-8","replace")
print("="*78); print("A. getChartData 调用点 @170945"); print("="*78)
print(T[170700:171400])

# --- 1. 取年度树，找 GDP 指标节点 ---
print(); print("="*78); print("B. 年度树里定位 GDP 指标"); print("="*78)
r = get_text(f"{API}/getCatalogsAndIndexTree?code=3", referer=PAGE, headers=H, save=True)
tree = json.loads(r.content)["data"]
flat = []
def walk(nodes, path):
    for nd in nodes:
        p = path + [nd.get("othername") or nd.get("name")]
        if nd.get("type") == "indicator":
            flat.append((nd, p))
        walk(nd.get("children") or [], p)
walk(tree, [])
print(f"  指标叶子共 {len(flat)} 个")
gdp = [(nd,p) for nd,p in flat if "国内生产总值" in (nd.get("name") or "") and "指数" not in (nd.get("name") or "")]
for nd, p in gdp[:8]:
    print(f"   _id={nd['_id']}  pid={nd.get('treeinfo_pid')}  level={nd.get('_level')}")
    print(f"      name={nd.get('name')}")
    print(f"      path={' > '.join(str(x) for x in p)}")
node, path = gdp[0]

# --- 2. queryIndicatorsByCid ---
print(); print("="*78); print("C. new/queryIndicatorsByCid"); print("="*78)
for q in [f"cid={node['treeinfo_pid']}&dt=&name=",
          f"cid={node['treeinfo_pid']}&dt=&name=国内生产总值",
          f"cid={node['_id']}&dt=&name="]:
    r = get_text(f"{API}/new/queryIndicatorsByCid?{q}", referer=PAGE, headers=H, save=True)
    show(f"queryIndicatorsByCid?{q}", r, 600)

# --- 3. POST 取数（照抄前端 payload 结构）---
print(); print("="*78); print("D. POST getEsDataByIndicatorIdAndDa"); print("="*78)
payloads = [
  {"尝试1-minimal": {"cid": node["treeinfo_pid"], "targets": [node], "targetList": [node],
                     "code": "3", "datePage": "yearData", "dateValue": "2020",
                     "area": "000000000000", "dateNum": "1", "tabsType": "simple",
                     "defaultArea": "", "isRemoveEmptyRowAndCol": False}},
  {"尝试2-nodeAsCid": {"cid": node["_id"], "targets": [node], "targetList": [node],
                     "code": "3", "datePage": "yearData", "dateValue": "2020",
                     "area": "000000000000", "dateNum": "1", "tabsType": "simple",
                     "defaultArea": "", "isRemoveEmptyRowAndCol": False}},
]
for tag, pl in payloads:
    r = get_text(f"{API}/getEsDataByIndicatorIdAndDa", method="POST", referer=PAGE,
                 headers=HJ, json_body=pl, save=True)
    show(tag, r, 800)
    print(f"      request body: {json.dumps(pl, ensure_ascii=False)[:400]}")
