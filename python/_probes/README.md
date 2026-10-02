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
cd <项目根>
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

---

## 已知观察：`cid` 被后端忽略，错 `cid` 会写出重复的 parsed 副本

第二轮（World Bank 接入 + 交叉验证）验证门禁 FAIL 分支时，曾把
`tools/compare-gdp.py` 里的 `NBS_CID` 临时改成 `00000000000000000000000000000000`
再跑一次门禁，结果竟然是 **6/6 PASS（没有失败）**。查证后确认：这不是门禁漏报，
而是一个真实的接口行为。

* `getEsDataByIndicatorIdAndDa` **忽略 `cid`**：它按 `id`(=tree_node_id) +
  `rootId` + `da` + `dts` 选序列。错 `cid` 返回的观测与正确 `cid` 完全相同，
  连 raw 存档都**逐字节一致**（361 字节，sha256[:12]=`847200508cdb`）；
  响应体里也没有 `cid` 字段（回显为空）。该参数不参与任何服务端判定。
* **副作用（数据血缘隐患，本轮不修）**：`nbs_client` 的 parsed 落盘文件名是
  `sha256(规范化 request 参数)[:16]`，而 `cid` 在 request 里。于是错 `cid`
  的调用会在 `data/parsed/nbs/` 下写出**另一个指纹的文件**，内容与正版逐字节相同：

  | request 里的 cid | parsed 文件 |
  |---|---|
  | `f7fd25aaad184414875632cf2327da60`（正确） | `getEsDataByIndicatorIdAndDa_4441bd14d17607d5.json` |
  | `00000000000000000000000000000000`（错） | `getEsDataByIndicatorIdAndDa_bb224db5c889f098.json` |

  同一份数据因此会有两份不同指纹的副本，事后按 `request` 追溯来源时容易误判。
* 处理决定：**只记录不修**。可选修法（把 `cid` 从 request 指纹里剔除、或落盘前
  校验 `cid` 是否在期望集合内）都会改到生产链路的落盘规则，在 NBS 明确接口语义
  之前保持现状。`nbs_client.fetch_indicator_data` 的 docstring 已就地记录该行为。

---

## 2026-09-27 探测：BIS / OECD / PWT / Maddison 的独立性判定

**探测动机**：方向 C 第一轮（FRED `CHNCPIALLMINMEI`）与 NBS CPI 10 年全部落「一致」
档（最大 0.081 pp），但 FRED 那条序列的上游本就是 NBS 经 OECD 转述 ——
**一致性只证明转述无误，不是独立验证**。所以要去 CPI 维度找一个「真正独立编制」的源。

**判定标准**（本轮统一口径）：*独立编制* = 发布机构**自己采集或自己估算**，
而不是「拿到各国官方数据后做简单换算 / 汇编」。

**探测结论：四个候选全部不是独立编制。**

### 判定依据

| 候选 | 是否独立编制 | 判定依据（实测） |
|---|---|---|
| **BIS** `WS_LONG_CPI` | ❌ 转载 | 官方 FAQ 原文：*"Consumer price indices are predominantly compiled by **national statistical offices**."* BIS 自己的加工只有**拼接**（*"constructed long consumer price indices, by joining the series available for consecutive periods"*）与**重定基**（2010=100） |
| **OECD** `DF_PRICES_ALL` | ❌ 转载 | `CL_METHODOLOGY_PRI` 里中国只有 **`N` = "National"**（无 OECD 调和版）；且其 2015–2024 年度同比与 IMF WEO `PCPIPCH` **逐年逐位完全相同**（1.4/2.0/1.6/2.1/2.9/2.5/0.9/2.0/0.2/0.2） |
| **PWT 11.0** | ❌ 二手 + 自研估算 | PPP 输入来自 ICP（*"PWT is based on the basic data on PPPs from the International Comparison Program"*），缺口用 WB 插值；中国自标 55/72 年为 `Extrapolated` |
| **Maddison 2023** | ❌ 二手（自述） | *"builds on Angus Maddison's original dataset. The original estimates are kept intact"*；对中国 1952–2008 直接用 Wu (2014)；且**全库只有 `gdppc` + `pop`，不含任何价格数据** |

### 对比表（五项要求：API / 独立性 / 中国指标 / 年份 / 接入成本）

