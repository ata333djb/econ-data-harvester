# NBS 新版数据平台 API 探测档案（_probes）

本目录保存国家统计局新版数据平台（DSF 框架 SPA）接口逆向过程中的一次性脚本，
**仅作可复现的探索证据保留，不是生产代码**。

生产接口已固化到 [`../econ_core/nbs_client.py`](../econ_core/nbs_client.py)。

---

## 最终结论速查

| 项 | 结论 |
|---|---|
| API 根 | `https://data.stats.gov.cn/dg/website/publicrelease/web/external/` |
| 前缀规则 | bundle 里写的是 `/publicrelease/...`，前端 `dsf.url.getWebPath()` 会补上 `webRoot.default = "/dg/website/"` |
| 取数接口 | `POST getEsDataByIndicatorIdAndDa`，JSON body `{cid, id, da, dt, rootId, dts}` |
| `da` 语义 | **地区代码**（全国 `000000000000`），不是数据表 id |
| `dts` 语义 | 时间点代码：年度 `"2020YY"` / 季度 `"2020SS"` / 月度 `"2020MM"` |
| 目录树 | `GET getCatalogsAndIndexTree?code=` → `1` 月度 / `2` 季度 / `3` 年度 |
| 指标列表 | `GET new/queryIndicatorsByCid?cid=&dt=&name=` |
| 必要请求头 | `Referer: .../dg/website/page.html`、`X-Requested-With: XMLHttpRequest` |
| 鉴权 | **无需登录/cookie** |
| 最大坑 | **HTTP 状态码不反映业务结果**：路径/参数错可能返回 200 + HTML「服务异常」页；业务失败返回 200 + `success:false` |
| 旧版接口 | `easyquery.htm` 已被 WAF 按 `reason:UrlACL` 403 封禁，不可用 |
| 传输层 | 本机 Schannel 损坏（curl/.NET/IWR 全废），必须走 Python OpenSSL 栈 |

---

## 脚本清单

按探索时间顺序排列。

| # | 脚本 | 当时探测什么 | 结论 |
|---|---|---|---|
| 1 | `probe_nbs_bundles.py` | 下载首页 11 个 bundle，grep `publicrelease` / `external` / `tree` 等关键词 | 定位到 `/publicrelease/web/external/` 命名空间；发现 `webRoot.default="/dg/website/"` 前缀规则；`easyquery` 在 bundle 中 0 命中 |
| 2 | `probe_nbs_endpoints.py` | 枚举 bundle 内所有 `$http.*()` 调用，实测修正前缀后的响应形态 | 得到 30 个候选路径；确认 `/publicrelease/...` 直连返回 Spring Whitelabel 404（通用 404，不能用于判断路径对错）。**Part 1 为纯离线**，读 raw 缓存即可复跑 |
| 3 | `probe_nbs_chunks.py` | 从 `subPage.js` 提取 webpack chunk 名映射表 | 页面代码**按需懒加载**，核心取数逻辑不在首页 11 个 bundle 内 —— 这是前两轮 grep 不到的根本原因 |
| 4 | `probe_nbs_pagechunks.py` | 逐个拉取页面级 chunk（`DataPage.js` 等）并提取端点 | 拿到 20+ 真实端点；`DataPage.js`（223 KB，数据查询页）给出完整取数 API 面 |
| 5 | `probe_nbs_api_confirm.py` | 用「`/dg/website/` + `/publicrelease/...`」前缀批量验证 11 个端点 | 首次成功：`getAllProvince` 返回 200 JSON。确认前缀正确 |
| 6 | `probe_datapage_ctx.py` | `DataPage.js` 中 5 个关键词 ±200 字符上下文 | `getEsDataByIndicatorIdAndDa` = **POST + JSON**；`getDefaultIndicData` 的 `code = Number(returnCode(datePage)) + 18`；`queryMacroecData` / `getCatalogsAndIndexTree` 不在本文件 |
| 7 | `probe_datapage_params.py` | POST 调用点上游 2600 字符 + `indicators` / `daCid` / `returnCode` / `datePage` 线索 | 找到 `getChartData(e)` 包裹结构；确认 `daCid` 走 `getDasByDaCatalogId` |
| 8 | `probe_datapage_payload.py` | payload 对象字面量、`returnCode` / `datePage` 取值域、`targetList` 来源 | 拿到完整 payload 字段表；`datePage` 取值域（`yearData` / `monthData` / `quarterData` …） |
| 9 | `probe_datapage_find.py` | 跨全部 chunk 搜索定义位置 | `returnCode:function` 定义在 `DataPage~ReportPage~SearchPage.js` 与 `MapPage.js`，不在 `DataPage.js` |
| 10 | `probe_params_final.py` | `returnCode` / `getTargetList` / `queryIndicatorsByCid` / `getCatalogsAndIndexTree` 定义 | `returnCode` 查 `tabsCode`（`monthData→1` / `quarterData→2` / `yearData→3`）；`getTargetListParams = {cid, dt, name}` |
| 11 | `probe_tree_codes.py` | 提取 `tabsCode` 全表 + 对 `getCatalogsAndIndexTree?code=N` 扫频 | `code` 1/2/3 = 月度/季度/年度；年度树含 **60,325** 个指标叶子 |
| 12 | `probe_payload_build.py` | `getChartData` 调用点上游 3200 字符 | 确认 payload 结构 `{cid, id, da, dt, rootId, dts}` 与 `dts = [年份 + "YY"]` 的拼装规则。**纯离线脚本** |
| 13 | `probe_gdp.py` | 首次构造 POST 取数 | 返回 200 `success:true` 但 `v=""` —— `da` 猜成数据表 id、`id` 用了错误的指标标识 |
| 14 | `probe_gdp2.py` | 打通 `new/queryIndicatorsByCid` | 取到 6 个指标（含 GDP，`_id=7dc6a2ee…`）；POST 结构正确但 `da=null` → `v` 仍为空 |
| 15 | `probe_gdp3.py` | 试探 `da` 的候选值 | 三个候选值全部被**原样回显**到响应的 `code` 字段、`v` 恒为空 —— 证明**该接口不校验 `da`**，错误参数表现为静默空值而非报错 |
| 16 | `probe_gdp4.py` | `da = 地区代码 "000000000000"` | **成功**：2020 年 GDP = `1034867.6` 亿元；`rootId` 取 `tree[0].children[0]._id` |
| 17 | `probe_da_source.py` | 定位 `integrationChartRequest` 的调用者，追 `da` 来源 | `integrationChartRequest(this.target, this.areaValue)` → **`da` 就是 `areaValue`（地区代码）**；顺带发现 `http_client` 缓存键缺陷（POST body 未参与键，5 次不同 body 覆盖同一 `.bin`） |
| 18 | `probe_after_fix.py` | 缓存键修复后的回归验证 | ① 键规则自检通过 ② 2015–2024 全年 GDP 落盘成功 ③ `getDefaultIndicData` 仅 GET 可用（POST 返回服务异常页）④ `getDaCatalogTreeByIndicatorCid` 参数名必须是 `cid`（`indicatorCid` 与 POST 均返回服务异常页） |

