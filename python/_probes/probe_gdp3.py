import json, sys
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(r"D:\universe\econ-data-harvester")
sys.path.insert(0, str(ROOT / "python"))
from econ_core.http_client import get_text

API  = "https://data.stats.gov.cn/dg/website/publicrelease/web/external"
PAGE = "https://data.stats.gov.cn/dg/website/page.html"
H  = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json, text/plain, */*"}
HJ = dict(H); HJ["Content-Type"] = "application/json;charset=UTF-8"

GDP_IND = "7dc6a2ee6c614960b7059991e0cc4d96"   # 国内生产总值 (亿元)
GDP_CID = "f7fd25aaad184414875632cf2327da60"

def get(tag, url):
    r = get_text(url, referer=PAGE, headers=H, save=True)
    print(f"\n[{r.status}] {tag}")
    print(f"   REQ  : {r.url}")
    print(f"   cache: {r.cache_path.name if r.cache_path else None}")
    print(f"   RESP : {r.content[:700]}")
    return r

print("="*78); print("A. getDaCatalogTreeByIndicatorCid —— 指标→数据表映射"); print("="*78)
r = get(f"indicatorCid={GDP_IND}", f"{API}/getDaCatalogTreeByIndicatorCid?indicatorCid={GDP_IND}")
try:
    j = json.loads(r.content); d = j.get("data")
    if isinstance(d, list):
        print(f"   -> {len(d)} 个节点")
        for nd in d[:6]:
            print(f"      {json.dumps(nd, ensure_ascii=False)[:300]}")
except Exception as e:
    print(f"   解析失败 {e}")

print(); print("="*78); print("B. getDasByDaCatalogId 不同 daCid 试探"); print("="*78)
for cand, lab in [(GDP_IND, "指标节点id"), ("db8e5a86c08246e79b1b11251927e740", "ek_dp 前缀")]:
    r = get(f"daCid={lab}", f"{API}/getDasByDaCatalogId?daCid={cand}")
    try:
        d = json.loads(r.content).get("data")
        if isinstance(d, list) and d:
            print(f"   -> {len(d)} 个数据表; 首项 {json.dumps(d[0], ensure_ascii=False)[:300]}")
    except Exception: pass

print(); print("="*78); print("C. POST 取数：候选 da 组合 + 年份"); print("="*78)
def post(tag, pl):
    r = get_text(f"{API}/getEsDataByIndicatorIdAndDa", method="POST", referer=PAGE,
                 headers=HJ, json_body=pl, save=True)
    print(f"\n[{r.status}] {tag}")
    print(f"   BODY : {json.dumps(pl, ensure_ascii=False)}")
    print(f"   cache: {r.cache_path.name if r.cache_path else None}")
    print(f"   RESP : {r.content[:600]}")
    return r

CAND_DA = ["db8e5a86c08246e79b1b11251927e740", GDP_IND, GDP_CID, "", None]
for da in CAND_DA[:3]:
    post(f"da={da}", {"cid": GDP_CID, "id": GDP_IND, "da": da, "dt": "",
                      "rootId": GDP_CID, "dts": ["2020YY"]})
post("da=ek_dp整体", {"cid": GDP_CID, "id": GDP_IND, "da": "db8e5a86c08246e79b1b11251927e740",
                      "dt": "", "rootId": GDP_CID, "dts": ["2020YY", "2021YY", "2019YY"]})