| | 公开 API | 独立编制 | 中国价格类指标 | 年份范围 | 接入成本 | 独立价值 |
|---|---|---|---|---|---|---|
| **BIS** | ✅ SDMX 2.1 免密钥 | ❌ 转载 NSO（自认） | 月度 CPI 指数 + 同比 | **1995-01 ~ 2026-08** | **低** | 拼接/重定基的独立实现 |
| **OECD** | ✅ SDMX（**有 429**） | ❌ `N`=National | CPI；自编 PPP **无中国** | 2015=100 起 | 中 | **零**（与 IMF 逐位相同） |
| **PWT 11.0** | ❌ 仅 XLSX/DTA | ❌ ICP/WB + 自研估算 | `pl_gdpo` 价格水平 | 1950–2023 | 高（TLS + 大文件） | 中国 55/72 年自标 Extrapolated |
| **Maddison 2023** | ❌ 仅 XLSX/DTA | ❌ 自述二手 | **无** | 1–2022 | 高 | **零** |

### 关键实测证据

**BIS**

- 端点 `https://stats.bis.org/api/v1/`；**`?format=jsondata` 恒 406**（`Unsupported format: jsondata`），
  只能用 `format=sdmx-json`；`/datastructure/...` 端点实测**读超时**（180s），不可依赖
- 密钥形状 **`FREQ.REF_AREA.UNIT_MEASURE` 三位都要给**（`CN.M` → 404
  `No data for data query`，正确写法 `M.CN.771`）；维度顺序取自 `availableconstraint` 的 `cubeRegions`
- `UNIT_MEASURE`：`771` = 同比 %，`628` = 指数（2010=100）
- 实测规模：`M.CN.771` 368 条（1996-01 起）、`M.CN.628` 380 条（**1995-01 起**）、
  `A.CN.628` 48 条（1978 起）、`A.CN.771` 47 条（1979 起）
- **BIS 的 771 与它自己的 628 逐位吻合**（算过 `idx[y]/idx[y-1]*100-100`，11 年全等）
  → 证实 BIS 是「自建指数 → 自行折算同比」，这是它的加工所在
- 与 NBS「上年=100」对齐后差异：**max 0.174 pp（2018）/ mean 0.09 pp**，
  与 NBS×FRED 基线（0.081 pp）**同量级**；差异来源 BIS 自述为舍入
  （*"The year-on-year changes are calculated from the index data. Therefore, they can differ
  from the official statistics due to rounding effects."*）

**OECD**

- structure 服务的 `format` 白名单不含 `jsondata`（报错正文列出：
  `structure, xml-structure-3.0.0, sdmx-3.0, json-structure-2.0.0`）；
  data 服务是**另一套**白名单（含 `jsondata` / `csv`）—— 见 PROJECT_STATE §3.12
- `dataflow/all/all/latest?format=structure` → 200，**8,920,015 字节 / 1548 个 dataflow / 53 个 agency**；
  `dataflow/OECD/all/latest` → 404 `No Results Found`（agency 必须写全，如 `OECD.SDD.TPS`）
- `/data/` **拒绝不完整密钥**：`.../DF_PRICES_ALL,1.0/CHN` → 403 `Not enough key values in query, expecting 8 got 1`
- 中国在 OECD **自编 PPP 全家桶里完全缺席**：`DF_PPP` / `DF_PPP_PPP` / `DF_PPP_CPL`（constraint 51 个地区）、
  `DF_PP_CPL_M`（38 个）、`DF_PRICES_COICOP2018@DF_PRICES_C2018_ALL`（40 个）逐个查过，均无 CHN；
  拉数据返回 404 `NoRecordsFound`
- OECD 唯一发布的那个中国 PPP（`DF_TABLE4` 的 `PPP_B1GQ`）实测与 World Bank `PA.NUS.PPP`
  **2015–2021 abs diff = 0.000000（6 位小数）**，2022–2023 差 0.06~0.08%（WB 修订所致）
  → 那是世界银行 ICP 的数，不是 OECD 的
- **限流**：约 15 次快速请求后 429（正文提示联系 OECD Data Explorer feedback form），
  `Retry-After: 0` **不可信**（立刻重试仍 429），需 15~30s 静默；稳定做法 **9~12s 间隔 + 指数退避**
