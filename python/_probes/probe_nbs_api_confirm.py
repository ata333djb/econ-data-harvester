import sys, json
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]  # 项目根：从本文件位置推导，不写死机器路径
sys.path.insert(0, str(ROOT / "python"))
from econ_core.http_client import get_text

BASE = "https://data.stats.gov.cn"
API  = f"{BASE}/dg/website/publicrelease/web/external"
PAGE = f"{BASE}/dg/website/page.html"
H = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json, text/plain, */*"}

TESTS = [
    ("省列表",            f"{API}/getAllProvince"),
    ("目录+指标树(code=hgnd)", f"{API}/getCatalogsAndIndexTree?code=hgnd"),
    ("目录+指标树(code=zd)",   f"{API}/getCatalogsAndIndexTree?code=zd"),
    ("默认指标数据",       f"{API}/new/getDefaultIndicData"),
    ("宏观数据",           f"{API}/queryMacroecData"),
    ("最新发布",           f"{API}/getNewestPub"),
    ("发布日程",           f"{API}/queryAgendas"),
    ("全部发布库",         f"{API}/queryAllPblIbs"),
    ("搜索 query",         f"{API}/query?search=GDP"),
    ("搜索 queryCount",    f"{API}/queryCount?search=GDP"),
    ("CMS 文章",           f"{API}/new/queryCMSArticles?code=zxfb&pagenum=1&pageSize=10"),
]
ok = []
for label, url in TESTS:
    try:
        r = get_text(url, referer=PAGE, headers=H, save=True)
        b = r.content
        isjson = b.lstrip().startswith(("{", "["))
        kind = "SPA-404" if "页面不存在" in b else ("SPRING-404" if '"status":404' in b else ("JSON" if isjson else "OTHER"))
        print(f"\n[{r.status}] {kind:<11} {len(b):>7}B  {label}")
        print(f"    {url}")
        print(f"    cache={r.cache_path.name if r.cache_path else None}")
        if kind == "JSON":
            print(f"    >>> {b[:400]}")
            ok.append({"label": label, "url": url, "status": r.status, "bytes": len(b),
                       "cache": r.cache_path.name if r.cache_path else None, "head": b[:1500]})
    except Exception as e:
        print(f"\n[FAIL] {label}: {type(e).__name__}: {e}")

Path("data/parsed/nbs_api_confirmed.json").write_text(
    json.dumps({"api_base": API, "confirmed": ok}, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"\n\n===== 可用端点 {len(ok)}/{len(TESTS)} =====  [报告 -> data/parsed/nbs_api_confirmed.json]")
