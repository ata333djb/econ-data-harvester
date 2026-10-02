import json, sys
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[2]  # 项目根：从本文件位置推导，不写死机器路径
sys.path.insert(0, str(ROOT / "python"))
from econ_core.http_client import get_text

API  = "https://data.stats.gov.cn/dg/website/publicrelease/web/external"
PAGE = "https://data.stats.gov.cn/dg/website/page.html"
H  = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json, text/plain, */*"}
HJ = dict(H); HJ["Content-Type"] = "application/json;charset=UTF-8"

def call(tag, url, **kw):
    r = get_text(url, referer=PAGE, headers=H, save=True, **kw)
    b = r.content
    print(f"\n[{r.status}] {tag}")
    print(f"   REQ  : {r.method} {r.url}")
    if kw.get("json_body") is not None:
        print(f"   BODY : {json.dumps(kw['json_body'], ensure_ascii=False)}")
    print(f"   cache: {r.cache_path.name if r.cache_path else None}")
    print(f"   RESP : {b[:500]}")
    return r

GDP_CID = "f7fd25aaad184414875632cf2327da60"   # 年度数据 > 国民经济核算 > 国内生产总值
GDP_NODE = "7dc6a2ee6c614960b7059991e0cc4d96"  # 指标: 国内生产总值 (亿元)

print("="*78); print("步骤1: 数据表列表 getDasByDaCatalogId"); print("="*78)
r1 = call("getDasByDaCatalogId?daCid=GDP_CID", f"{API}/getDasByDaCatalogId?daCid={GDP_CID}")

print(); print("="*78); print("步骤2: 指标列表 queryIndicatorsByCid"); print("="*78)
r2 = call("queryIndicatorsByCid", f"{API}/new/queryIndicatorsByCid?cid={GDP_CID}&dt=&name=")
inds = []
try:
    j = json.loads(r2.content)
    inds = (j.get("data") or {}).get("list") or []
    print(f"   -> 解析出 {len(inds)} 个指标")
    for it in inds[:5]:
        print(f"      _id={it.get('_id')}  i_showname={it.get('i_showname')}  ek_dp={it.get('ek_dp')}")
except Exception as e:
    print(f"   解析失败: {e}")

das = []
try:
    j1 = json.loads(r1.content)
    das = j1.get("data") or []
    print(f"   -> 解析出 {len(das)} 个数据表")
    for d in das[:5]:
        print(f"      {json.dumps(d, ensure_ascii=False)[:220]}")
except Exception as e:
    print(f"   解析失败: {e}")

print(); print("="*78); print("步骤3: POST getEsDataByIndicatorIdAndDa 取 2020 年 GDP"); print("="*78)
if inds:
    ind = inds[0]
    da_val = None
    if das:
        d0 = das[0]
        da_val = d0.get("name_value") or d0.get("_id") or d0.get("value")
    print(f"   id  = {ind.get('_id')}")
    print(f"   da  = {da_val}")
    for label, da in [("da=数据表列表首项", da_val), ("da=GDP_CID", GDP_CID)]:
        for dts in [["2020YY"], ["2020YY","2021YY"], []]:
            pl = {"cid": GDP_CID, "id": ind.get("_id"), "da": da, "dt": "",
                  "rootId": GDP_CID, "dts": dts}
            call(f"{label} dts={dts}", f"{API}/getEsDataByIndicatorIdAndDa", method="POST", json_body=pl)
            break
        break