- **没有机器可读的 provenance 字段**（`metadata/dataflow/...` → 403 `Invalid structure`，
  `metadatastructure/...` → 404），来源只能靠数值比对反推 —— 上面两条结论正是这么得来的

**PWT 11.0**（发布于 2025-10-07，DOI `10.34894/FABVLR`）

- 观测值**只有 XLSX / DTA**；DataverseNL 的 REST API 只给元数据（`/api/datasets/:persistentId/` → 200 JSON）；
  `cran.r-project.org/package=pwt11` → 404（`pwt10` 是旧版）；PyPI 无 `pwt` 包；**无 SDMX 端点**
- 实测 `pwt110.dta` 13,690 行 = 185 国 × 74 年，**1950–2023**；
  `pwt110.xlsx` Content-Length **5,839,841**，sha256 `7b337e94f39dfe…`（下载截断问题见 §3.11）
- 变量（官方 Legend 原文）：`pl_gdpo` = "Price level of CGDPo (PPP/XR), price level of USA GDPo in 2021=1"；
  参考年 PWT 11.0 从 2017 改为 **2021**
- **`i_cig` 标志统计（中国）**：Benchmark 仅 **2005 / 2011 / 2017 / 2021 四年**；
  ICP 时序基准或插值 2012–2016、2018–2020（8 年）；Interpolated 2006–2010（5 年）；
  **Extrapolated 其余 55 年（1952–2004 及 2022–2023）** —— 即中国价格水平大部分年份
  是**外推**的。实测 `pl_gdpo`：2005 = 0.2242 → 2011 = 0.4528 → 2021 = 0.6219 → 2023 = 0.5760
- PWT 11.0 **换掉了中国 GDP 口径**：此前用 Wu (2014) 替代序列，11.0 改用官方 GDP
  （UN NAMA，回溯到 1952），Wu 序列降级保留为 **`CH2`** —— 实测 `pwt110_na_data.dta`
  里 `CHN`（1952–2025）与 `CH2`（1950–2021）并存，印证文档说法
- 许可 CC BY 4.0

**Maddison 2023**（DOI `10.34894/INZBF2`，Dataverse publicationDate 2024-04-26）

- 实测下载并解析 `mpd2023_web.xlsx`（**4,903,804 字节**，sha256 `ecc5916ca12789b9…`，
  与子代理独立下载的长度一致）；Dataverse 元数据 API 确认只含 2 个文件（XLSX + DTA）
- 工作表：`Notes` / `Sources` / `GDPpc` / `Population` / `Full data` / `Regional data` / `Maddison original sources`
- **`Full data` 表只有 6 列**：`countrycode, country, region, year, gdppc, pop`
  —— **没有任何价格指数**，也没有 `cgdppc`
- 中国：**776 个观测，公元 1 ~ 2022**（有 post-2020 数据：2021 gdppc = 18,666.60，
  2022 = 19,238.18，2011$）；全库最大年份 2022，**无 2023+**
- 中国来源（`Sources` 表原文）：1000–1661 Broadberry/Guan/Li (2018)；1661–1933 该文 + Xu et al. (2016)；
  **1952–2008 Wu (2014)**（Conference Board EPWP #14-01）；人口 1990 起 Conference Board TED
- 许可 CC BY 4.0，另有「图形展示」与「少于 12 国的子集」两种情形必须引用原始论文的附加条款

### 本轮探测留下的证据物

- `data/raw/_probe_pwt_maddison/maddison2023.xlsx`（4,903,804 B，`data/raw/*` 已被 gitignore，
  不 commit，留作探测证据；sha256 与子代理独立下载交叉验证一致）
- 被截断的 `pwt110.xlsx` **没有保留**（下载不完整，不是干净证据；PWT 各项事实均有
  子代理的长度校验记录支撑）

### 对后续方向的推论

**正确的方向是把验证目标从「CPI」换成「价格水平」**，因为独立测量**存在**，只是不在 CPI 维度：
世界银行 **ICP 2021** 是唯一真·独立价格采集（各经济体自己采集一篮子代表品，中国参加了
2021 轮，NBS 2024-05 自行发布过结果）。单独立项，见 PROJECT_STATE §5.3。

---

