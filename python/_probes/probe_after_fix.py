import json, sys, hashlib
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(r"D:\universe\econ-data-harvester")
sys.path.insert(0, str(ROOT / "python"))
from econ_core.http_client import get_text, cache_path_for

API  = "https://data.stats.gov.cn/dg/website/publicrelease/web/external"
PAGE = "https://data.stats.gov.cn/dg/website/page.html"
HI = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json, text/plain, */*"}
HJ = dict(HI); HJ["Content-Type"] = "application/json;charset=UTF-8"
GDP_IND = "7dc6a2ee6c614960b7059991e0cc4d96"
GDP_CID = "f7fd25aaad184414875632cf2327da60"
ROOTID  = "71d41888d5a44bb2a67402ef4e60003e"
URL_ES  = f"{API}/getEsDataByIndicatorIdAndDa"

print("="*78); print("0. 缓存键规则自检"); print("="*78)
b1 = {"cid": GDP_CID, "id": GDP_IND, "da": "000000000000", "dt": "", "rootId": ROOTID, "dts": ["2015YY"]}
b2 = {"cid": GDP_CID, "id": GDP_IND, "da": "000000000000", "dt": "", "rootId": ROOTID, "dts": ["2016YY"]}
print("  GET  key:", cache_path_for(URL_ES, "GET")[0].name, "  expect", hashlib.sha256(("GET:"+URL_ES).encode()).hexdigest()[:16])
for tag, bb in [("POST body#1", b1), ("POST body#2", b2)]:
    js = json.dumps(bb, ensure_ascii=False)
    print(f"  {tag} key:", cache_path_for(URL_ES, "POST", js)[0].name,
          "  expect", hashlib.sha256(("POST:"+URL_ES+":"+js).encode()).hexdigest()[:16])
print("  -> 两个不同 body 的键是否不同:", cache_path_for(URL_ES,"POST",json.dumps(b1,ensure_ascii=False))[0].name
      != cache_path_for(URL_ES,"POST",json.dumps(b2,ensure_ascii=False))[0].name)

print(); print("="*78); print("A. 2015-2024 年度 GDP（save=True）"); print("="*78)
plA = {"cid": GDP_CID, "id": GDP_IND, "da": "000000000000", "dt": "", "rootId": ROOTID,
       "dts": [f"{y}YY" for y in range(2015, 2025)]}
rA = get_text(URL_ES, method="POST", referer=PAGE, headers=HJ, json_body=plA, save=True)
print(f"  REQUEST  : POST {URL_ES}")
print(f"  REQUEST BODY: {json.dumps(plA, ensure_ascii=False)}")
print(f"  STATUS   : {rA.status}")
print(f"  RAW .bin : {rA.cache_path}")
print(f"  RAW meta : {rA.meta_path}")
print(f"  raw bytes: {rA.cache_path.stat().st_size}  sha256[:16]={hashlib.sha256(rA.cache_path.read_bytes()).hexdigest()[:16]}")
print(f"  RESP (full):")
print(rA.content)
try:
    j = json.loads(rA.content)
    print("\n  逐项原始数值:")
    for it in j["data"]:
        print(f"    {it['dt']:<8} dt_name={it['dt_name']:<8} v={it['v']:<12} unit={it['unit']:<6} i={it['i']} i_name={it['i_name']}")
except Exception as e:
    print("  解析失败:", e)

print(); print("="*78); print("B. getDefaultIndicData?code=21"); print("="*78)
for meth in ["GET", "POST"]:
    u = f"{API}/new/getDefaultIndicData?code=21"
    kw = {"json_body": {"code": 21}} if meth == "POST" else {}
    r = get_text(u, method=meth, referer=PAGE, headers=(HJ if meth=="POST" else HI), save=True, **kw)
    print(f"\n  --- METHOD={meth} ---")
    print(f"  REQUEST : {meth} {u}")
    if meth == "POST": print(f"  REQUEST BODY: {json.dumps({'code':21})}")
    print(f"  STATUS  : {r.status}")
    print(f"  RAW .bin: {r.cache_path}")
    print(f"  RESP[:500]: {r.content[:500]}")

print(); print("="*78); print("C. getDaCatalogTreeByIndicatorCid 参数名变体"); print("="*78)
IDV = "db8e5a86c08246e79b1b11251927e740"
variants = [
    ("GET  ?indicatorCid=", f"{API}/getDaCatalogTreeByIndicatorCid?indicatorCid={IDV}", "GET", {}),
    ("GET  ?cid=",          f"{API}/getDaCatalogTreeByIndicatorCid?cid={IDV}", "GET", {}),
    ("POST body {indicatorCid}", f"{API}/getDaCatalogTreeByIndicatorCid", "POST", {"json_body": {"indicatorCid": IDV}}),
]
for tag, u, meth, kw in variants:
    r = get_text(u, method=meth, referer=PAGE, headers=(HJ if meth=="POST" else HI), save=True, **kw)
    abnormal = "服务异常" in r.content
    print(f"\n  --- {tag} ---")
    print(f"  REQUEST : {meth} {u}")
    if kw: print(f"  REQUEST BODY: {json.dumps(kw['json_body'], ensure_ascii=False)}")
    print(f"  STATUS  : {r.status}   服务异常页={abnormal}")
    print(f"  RAW .bin: {r.cache_path}")
    print(f"  RESP[:500]: {r.content[:500]}")