---

## 关于缓存键

`http_client` 的缓存键在探索过程中修过一次：

| 版本 | 规则 |
|---|---|
| 修复前 | `sha256(url)[:16]` —— POST body 未参与，不同 body 互相覆盖存档 |
| 修复后 | GET → `sha256("GET:" + url)[:16]`；POST → `sha256("POST:" + url + ":" + body)[:16]` |

影响：

* `probe_nbs_chunks.py`、`probe_nbs_endpoints.py` 已改为 `cache_path_for()` 优先 + **旧键回退**，两种命名都能读。
* 以下 4 个脚本**硬编码了旧键** `0aa788b698ee8e07`（= `DataPage.js` 的旧键）：
  `probe_da_source.py`、`probe_datapage_params.py`、`probe_gdp.py`、`probe_payload_build.py`。
  这些脚本当初是**离线读缓存**的（不联网），而对应的旧存档文件仍在 `data/raw/_http_cache/` 下，
  因此仍可原样复跑；若日后清空 raw 缓存，这些脚本需改用 `cache_path_for()`。

---

## 运行方式

所有脚本均需项目 venv 的 Python（本机 Schannel 损坏，必须走 OpenSSL 栈），
且需要把 `python/` 加入 `sys.path`（脚本内已自行引导）：

```powershell
cd D:\universe\econ-data-harvester
.\.venv\Scripts\python.exe .\python\_probes\probe_payload_build.py      # 纯离线，读 raw 缓存
.\.venv\Scripts\python.exe .\python\_probes\probe_after_fix.py          # 联网
```

## 其他说明

* 全部 18 个脚本已统一去除 **UTF-8 BOM**（原由 PowerShell `Set-Content -Encoding UTF8` 生成，
  带 BOM 会使 `#!/usr/bin/env python3` shebang 在类 Unix 上失效）。
* 脚本产出物：
  * raw 原始响应 → `data/raw/_http_cache/`（含首次逆向时下载的全部 JS bundle）
  * parsed 报告 → `data/parsed/nbs_*.json`（各轮扫描结果）
* 本目录不参与生产链路；任何生产逻辑请改 `../econ_core/`。