## 2026-09-27 探测：指标目录的源可达性（`probe_catalog_sources.py`）

**探测动机**：方向 E 第一轮要做「用户输入指标名 -> 得到数据」，第一层是指标目录
（`../econ_core/catalog_data.yaml`）。目录里每一条「某指标在某源上要什么参数」
都必须是**真跑过取数**的结论，不能靠猜代码或抄文档。

**用法**（8 个阶段，可单跑）：

```powershell
$env:PYTHONPATH="<项目根>\python"
.\.venv\Scripts\python.exe python\_probes\probe_catalog_sources.py nbs        # 拉三棵目录树
.\.venv\Scripts\python.exe python\_probes\probe_catalog_sources.py nbs_targets # 18 指标候选三元组
.\.venv\Scripts\python.exe python\_probes\probe_catalog_sources.py wb         # WB 全量目录 29544 条
.\.venv\Scripts\python.exe python\_probes\probe_catalog_sources.py imf|bis|fred
.\.venv\Scripts\python.exe python\_probes\probe_catalog_sources.py verify     # 终验：真跑 56 次取数
.\.venv\Scripts\python.exe python\_probes\probe_catalog_sources.py diag_range # 停更序列的精确停更期
.\.venv\Scripts\python.exe python\_probes\probe_catalog_sources.py diag_wb    # 宽窗口复核
```

**结论速查**

| 项 | 结论 |
|---|---|
| NBS 目录树规模 | 月度 **14605** 叶 / 季度 **799** / 年度 **60325**，拍平后 78946 个节点 |
| NBS `root_id` | **不是**指标的祖先，而是「该目录第一条 level-2 类目」的常量：月 `3c9c4593…` / 季 `1b1ce0cf…` / 年 `71d41888…` |
| NBS `cid` | = 指标叶的 `treeinfo_pid`（父目录节点）；取数时后端忽略（§3.1），取目录树要用 |
| World Bank | 全量目录 **29544** 条（2 页 × 20000）；服务端**不支持搜索**，只能整份取回本地过滤 |
| IMF | DataMapper 目录 **132** 条，本轮全量列出 |
| BIS | **32** 个 dataflow；只有 `WS_LONG_CPI` 找到中国可用密钥（`M.CN.771` / `M.CN.628` / `A.CN.771`），其余 9 个候选密钥全 404（第三维 `UNIT_MEASURE` 各家不同，未逐个解析 datastructure） |
| FRED | **无免密钥搜索**，只能按候选 ID 试：12 个里 11 个可用 |
| 终验结果 | **53 条可取数 / 3 条确认不可用 / 0 条意外失败** |

**三条「探过、确认不可用」**（已留痕未收录，标 `expect_unavailable`）：

| 映射 | 实测 |
|---|---|
| `imf\|BX_GDP`（出口占 GDP 比重） | 对 CHN 返回 **0 行** |
| `imf\|BM_GDP`（进口占 GDP 比重） | 对 CHN 返回 **0 行** |
| `worldbank\|GC.DOD.TOTL.GD.ZS`（中央政府债务） | 1960-2025 共 66 行**全为 null** |

**一条「有数据但已停更」**：NBS `实际利用外商直接投资金额累计值` ——
2014-01..**2019-11** 共 71 个非空期（末值 124394），2020-01 起全部是占位行。

**本轮最大的方法学教训**：**「行数」不等于「有数据」**。NBS 对未发布的期会回
「`dt_name` 有值、`v` 是空串」的占位行，第一版终验按 `len(raw)` 判通过，
把 3 条映射判成 OK，其中 1 条（FDI）是真假通过。判据改成**非空值个数**后立刻分明。
另外 **「某窗口全空」不等于「源不支持」** —— `GC.DOD.TOTL.GD.ZS` 就是换到 1960-2025
才敢下的结论。详见 PROJECT_STATE §3.15。

**证据物**（`data/raw/_probe_catalog/`，已被 gitignore）：`verified.json`（逐条取数结果）、
`nbs_index.json`（78946 节点）、`nbs_targets.json`、`nbs_tree_{1,2,3}.json`、
`wb_index.json`、`imf_index.json`、`bis_dataflows.json`、`bis_probe.json`、`fred_probe.json`、
`diag_range.json`、`diag_wb.json`、`diag_nbs.json`。
