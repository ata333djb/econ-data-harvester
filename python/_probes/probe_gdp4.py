import json, sys
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(r"D:\universe\econ-data-harvester")
sys.path.insert(0, str(ROOT / "python"))
from econ_core.http_client import get_text

API  = "https://data.stats.gov.cn/dg/website/publicrelease/web/external"
PAGE = "https://data.stats.gov.cn/dg/website/page.html"
HJ = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json, text/plain, */*",
      "Content-Type": "application/json;charset=UTF-8"}
H  = {k:v for k,v in HJ.items() if k != "Content-Type"}

# 年度树 -> 取 rootId（root.childNodes[0].childNodes[0]._id）
r = get_text(f"{API}/getCatalogsAndIndexTree?code=3", referer=PAGE, headers=H, save=False)
tree = json.loads(r.content)["data"]
root_node = tree[0]                                  # 年度数据
first_cat = (root_node.get("children") or [None])[0] # 第一个一级类目
ROOTID = first_cat.get("_id")
print(f"rootId = {ROOTID}  ({first_cat.get('othername')})")

GDP_IND = "7dc6a2ee6c614960b7059991e0cc4d96"
GDP_CID = "f7fd25aaad184414875632cf2327da60"
EK_DP   = "db8e5a86c08246e79b1b11251927e740_1"
AREA    = "000000000000"

def post(tag, pl):
    r = get_text(f"{API}/getEsDataByIndicatorIdAndDa", method="POST", referer=PAGE,
                 headers=HJ, json_body=pl, save=False)
    print(f"\n[POST {r.status}] {tag}")
    print(f"   BODY : {json.dumps(pl, ensure_ascii=False)}")
    print(f"   RESP : {r.content[:700]}")
    return r

print(); print("="*78); print("da = 地区代码 组合测试"); print("="*78)
post("id=指标_id, cid=父节点", {"cid": GDP_CID, "id": GDP_IND, "da": AREA, "dt": "",
                              "rootId": ROOTID, "dts": ["2020YY"]})
post("id=ek_dp",           {"cid": GDP_CID, "id": EK_DP, "da": AREA, "dt": "",
                              "rootId": ROOTID, "dts": ["2020YY"]})
post("id=指标_id, cid=指标自身", {"cid": GDP_IND, "id": GDP_IND, "da": AREA, "dt": "",
                              "rootId": ROOTID, "dts": ["2020YY"]})
post("多年度 2015-2020",    {"cid": GDP_CID, "id": GDP_IND, "da": AREA, "dt": "",
                              "rootId": ROOTID, "dts": [f"{y}YY" for y in range(2015, 2021)]})
