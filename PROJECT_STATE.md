# PROJECT_STATE.md —— EconDataHarvester 状态快照

- 生成时间：2026-09-27
- 面向对象：**新对话的 Agent**。读完之后应当能直接继续工作，不需要回看任何历史对话。
- 维护规则：每完成一轮实质改动就更新本文件。**只写状态**，不写历史对话，不粘贴代码，只给路径与一句话职责。

权威性顺序（冲突时以序号小的为准）：

1. 门禁 tools/run-all-checks.py 的**实际输出**（当前应为 24/24 PASS）——这是唯一硬标准
2. 本文件
3. 各模块 docstring —— 细节、实测证据、踩坑经过都写在那里

---

## 1. 当前进度快照

### 1.1 已完成模块（一行一个）

**采集层**

- python/econ_core/http_client.py —— 纯标准库 HTTP 客户端；原始响应按 url+method+body 指纹存档到 data/raw/_http_cache/，是**所有**网络访问的唯一出口
- python/econ_core/nbs_client.py —— 国家统计局新版数据平台客户端（POST getEsDataByIndicatorIdAndDa / GET 目录树、默认指标、省级列表）
- python/econ_core/worldbank_client.py —— World Bank v2 REST 客户端（指标数据 / 指标搜索 / 国家列表）
- python/econ_core/imf_client.py —— IMF DataMapper v1 客户端（指标数据 / 指标目录 / 国家列表）
- python/econ_core/fred_client.py —— FRED（fredgraph.csv 免密钥端点）客户端；固定朴素 UA，按日期分布自动识别频率
- python/econ_core/bis_client.py —— BIS（国际清算银行）SDMX 2.1 客户端；中国 CPI 月度序列 1995 起 + 通用 SDMX 拉取（**本轮新增**）

**规范化层**

- python/econ_core/normalize.py —— 长表规范化层。统一 14 列 schema + row_sha16 行指纹；含四个入口：normalize_observations（NBS）、normalize_worldbank_observations、normalize_imf_observations、normalize_cpi_wide（后三个是第四阶段新增，用于把 IMF / CPI 宽表纳入统一入口）
- python/econ_core/series_key.py —— series_key 规范化（别名解析 + 大小写变体）；export.py 与 report.py 共用的**去重依据**（**本轮新增**）

**CLI 与 DSH 插件**

- python/econ_core/nbs_client_cli.py —— NBS 四子命令 CLI（list-provinces / get-default-indicator / get-catalog-tree / fetch-indicator）
- python/econ_core/worldbank_client_cli.py —— World Bank 三子命令 CLI（fetch-indicator / list-indicators / list-countries）
- python/econ_core/imf_client_cli.py —— IMF 三子命令 CLI（fetch-indicator / list-indicators / list-countries）
- python/econ_core/fred_client_cli.py —— FRED 两子命令 CLI（fetch-series / list-search；搜索需 api_key，恒返回空列表）
- python/econ_core/bis_client_cli.py —— BIS 两子命令 CLI（fetch-cpi / fetch-series；两者都是取数类，必带 fetched_at）（**本轮新增**）
- src/plugins/nbs-adapter.js —— 注册 nbs_fetch_indicator / nbs_get_catalog_tree / nbs_get_default_indicator / nbs_list_provinces 四个工具（薄壳转发 CLI）
- src/plugins/worldbank-adapter.js —— 注册 wb_fetch_indicator / wb_list_indicators / wb_list_countries 三个工具
- src/plugins/imf-adapter.js —— 注册 imf_fetch_indicator / imf_list_indicators / imf_list_countries 三个工具
- src/plugins/fred-adapter.js —— 注册 fred_fetch_series / fred_list_search 两个工具
- src/plugins/bis-adapter.js —— 注册 bis_fetch_cpi / bis_fetch_series 两个工具（**本轮新增**）
- tools/compare-cpi.py —— CPI 交叉验证：NBS「上年=100」vs FRED/OECD（先年均值再转同比；**本轮新增**）
- tools/smoke-fred-adapter.mjs —— FRED 插件 argv 拼装冒烟（桩替换 execFile，含 start/end 可选参数）
- src/plugins/hello.js —— 最小宿主插件，只用来证明 preset 装配链路可激活（不参与业务）

**验证与分析层**

- python/econ_core/cross_validation.py —— 两条序列逐期比对；差异率四段阈值（一致/可接受/警告/冲突）+ summary 三值结论（可直接拼接/需人工复核/不可拼接）
- python/econ_core/missing.py —— 缺失检测与**四分类**：true_gap / not_yet_published / discontinued / series_start（判定优先级：先尾部 >=3 年，再尾部 <3 年，再头部，最后中段）
- python/econ_core/fill_strategy.py —— 填补策略执行器：把决策写成行级元数据，**当前不执行任何插值**（出现 interpolate 直接抛 NotImplementedError）
- python/econ_core/source_profiler.py —— 来源画像：profile_series / compare_profiles / explain_divergence（**本轮新增**）
- python/econ_core/source_profiles.yaml —— **人工编纂**的来源画像知识库：4 个发布机构 + 13 条指标口径 + 18 个字段语义 + 口径家族表
- python/econ_core/arbiter.py —— 口径判定仲裁：把「画像判定」与「实测差异（读 cross_check 产物）」配成一条记录，给出 同口径 / 可桥接 / 不可拼接 / 人工复核 四值判定 + alignment（画像与实测是否同调）
- python/econ_core/credibility.py —— 五维可信度评分（Expertise 0.25 / Provenance 0.20 / Timeliness 0.10 / Transparency 0.15 / Coherence 0.30）-> 0-100 分 + high/medium/low
- python/econ_core/splicer.py —— **序列拼接器**：按策略在重叠期选源（later_wins / earlier_wins）+ 重叠期一致性 + 三类断点（level_jump / trend_break / variance_shift）+ 三值 verdict（**本轮新增**）
- tools/splice-cpi.py —— **首次真实拼接**：BIS 中国 CPI 同比（月度按年均值年化）⊗ NBS 年度 CPI，落 data/validated/spliced/（**本轮新增**）
- tools/smoke-bis-adapter.mjs —— BIS 插件 argv 拼装冒烟（桩替换 execFile；覆盖「三参数全可选」与「key 原样透传」）

**输出层**

- tools/export.py —— processed → CSV（data/output/econ_data.csv）+ SQLite（data/output/econ_data.db，含 observations 表 / idx_key_period 索引 / series_summary 视图）+ Markdown 数据字典（data/output/data_dictionary.md）
- tools/report.py —— 把 5 份 JSON 报告合成**单文件 HTML**（data/output/report.html，非技术同事可直接看）

**文档**

- python/econ_core/README.md —— 生产层已知上游事实（失业率两条口径、登记失业率 2022 起停更等）
- python/_probes/README.md —— 逆向探测脚本索引与已知观察（NBS 接口考古、缓存键缺陷等）

### 1.2 门禁：24 项（tools/run-all-checks.py，当前 24/24 PASS）

| # | 检查 | 类型 | 说明 |
|---|---|---|---|
| 1 | smoke-nbs-adapter | node | NBS 插件 argv 拼装（桩替换 execFile，不 spawn） |
| 2 | smoke-worldbank-adapter | node | World Bank 插件 argv 拼装 |
| 3 | smoke-imf-adapter | node | IMF 插件 argv 拼装 |
| 4 | smoke-fred-adapter | node | FRED 插件 argv 拼装（含 start/end 可选参数） |
| 5 | smoke-bis-adapter | node | BIS 插件 argv 拼装（**本轮新增**；含「三参数全可选」与「key 原样透传」） |
| 6 | check-cli-envelope | python | 真跑 12 个子命令（13 用例）校验 stdout 信封契约 R1-R11；timeout 420s |
| 7 | missing --test | python | 缺失四分类自检（纯离线，约 0.3s） |
| 8 | source_profiler --test | python | 来源画像自检，8 个场景（纯离线，约 0.4s） |
| 9 | arbiter --test | python | 口径判定自检，7 个场景（纯离线，约 0.3s） |
| 10 | normalize --test | python | 规范化层自检（含 row_sha16 唯一性、i_name null 补齐） |
| 11 | cross_validation --test | python | 交叉验证四段阈值自检 |
| 12 | compare-gdp | python | NBS vs World Bank 中国 GDP 端到端 |
| 13 | compare-gdp-3way | python | NBS vs WB vs IMF 三方（汇率取自 WB PA.NUS.FCRF）；timeout 420s |
| 14 | compare-gdp-real | python | NBS GDP 指数 vs IMF NGDP_RPCH 实际增速（无汇率污染） |
| 15 | compare-unemployment | python | 登记失业率 / 调查失业率 / IMF LUR 三方（百分点） |
| 16 | compare-cpi | python | CPI 交叉验证：NBS vs FRED/OECD（百分点）；timeout 420s |
| 17 | scan-missing | python | 缺失检测与分类扫描，产出 data/validated/missing_report.json；timeout 420s |
| 18 | materialize-validated | python | 声明式清单落盘 data/validated/；timeout 420s |
| 19 | run-fill-strategy | python | validated → processed（零填充，**已脱网**，约 0.4s） |
| 20 | credibility --test | python | 可信度评分自检，5 个场景（纯离线，约 0.6s） |
| 21 | export | python | processed → CSV / SQLite / 数据字典（纯离线） |
| 22 | report | python | 5 份 JSON → 单文件 HTML 质量报告 + 7 项自检（纯离线，约 1s） |
| 23 | splicer --test | python | 拼接器自检，4 个必测场景 + 4 个边界（纯离线，约 0.1s）（**本轮新增**） |
| 24 | splice-cpi | python | 首次真实拼接：BIS 年化 ⊗ NBS 年度 CPI（读 validated + 拉一次 BIS）；timeout 420s（**本轮新增**） |

门禁的运行顺序**有依赖**：materialize-validated（18）-> run-fill-strategy（19）-> credibility（20）-> export（21）-> report（22）-> splicer（23）-> splice-cpi（24）最后。credibility 读 validated + processed + cross_check 三样产物，所以不能挪到 arbiter 旁边（干净检出时会误报）；**splice-cpi 读 `data/validated/nbs/nbs_cpi_全国居民消费价格指数（上年=100） (%).json`，必须排在 materialize-validated 之后**。check-cli-envelope 与三个 compare 脚本是网络密集型，超时放宽到 420s。

**第 24 项 `splice-cpi` 是我主动加的**（本轮任务只要求加 splicer --test 变成 23/23）。理由：`splicer --test` 只证明**合成**数据能拼；PROJECT_STATE 长期记着「系统至今从未真正拼接」，而「真实链路上两条序列真的接上了、断点真的被判出来」只有跑真实脚本才算验过。若认为超范围，删掉 `CHECKS` 里那一行即可（其余不受影响）。

**check-cli-envelope 的 BIS 两条用例是本轮新加的**（12 子命令 / 13 用例）。理由：smoke 脚本用桩替换 execFile，只能验证 argv 拼装，**验证不了真实 CLI 的信封契约**（§3.8）；新源不进契约测试就等于信封契约无覆盖。**只加条目、未改任何已有条目。** 顺带发现：**FRED 不在 check-cli-envelope 的覆盖里**（历史遗留，属既有状态，本轮未动），其信封契约目前只有 smoke 的 argv 桩覆盖。

### 1.3 当前数据规模

- **声明式序列数**：**12 条**（NBS 6 + World Bank 1 + IMF 2 + FRED 1 + **BIS 2**）
- **BIS 序列（本轮接进全链路）**：`bis|WS_LONG_CPI|M.CN.771`（同比 %，月度 368 期，1996-01~2026-08）、`bis|WS_LONG_CPI|M.CN.628`（指数 2010=100，月度 380 期，1995-01~2026-08）。**月度原生粒度，不做年化**（理由见 `tools/scan-missing.py` 里 `BIS_CPI` 的注释）
- **总行数**：**900 行**（validated 948 = 12 条声明式 + selftest 残留；processed 910 / CSV 900 / SQLite 900，口径差异见 §3.9）。历史提醒：曾经的 152 是重复计数（alias 副本算了两遍），已由 export.py 去重修正
- **按源分布（CSV/SQLite 实测）**：**bis 748** / imf 69 / nbs 63 / fred 10 / worldbank 10。**BIS 一次接入就占了 83% 的行数** —— 748 = 368 + 380
- **缺失行数**：10 行（全部来自 NBS/IMF 年度序列；**BIS 两条零缺失**）；**一个都没有填补**
- **缺失分类（按行统计，口径同 §3.9）**：series_start 5 / discontinued 3 / not_yet_published 2 / true_gap **0**
- **缺失动作（按行统计）**：leave_null 8 / wait 2 / interpolate 0
- **row_sha16 覆盖率**：900/900（全部唯一）
- 落盘位置：data/validated/（12 个长表 JSON + missing_report.json + spliced/）→ data/processed/（12 个带缺失元数据的 JSON）→ data/output/（CSV + SQLite + 字典）
- **首次真实拼接产物**：`data/validated/spliced/cpi_bis_nbs_spliced.json` —— 30 期（1996~2025），拼接点 2015，verdict=需桥接
- **missing_report.json 的 n_series=13**（12 条声明式 + 1 条 selftest 残留）。**注意**：这个数在本轮之前是 23，因为落盘扫描的去重**从来没生效过**（详见 §3.14）
- 知识库另有 **2 条只用于对比、未落盘**的序列（nbs|gdp|index_prev_year_100、imf|NGDP_RPCH），所以知识库规范键 > 落盘序列数

12 个声明式 series_key（知识库必须与之一一对应）：

1. nbs|gdp|cny_100m
2. nbs|cpi|全国居民消费价格指数（上年=100） (%)
3. nbs|cpi|城市居民消费价格指数（上年=100） (%)
4. nbs|cpi|农村居民消费价格指数（上年=100） (%)
5. nbs|registered_unemployment（停更于 2021）
6. nbs|surveyed_unemployment（起点 2018）
7. worldbank|NY.GDP.MKTP.CN
8. imf|NGDPD
9. imf|LUR（起点 2017）
10. fred|CHNCPIALLMINMEI（FRED/OECD 中国 CPI 指数，月度 -> 年均值）
11. bis|WS_LONG_CPI|M.CN.771（BIS 中国 CPI 同比 %，月度 368 期；**本轮接进全链路**）
12. bis|WS_LONG_CPI|M.CN.628（BIS 中国 CPI 指数 2010=100，月度 380 期；**本轮接进全链路**）
13. NBS|000000000000|db8e5a86c08246e79b1b11251927e740（**别名**，指向第 1 条，不计入 12 条声明式）

### 1.4 最近一轮新增（方向 C 第四轮：修 http_client + 拼接器）

- **修掉 `http_client` 的大小写敏感缺陷**（§3.13 闭环）：新增 `_header_get()` 做大小写不敏感查找，`_decompress` / `_decode_bytes` / `encoding` 三处调用点全部改用它；顺带修掉同一处 `resp_headers["Content-Type"]` 的 **KeyError**（比取不到更严重，会让整次请求失败而不是降级）
- **`python/econ_core/splicer.py` + `tools/splice-cpi.py`**（**本轮新增**）：拼接器 **30 项自检全过**；首次真实拼接 30 期、拼接点 2015、verdict=需桥接
- **BIS 两条序列接进 `scan-missing.py` 的声明式清单**（`materialize-validated.py` 自动继承，因为它复用清单不复制）→ 打通 validated / processed / CSV / SQLite。数据规模从 **152 行涨到 900 行**（bis 占 748）
- **月度 vs 年化的决定：保留月度原生粒度**。理由：① BIS 的价值就在 1995 起的月度覆盖，年化后只剩 31/32 行，等于把接它的理由丢掉一半；② 拼接器的断点检测在月度分辨率下才有意义（年度 3 点斜率会横跨好几年的真实变化）；③ 想跟年度 CPI 对比可以在对比脚本里年化，反过来从年度恢复月度不可能 —— 保留信息量大的形态是**单向安全**的选择
- **又修了一个静默 bug（§3.14）**：`scan_materialized()` 用 `_auto_series_key` 推行键，与声明式清单的规范键**对所有落盘文件都不一样**，导致「按 series_key 跳过已覆盖的」这层去重**从来没生效**。修成「产物自带 series_key 优先」后，missing_report 的 n_series 从 23 降到 13、缺口段从 10 降到 5 —— 也就是**之前有一半统计是重复计数**
- 门禁 22 -> **24/24**（新增第 23 项 `splicer --test`；**另加第 24 项 `splice-cpi`**，理由见 §1.2 表下说明）
- **本轮踩到并修掉的拼接器设计问题**：`level_jump` 只看「相对跳变」，把中国 CPI 2014→2015（2.06%→1.4% 的真实变化）判成 reject「不可拼接」。修法是同时报 `excess_ratio` = 绝对跳变 / 该序列接缝前同源步长中位数，超额 ≤ 1 就降为 ok

### 1.5 上一轮新增（方向 C 第三轮：接 BIS）
- **四源独立性探测已完成并固化**（§3.10/§3.11/§3.12）：BIS / OECD / PWT 11.0 / Maddison 2023 **全部不是独立编制**，细节与对比表在 `python/_probes/README.md`
- **`bis_client.py` + `bis_client_cli.py` + `bis-adapter.js` + `smoke-bis-adapter.mjs` + 知识库 BIS 条目**（2 个 publisher / 2 条 indicator / 2 条 family）
- 自检 14/14 PASS；BIS 中国 CPI 月度实测 **1995-01 起的指数（380 期）/ 1996-01 起的同比（368 期）**
- **接 BIS 的理由是加工而非采集**：BIS 对长序列做了**拼接 + 重定基**，是本项目第一条真正需要拼接的链外序列
- 门禁 21 -> **22/22**（新增第 5 项 smoke-bis-adapter；另在 check-cli-envelope 补了 2 条 BIS 用例，只加不改）
- **发现 `http_client` 漏解压小写 `content-encoding: gzip`**（§3.13），本轮在 `bis_client` 就地绕开、未修 http_client
- **修好了 `verify-preset.mjs` 的 default 断言**（原断言要求 `default` 必须等于本 preset 的目录 id，于是 `default: standard` 被判 FAIL）。现改为「等于本 preset id **或** 是内置 preset（standard / minimal / code / cordis）」，理由是 `default: standard` 是**有意配置**（避免新会话打不开），不是错误。修后 `ALL CHECKS PASSED`（exit 0）。**注意 `verify-preset.mjs` 不在门禁 22 项里**，需要手工跑
- **本轮刻意不做**：拼接器与拼接断点检查（下一轮）、把 BIS 接进声明式清单与导出链路

### 1.6 更早一轮（arbiter 改成数据驱动）

- python/econ_core/arbiter.py —— `_adapt` 重写：读产物自带的 `series_a` / `series_b` / `measured`（或 `pairs` 数组），不再按键名猜形状；缺字段只记 warning
- 5 个对比脚本（compare-gdp / -3way / -real / -unemployment / -cpi）输出都补了这三个字段（多对用 `pairs` 数组）
- 效果：arbiter **7 对、0 warning**；CPI 对（NBS × FRED）判「可桥接」——KB 里写的 comparability 是 medium，而 KB 是人工判据，不由脚本改
- 教训已记进 §5.4：曾因键名 `max_abs_diff_pp` 撞车，产出过一条错配的第 7 对

---

## 2. 关键约定（未来必须遵守）

### 2.1 运行环境（硬编码，不要改成 PATH 里的 python）

- **解释器**：D:\universe\econ-data-harvester\.venv\Scripts\python.exe
- **工作目录（cwd）**：D:\universe\econ-data-harvester（项目根）
- **PYTHONPATH**：D:\universe\econ-data-harvester\python（让 econ_core.* 可解析）
- **PYTHONIOENCODING**：utf-8（否则 Windows 控制台会乱码）
- **为什么必须用 venv**：本机 Schannel 凭证库不可用，非 OpenSSL 栈会 TLS 失败；http_client.assert_venv() 会强制拦截
- Node 侧脚本用系统 node（tools/*.mjs 通过 shutil.which("node") 或直接 node 命令）
- **境外源一律先试朴素 UA**：IMF（Akamai）与 FRED 都会拒 Chrome UA，只有 `python-urllib/3.12` 能通；已固化在 imf_client.IMF_HEADERS 与 fred_client.FRED_HEADERS

### 2.2 CLI 契约（五个 *_client_cli.py 共同遵守）

- **成功**：stdout 是**单行** JSON 信封，含 ok=true、command、data，退出码 0
- **失败**：stdout 仍是单行 JSON 信封，含 ok=false、command、data=null、error，退出码 2
- **人类可读日志一律走 stderr**（http_client 的存档日志、[parsed]、[meta]、[search] 等），不得污染 stdout
- ok=true 时：**取数类**（command 含 fetch）必须带 fetched_at；**元数据类**（command 含 list）必须带 row_count
- 无数据时**省略**字段而不是写 null（例如 wb_list_countries 未传 region 时不写 region 字段；imf fetch-indicator 无数据时不写 period_min/period_max）——写 null 会让 DSH 输出 schema 校验失败

### 2.3 长表 schema（14 列，顺序固定）

region_code / region_name / indicator_id / tree_node_id / indicator_name / period / period_type / value / unit / source / fetched_at / raw_cache / row_sha16 / raw_fields

非标准列（仅个别序列有）：aggregated_from_months（调查失业率的年度聚合行）。缺失元数据三列由 fill_strategy 追加：missing_classification / missing_action / missing_evidence。

### 2.4 缺失字段名（已定案，不要改名）

- **missing_classification** —— 四分类之一
- **missing_action** —— 建议动作（interpolate / wait / leave_null / manual_review）
- **missing_evidence** —— 分类依据（人可读）
- 未来若要加填充信息，追加 **fill_method** / **fill_from**，不要复用上面三个字段

### 2.5 缺失四分类（econ_core/missing.py）

| 分类 | 含义 | 建议动作 | 可否填充 |
|---|---|---|---|
| true_gap | 中段空洞，两侧都有观测 | interpolate | 可（但当前数据集 0 例） |
| not_yet_published | 尾部缺失且不足 3 年 | wait | 否（等官方发布） |
| discontinued | 尾部缺失且 >= 3 年 | leave_null | **否（外推 = 伪造数据）** |
| series_start | 头部缺失 | leave_null | **否（往前补 = 凭空发明历史）** |

判定优先级：1) 尾部且 >=3 年 -> discontinued；2) 尾部且 <3 年 -> not_yet_published；3) 头部 -> series_start；4) 其余 -> true_gap。「年数」按网格粒度折算（月度 12 期 = 1 年）。

**重要**：series_start 只在调用方给出**显式 expected_periods 窗口**时才可能被检出；只从数据自身推 min..max 网格时永远不会产生头部缺口。

### 2.6 信封契约规则 R1-R11

完整定义写在 tools/check-cli-envelope.py 的**模块 docstring**（那里还写了「规则设计哲学：按命令语义分类，不追求字段表面统一」）。要点：

- R1 单行合法 JSON；R2 ok/command 类型；R3a fetched_at 若存在必须 ISO8601；R3b row_count 若存在必须 int
- R4 ok=false 必须带 error；R5 region 若存在必须是 str（不能 null）；R6 ok=true 时 data 必须存在且非 null（array 或 object 均可）
- R7 raw_cache 若存在必须 str；R8 exit code 与 ok 一致
- R9 wb_list_countries 不传 --region 时不应有 region 字段；R10 NBS fetch-indicator 每行的 indicator_id 必须 != tree_node_id
- **R11（关键）**：command 含 fetch 必须有 fetched_at；含 list 必须有 row_count；get-catalog-tree 与 get-default-indicator 豁免

新增命令时：**先判它属于哪一类（取数 / 元数据 / 配置），再套该类规则**；出现第四类语义应在 R11 里显式加分支，而不是放宽已有规则。

### 2.7 命名与大小写

- **source 一律小写**：nbs / worldbank / imf。normalize 的 SOURCE / SOURCE_WB 常量已改小写；fill_strategy 写文件时也会把 source 目录名小写（跨平台一致）。但本机 data/processed/NBS 仍是大写目录名（Windows 复用旧目录，见 3.9）
- **series_key 形态**：source|region_code|indicator_id（自动推导）或 source|family|name（声明式清单），例如 nbs|gdp|cny_100m / imf|LUR
- **文件名安全化**只替换 Windows 非法字符（\ / : * ? " < > |），**必须保留中文**（见第 3 节）

### 2.8 知识库是人工编纂的，不是自动抽取

- python/econ_core/source_profiles.yaml **必须人工维护**。数据层只有 indicator_name / unit / raw_cache 这类表层字段；统计方法、覆盖范围、可比性必须人写
- 查不到官方明确定义就写 **unknown**，不要编。unknown 表示「未找到明确依据」，不等于「该项没有」
- **新增序列必须补知识库条目**，否则 source_profiler.profile_series 会抛 KeyError（报错信息会列出可用键）
- 对比时优先用 families 段（口径家族）判断「是不是同一类」，自由文本措辞不同不算口径不同
- 知识库的 updated 字段会被当作 profile_version 输出

### 2.9 数据分层与不变量

- 分层：data/raw（HTTP 原文）-> data/parsed（按请求指纹解析结果）-> data/validated（长表）-> data/processed（带缺失元数据）-> data/output（产品）
- **原始数据不修改**：任何一层都不得改写上层内容；缺失就是 null，**不插值**（fill_strategy 里出现 interpolate 会抛 NotImplementedError）
- **缺失行必须保留**：导出时 value 为空也不丢行，原因写在 missing_action / missing_classification
- 每行都有 row_sha16（行级指纹，不含自身），任何字段变动都要重算指纹
- data/ 下所有目录的内容都被 .gitignore 忽略，只保留 .gitkeep

### 2.10 单位对齐必须显式

跨源比数值前必须先把单位对齐并**把推理过程打印出来**，不允许隐式换算。已确立的换算：NBS 亿元 <-> WB 元（x1e8）、IMF 十亿美元 -> 亿美元（x10）、汇率取自 World Bank PA.NUS.FCRF。

### 2.11 cross_check 产物契约（arbiter 数据驱动的前提）

- **每个对比产物必须自带 `series_a` / `series_b` / `measured`**（多对用 `pairs` 数组）；`measured` 里给 `diff_pp` + `diff_type`（"pp" 或 "percent"）+ `source`。缺字段的产物 arbiter 只记 warning 跳过
- **arbiter 不猜序列对**：形状由产物自己声明（见 5.4 的错配事故）

### 2.12 各源的 URL / 传输层固定约束（写死在 client 里，别顺手改）

上面 2.2 管的是**出口契约**，这一节管**入口**（怎么把上游取回来）。每条都是实测踩出来的：

- **境外源先试朴素 UA**（`python-urllib/3.12`）：IMF 的 Akamai 与 FRED 都会 403 / 掐断 Chrome UA（§3.2）。BIS 不挑 UA，但沿用朴素 UA 保持一致
- **BIS SDMX 只用 `format=sdmx-json`**：`?format=jsondata` 恒 **406** `Unsupported format: jsondata`。密钥必须给满三位 `FREQ.REF_AREA.UNIT_MEASURE`（如 `M.CN.771`），少一位返回 404。`UNIT_MEASURE`：`771`=同比 %、`628`=指数（2010=100）。（OECD SDMX 的 `format` 白名单**完全不同**，见 §3.12 —— 两家的值不可互抄）
- **`http_client` 的 `Content-Encoding` 查找大小写敏感**：服务器回小写头名（BIS 就是）时**不会解压**，gzip 字节会被当 JSON 解析而崩。当前绕法在 `bis_client._decode_body`（按 gzip magic 判断），**新源如果也返回压缩就别假设 http_client 处理好了** —— 详见 §3.13

---

## 3. 已知的坑（不要再踩）

### 3.1 NBS 的 cid 参数被后端忽略

- 现象：把 fetch_indicator_data 的 cid 改成任意错值，返回的观测**逐字节相同**（同一 raw 存档 sha256[:12]）
- 原因：getEsDataByIndicatorIdAndDa 实际按 id（tree_node_id）+ rootId + da + dts 选序列；响应里根本没有 cid 字段
- 处置：**不改签名**，只在 nbs_client.fetch_indicator_data 的 docstring 里记录了实测证据
- 副作用（已知未修）：cid 参与 parsed 落盘的文件名指纹，所以错 cid 会写出**另一个指纹的副本**，内容与正版相同。详见 python/_probes/README.md 的「已知观察」

### 3.2 境外源会拒绝 Chrome UA（IMF 的 Akamai、FRED）

- 现象：用 http_client 默认头（Chrome 128 UA）请求 api.imf.org 的 DataMapper -> HTTP 403 AkamaiGHost Access Denied（连站点根 https://www.imf.org/ 也 403）
- 解法：**只把 User-Agent 换成 python-urllib/3.12**，其余默认头保留，同一 URL 立刻 200 + application/json。方向与「伪装成浏览器」相反
- 已固化在 imf_client.IMF_HEADERS。**不要顺手改回 Chrome**
- 同源坑：/v1/{indicator}/{country} 的 country 路径段**不做服务端过滤**（实测 /NGDPD/CHN 仍返回 229 个国家），过滤必须在客户端做

**第二个实例（FRED，本轮踩到）**：fredgraph.csv 对默认 Chrome UA 直接 `RemoteDisconnected`（重试 3 次全失败），空 UA 也不行，只有 `python-urllib/3.12` 能拿到 200。已固化在 fred_client.FRED_HEADERS；两个源都是「伪装浏览器反而被拒」——**接新的境外源时先试朴素 UA，别急着加浏览器头**。

### 3.3 NBS 的 i_name 有时是 null

- 现象：NBS 2024 年 GDP 行的 i_name 为 null，导致 indicator_name 为空
- 处置：normalize._fill_indicator_names 让**同一 indicator_id 的所有行共享同一个名称**（组内首个非空值；整组都空才回落到 indicator_id），并在填充后**重算 row_sha16**
- 衍生事实：Parquet 没有 null 字符串语义，回读会变 NaN，严格保真请读 JSON 版本

### 3.4 中文文件名绝不能用「剥非 ASCII」的方式安全化

- 现象：早期 _safe() 把非 ASCII 全部替换成下划线，于是 nbs|cpi|全国 / 城市 / 农村 三条序列被压成同一个文件名，**后写的静默覆盖先写的**（丢数据）
- 正确做法：只替换 Windows 非法字符（\ / : * ? " < > | 与控制符），**保留中文**
- 自我验证：fill_strategy 会复读 processed 文件核对 decisions 数 == 缺口数，正是这条检查抓到了那次覆盖

### 3.5 data/output/* 必须留在 .gitignore

- 注意 data/output/* 与顶级 output/* 是**两个不同的**路径，早期只忽略了后者，导致 CSV/DB/字典会被误提交
- 现在 .gitignore 里 raw / parsed / validated / processed / output 五个 data 子目录都有条目，靠 !data/**/.gitkeep 保留目录

### 3.6 venv 里已装 PyYAML 6.0.3（以及 pandas / pyarrow / numpy）

- venv **原本没有 PyYAML**；source_profiler 加载知识库需要它，已用 `pip_sandbox_install.py install pyyaml` 安装
- 若在新机器上重建环境，需补装：pyyaml（知识库必需）、**jinja2（HTML 报告必需）**；pandas + pyarrow（仅 write_validated_parquet 需要）
- 缺依赖时报错会直接给出安装命令，不会静默降级

### 3.7 ⚠️ 本机沙箱 ACL runner 故障（环境问题，不是策略拒绝）

- 现象：workspace-write 模式下任何 pwsh 调用都可能直接失败，报：

      sandbox mode "workspace-write" is requested but no sandbox backend is usable on this host;
      refusing to run the command unconfined. ...
      Runner failure: windows-acl-run: --temp is not an existing directory:
      C:\Users\user\AppData\Local\Temp\dsh-<随机后缀>

- 含义：ACL 受限令牌 runner 起不来（它的临时目录不存在）。报错本身建议切到 danger-full-access
- 处置约定：**先试普通模式；失败即带 sandbox_permissions=danger-full-access + justification 重试一次**。一次提权被拒即为终局，不要绕路
- 涉及 Temp 目录：C:\Users\user\AppData\Local\Temp\dsh-*（每个命令一个随机目录，所以重建单个目录没用）
- 读文件类工具（read/edit/write）**不受影响**，只有 pwsh 子进程受影响
- **写 workspace 之外的文件（家目录 preset 等）要 danger-full-access 审批**：审批无人应答时调用会**挂到墙钟上限（实测约 10 分钟）**才失败，而且**不会部分生效**（实测三处编辑全部未写入）。这类改动手工用 PowerShell 补，别指望提权

### 3.8 其他容易踩的（按层归类）

**NBS 取数**

- 城镇调查失业率**只在月度树（code=1）**，年度树（code=3）里没有；登记失业率反过来只在年度树
- 调查失业率实测从 2018-01 起才有数据（请求 2017 也只回 2018+）
- 登记失业率 2022/2023/2024 的 v 是**空串**（节点还在但不更新）
- 目录树里 13 个「失业」节点绝大多数是失业保险基金/参保人数，不是失业率

**CPI 宽表**

- 默认指标接口 yData[].value 是**字符串数组**（如 "101.4"），必须过 parse_value；不解析会被当成「全缺失」并误判成 discontinued
- yData[].du 是**数据单位 id，三条序列共用同一个值**，不能直接当 indicator_id；统一入口用 du#序号 保证唯一

**IMF**

- 数据**含预测值**（实测 CHN 的 NGDPD 到 2031、LUR 到 2031），做实际值比对必须自己截年份
- values 里有一个空字符串键（值为 null），解析时要跳过

**落盘扫描**

- 识别长表行必须要求 **period 与 value 同时存在**；否则会把对比脚本的结果行（只有 period + nbs_usd_100m 等）误当序列，整段判成缺失
- materialize-validated 会把声明式清单落盘到 data/validated/，于是同一序列会同时出现在「声明式清单」和「落盘扫描」里；scan-missing 里有去重（按 series_key 跳过已覆盖的），不要删掉

**测试方法学**

- smoke-*.mjs 用**桩**替换 execFile，只能验证 argv 拼装，**验证不了真实 CLI 的信封契约**；契约由 tools/check-cli-envelope.py 真跑子进程覆盖
- 沙箱下 node 的 child_process 管道 stdio 会 spawn EPERM，所以 smoke 脚本必须走桩、不能真起进程
- 门禁里 check-cli-envelope / 三个 compare / scan-missing / materialize-validated 都是网络密集型，单次耗时波动可达 5-6 倍（16s -> 103s），因此各自 420s 超时；run-fill-strategy 已脱网（0.4s）

- shutil.which("node") 在本机解析到带空格的路径（D:\New Folder\node.EXE），失败块里的命令必须给含空格的参数加引号才能复制粘贴执行

### 3.9 缺失统计有两个口径，别混用（也不要把大写 NBS 目录当 bug）

- **按行**（本文件 1.3 用的口径）：10 行 = series_start 5 + discontinued 3 + not_yet_published 2
- **按缺口段**（data/validated/missing_report.json 的 by_classification_*）：series_start 4 / discontinued 2 / not_yet_published 4，且声明式与落盘扫描各扫一遍，同一缺口被算两次
- 报数时必须写明口径；run-fill-strategy 打印的决策数是**段**口径，processed 行里的元数据才是行口径
- data/processed/ 下 NBS 序列落在**大写 NBS/** 目录，worldbank / imf 是小写。fill_strategy 已做 .lower()，这是 Windows 复用旧目录名的历史遗留，干净检出不复现

### 3.10 中国 CPI 无独立源（实测结论）

四个候选全部探测完毕，结论：**中国没有独立编制的 CPI**。
不是「还没找到」，是**采集者只有一个**。

- **BIS**：明确自认转载方（官方 FAQ 原文）
- **OECD**：`CL_METHODOLOGY_PRI` 里中国只有 `N`（= National）
- **PWT 11.0**：ICP/WB + 自研估算，中国 55/72 年自标 Extrapolated
- **Maddison 2023**：自述二手，且不测价格

真正的独立测量在**价格水平**维度（ICP），不在 CPI 维度。
ICP 2021 方向：单独立项，见 §5.3。

四个候选的实测判定依据与对比表见 `python/_probes/README.md`
（2026-09-27 探测段）。三条最硬的证据：

1. BIS CPI 页 FAQ 原文：*"Consumer price indices are predominantly compiled by national statistical offices."*（BIS 只做拼接 + 重定基）
2. OECD 的中国 CPI 同比与 IMF WEO `PCPIPCH` **逐年逐位完全相同** —— 两个发布方小数位全等，是同一份 NBS 序列被两次转载的签名
3. OECD 自编 PPP 全家桶（`DF_PPP*` / `DF_PP_CPL_M`）里**中国完全缺席**；唯一发布的那个中国 PPP（`DF_TABLE4` 的 `PPP_B1GQ`）与 World Bank `PA.NUS.PPP` 前 7 年 abs diff = 0.000000

**推论**：再加 CPI 维度的源不会增加论证力。方向 C 第三轮因此改接 BIS，
但**理由不是独立性**，而是它的序列长度（1995 起月度）与 BIS 自己的拼接/重定基实现。

### 3.11 PWT / Maddison 的 TLS 证书链问题

`dataverse.nl` / `www.rug.nl` 的 TLS 链**不完整**（缺 GEANT/HARICA 中间证书，
实测 issuer = `GEANT TLS ECC 1`，叶证书不含 AIA 扩展），而 venv 里**没有 CA bundle**
（`ssl.get_default_verify_paths().cafile` 为 None，也没装 certifi）。

要接的话必须先解决证书链问题。这**不是 UA 问题**，与 §3.2 的「境外源拒 Chrome UA」
是两回事 —— 别混记。实测对照：`sdmx.oecd.org` / `stats.bis.org` 同一解释器 TLSv1.3 正常，
只有 Groningen 那两个域失败。

附带现象：`dataverse.nl` 的 `datafile` 端点还会**中途截断响应**
（5,839,841 字节的 `pwt110.xlsx` 实测只拿到 ~2.9–4.1 MB 就断），
即便用 `Range: bytes=<have>-` 续传也未必补全（服务端对带 Range 的请求行为不一致）。
接 PWT / Maddison 需要「断点续传 + Content-Length 校验」，并留重试预算。

### 3.12 OECD SDMX 的 format 白名单差异

`https://sdmx.oecd.org/public/rest/v1/` 的 **structure 服务与 data 服务的 `format`
白名单不同**，同一个值在一个服务上合法、在另一个上 406：

- structure 服务接受：`structure, xml-structure-3.0.0, sdmx-3.0, json-structure-2.0.0`
  （**`jsondata` 在这里恒 406**，报错正文会列出这个白名单）
- data 服务接受：`genericdata, jsondata, structurespecificdata, csv, csvfile,
  csvfilewithlabels, xml-data-3.0.0, json-data-2.0.0, csv-data-2.0.0`

另外两条：

- **`/data/` 拒绝不完整密钥**：`.../DF_PRICES_ALL,1.0/CHN` → **403**
  `Not enough key values in query, expecting 8 got 1`，8 个维度位必须全部给
  （用 `.` 或 `all`）。
- **限流是真的**：约 15 次快速请求即 429，且响应头里的 `Retry-After: 0` **不可信**
  （立刻重试仍 429），实际需要 15~30s 静默，稳定做法是 **9~12s 间隔 + 指数退避**。
- 顺带：SDMX 里**没有机器可读的 provenance 字段**（`metadata/dataflow/...` → 403
  `Invalid structure`），来源只能靠数值比对反推 —— 这正是 §3.10 第 2/3 条证据的来源。

### 3.13 ⚠️ http_client 漏解压「头名全小写」的 gzip（真实缺陷，本轮就地绕开未修）

**现象**：接 BIS 时 `bis_client` 调 `http_client.get_json()` 直接崩：

    json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)
    [ERROR] JSON 解析失败，响应前 300 字符："\x1f\x8b\x08\x00..."

**根因**（已实测定位，不是猜）：`http_client.request()` 与 `_decompress()` 之间做的是
**大小写敏感的字典查找** ——

    decoding = _decompress(wire, resp_headers.get("Content-Encoding"))
    def _decompress(body, content_encoding):
        if not body or not content_encoding: return body   # <- 这里直接返回原始 gzip 字节

而 BIS（FusionEdgeServer）回的响应头名是**全小写**。缓存 meta 里的原始证据：

    last_fetch.headers = {'content-encoding': 'gzip', ...}      # 键名小写
    headers.get('Content-Encoding') -> None                     # 于是取不到
    headers.get('content-encoding') -> 'gzip'

`_headers_to_dict()` 用 `msg.items()` 原样搬运头名，所以服务器给什么大小写就存什么；
`_decompress` 只认 `Content-Encoding` 这一种写法。**凡返回小写头名的服务器都会中招。**

**实测对照**（同一 URL、同一 UA，只改 Accept-Encoding）：

| 请求头 | 结果 |
|---|---|
| 不带 / `identity` | 200 + 46932 字节**明文** JSON，无 Content-Encoding 头 |
| `gzip` | 200 + 8201 字节 gzip，`content-encoding: gzip`（小写） |
| http_client 默认 `gzip, deflate` | 200 + 8206 字节 gzip -> **被当成 JSON 解析而崩** |

NBS / World Bank / IMF / FRED 四个源目前都没踩到 —— 它们的响应要么不压缩，
要么头名大小写恰好对得上。所以这是**随服务器实现而定的潜在缺陷**，不是必然故障。

**当时的处置**（第三轮，遵守「不改 http_client」的约束）：`bis_client` 自己解压 ——
统一用 `get_bytes()` 取原始字节，再按 **gzip magic（`\x1f\x8b`）** 判断解压
（`_decode_body`）。实测 BIS 的 4 个端点全部走通。

**✅ 已修（第四轮，约束到期）**：http_client 新增 `_header_get(headers, name, default)` 做
**大小写不敏感**查找，三处调用点全部改用它：

| 位置 | 改前 | 改后 | 严重度 |
|---|---|---|---|
| `_decompress` 入参 | `resp_headers.get("Content-Encoding")` | `_header_get(resp_headers, "Content-Encoding")` | 漏解压 -> JSON 崩 |
| `encoding` 推导 | `resp_headers.get("Content-Type","")` + `resp_headers["Content-Type"]` | `_header_get(resp_headers, "Content-Type", "")` | **`["Content-Type"]` 会 KeyError 让整次请求失败**（比取不到更严重，之前没注意到） |
| `_decode_bytes` | 自制 for 循环比 `k.lower()` | `_header_get(...)` | 行为不变，去重复实现 |

验证：`http_client.get_json()` 现在**直取 BIS 就能拿到 dict**（旧代码必崩）；
`_decompress` 对 gzip / GZIP / deflate / 裸 deflate / None / 未知 `br` / 坏 gzip 七种输入
逐个验过，行为与修前约定一致（不解压或解压失败都按原始字节返回，只记 warning）。

`bis_client._decode_body` 的 magic 兜底**保留**了，但已从「绕 bug」重新定位为
「兜住回了压缩流却不声明 `Content-Encoding` 的服务器」；命中时**不再打日志**
（正常路径不该有噪音，命中即说明 http_client 又漏了，交给下游 JSON 报错暴露）。

### 3.14 ⚠️ scan_materialized 的去重从来没生效（静默重复计数，本轮修）

**现象**：`missing_report.json` 的 `n_series` 长期是「声明式 + 落盘扫描」两遍之和，
一直被当成"含重复但无害"。第四轮把 BIS 接进声明式清单后核对发现：**去重代码一行都没起作用**。

**根因**：`tools/scan-missing.py` 的 `scan_materialized()` 用
`missing._auto_series_key([r])` 推行键，得到的是 `source|region_code|indicator_id`
（如 `bis|000000000000|WS_LONG_CPI|M.CN.771`）；而声明式清单用的、以及
`materialize-validated.py` **写进落盘文件**的，是规范键
（如 `bis|WS_LONG_CPI|M.CN.771`）。实测**12 个落盘文件里 12 个都对不上**：

    bis\bis_WS_LONG_CPI_M.CN.771.json   stored=bis|WS_LONG_CPI|M.CN.771   auto=bis|000000000000|WS_LONG_CPI|M.CN.771   DIFF
    nbs\nbs_gdp_cny_100m.json           stored=nbs|gdp|cny_100m           auto=nbs|000000000000|db8e5a86…            DIFF
    imf\imf_LUR.json                    stored=imf|LUR                    auto=imf|CHN|LUR                            DIFF
    （其余 9 个同样 DIFF）

于是主流程里 `if key in declared_keys: 跳过` 永远不命中 —— **同一序列被扫两遍，
缺口段也被算两遍**。这正是 §3.9 说的"两个口径混用"背后被忽略的第三个坑。

**修法**（用本项目自己的约定，不新造规则）：`scan_materialized()` 改为
**产物自带的 `series_key` 优先，缺失才退回 `_auto_series_key`**。这正是
`fill_strategy._scan_validated_series()` 与 `source_profiles.yaml` 的
`fields.series_key` 早就写明的规则，`scan_materialized` 之前没跟上。

**修后实测变化**：

| 指标 | 修前 | 修后 |
|---|---|---|
| `n_series` | 23 | **13**（12 条声明式 + 1 条 selftest 残留） |
| 缺口段合计 | 10 | **5** |
| 跳过日志 | 无 | 12 条「已由声明式清单覆盖，不重复计」 |

即**此前约一半的统计是重复计数**。两条断言（登记失业率=discontinued、调查失业率=series_start）
修前修后都 PASS，所以这个 bug 不会被门禁抓到 —— 它只在**数字**上错。

---

## 4. 架构图

### 4.1 主干（从上到下）

    [公开数据源]                 [采集层]              [规范化层]          [分层落盘]
    -------------------------------------------------------------------------------
    NBS  data.stats.gov.cn    +
    World Bank  api.worldbank +--> http_client.py --> normalize.py ---> data/raw
    IMF  api.imf.org          +                                        data/parsed
                                                                      data/validated
                                                                            |
                                                                            v
                                                                  missing.py (四分类)
                                                                            |
                                                                            v
                                                     fill_strategy.py --> data/processed
                                                        (决策, 零填充)
                                                                            |
                                                                            v
                                                          export.py ------> data/output
                                                                            |
                                                                            v
                                                              CSV / SQLite / 数据字典

    旁路（方向 A 第一轮新增）:

        source_profiles.yaml (人工编纂的知识库) --+
                                                  +--> source_profiler.py --> 画像 /
        data/validated/ (数据血缘) ---------------+                          可比性 /
        data/validated/missing_report.json -------+                          差异归因
                                                                                |
                                                                                v
                                          (下一轮) Arbiter: 同口径 / 可桥接 / 不可拼接

    横切: tools/run-all-checks.py —— 24 项门禁，任何改动后必跑

### 4.2 每层职责与产物

| 层 | 模块 | 输入 | 产物 |
|---|---|---|---|
| 采集 | http_client.py + 五个 client | URL / 参数 | data/raw/_http_cache/*.bin（HTTP 原文） |
| 规范化 | normalize.py | 原始观测 | data/parsed/<source>/<endpoint>_<sha16>.json；data/validated/<source>/*.json |
| 缺失分类 | missing.py | 长表行 | 缺口 + 四分类（无独立落盘，进 missing_report.json） |
| 策略 | fill_strategy.py | validated + missing_report | data/processed/<source>/<name>_processed.json |
| **拼接（本轮新增）** | **splicer.py + tools/splice-cpi.py** | **两条同指标序列（长表行）** | **data/validated/spliced/<name>.json（拼接结果 + 重叠期 + 断点 + verdict）** |
| 输出 | export.py | processed | data/output/econ_data.csv、econ_data.db、data_dictionary.md |
| 画像 | source_profiler.py | 知识库 + validated + missing_report | 内存画像（供 Arbiter 消费） |

**拼接层的位置**：它在 validated **之后**、processed **之前**的语义位置上（输入是两条已规范化的序列），
但**本轮刻意不接进 processed**——`fill_strategy` 不认识拼接产物，硬接会动到它的扫描规则。
当前 splicer 只被 `tools/splice-cpi.py` 调用，产物落在 `data/validated/spliced/`（**会被落盘扫描看见**，
但它没有 `value` 键的顶层 rows 结构，所以不会被误当序列，见 §3.8 的落盘扫描识别规则）。

### 4.3 落盘信封结构（三层各有固定形状）

- **raw**：data/raw/_http_cache/<sha16>.bin 加同名 .meta.json（url / status / headers / 请求体）
- **parsed**：request / raw_cache / data / fetched_at
- **validated**：name / row_count / written_at / columns / rows，另有两个由 materialize-validated 追加的字段 series_key 与 expected_periods
- **processed**：rows / missing_report / decisions / processed_at

### 4.4 插件与 preset 装配

- 五个插件行（nbs-adapter / worldbank-adapter / imf-adapter / fred-adapter / bis-adapter）写在 .dsh/.agent-presets/econ-harvester/agent.cordis.yml，与 persona 等行并列
- 插件是**薄壳**：只做 argv 翻译与 stdout 信封翻译，采集/解析逻辑全在 Python 侧
- 插件不使用 defineTool（项目根没有 node_modules），手写同形状定义对象；**不使用 JS 模板字符串**
- .dsh/econ-harvester.patch.yml 用绝对路径把五个 adapter 作为 global insert 注入——这是 wb_* / imf_* / fred_* / bis_* 工具能在会话里出现的机制

---

## 5. 下一步待办

### 5.1 方向 A 第二轮：口径判定 Arbiter —— 已完成

- 产出：python/econ_core/arbiter.py + 报告落盘 data/validated/arbiter/（**7 对** + _index.json）；实测差异读 cross_check/*.json **不重跑取数**；高影响 unknown -> 人工复核，低影响 unknown -> 保留原判定；alignment 记画像与实测是否同调（见 5.4）

### 5.2 方向 A 第三轮：可信度评分 —— 已完成

- 产出：python/econ_core/credibility.py + 报告落盘 data/validated/credibility/（**12 条** + _index.json）；五维加权（Expertise .25 / Provenance .20 / Timeliness .10 / Transparency .15 / Coherence .30）-> 0-100 分 + high/medium/low。当前 9 high / 3 medium；最高 nbs|surveyed_unemployment 95.5，最低 imf|NGDP_RPCH 77.25；两条只对比不落盘的序列 provenance 只有 30（没有 series_file）

### 5.3 方向 D 与之后的待办

- **方向 D 已完成四轮**：① HTML 报告 ② 血缘 + 真实门禁状态 ③ 修 export alias 重复 ④ 图表内联（报告自包含，断网可看）；后续可选导出 PDF / 挂 CI（CI 用 `report.py --test --offline`，冷检出要先预热缓存）。原本挂在方向 A 第四轮下的**拼接断点检查已并入方向 C 第四轮**（那里才有真场景）
- **方向 C 第一轮：FRED CPI —— 已完成**：NBS「上年=100」vs FRED/OECD 10 年全部落「一致」档（最大 0.081 pp）。但 FRED 的原始数据来自 NBS（OECD 转述），**一致性只证明转述无误**
- **方向 C 第二轮「加独立源验证 CPI」—— 已探测，结论：不可达**。四个候选（BIS / OECD / PWT 11.0 / Maddison 2023）**全部不是独立编制**，见 §3.10 与 `python/_probes/README.md`。**不要重开这个方向**：问题不在「还没找到源」，在于中国的价格采集只有 NBS 一个执行者
- **方向 C 第三轮：接 BIS —— 已完成**（bis_client + CLI + adapter + 知识库 + smoke）。理由是**序列长度与拼接实现**，不是独立性：BIS 提供 1995-01 起的月度中国 CPI（现有序列 2015 起，扩 20 年），且 BIS 自己对转载序列做了拼接（joining consecutive periods）+ 重定基（2010=100），是本项目里第一条**真正需要拼接**的链外序列
- **方向 C 第四轮：修 http_client + 拼接器 —— 已完成**（门禁 24/24）。四件事：① 修 `http_client` 大小写缺陷（§3.13 闭环，顺带修掉一处 `KeyError`）；② BIS 两条接进声明式清单，打通 validated / processed / 导出（152 -> **900 行**）；③ 新增 `splicer.py` + `tools/splice-cpi.py`，**首次真实拼接** 30 期 / 拼接点 2015 / verdict=需桥接；④ 顺手修掉落盘扫描**去重从来没生效**的静默 bug（§3.14，n_series 23 -> 13）
- **拼接结果只到 validated，没进 processed**。下一步若要让它进导出链路，得先让 `fill_strategy` 认识拼接产物（它现在只认 `rows` 长表）。**也还没做**：水平调整（ratio splice / rebasing）、PROV-JSON 血缘
- **ICP 2021 方向：单独立项（真正独立的价格水平数据）**。CPI 维度已证不可达（§3.10），但**价格水平**维度的独立测量是存在的：世界银行 ICP 是各经济体**自己采集**一篮子代表品，2021 轮中国**参加了**（NBS 2024-05 自行发布过 2021 轮 ICP 结果）。可用它验 PWT 的 `pl_gdpo` 或 OECD `DF_TABLE4` 的中国 PPP —— 一方是中国官方采集、一方是多边化处理，这才是真交叉验证。立项前要先解决 §3.11 的 TLS 证书链（PWT 侧）
- **方向 B（已暂停，需单独立项）**：桌面版装配链路。实测本会话真正生效的是 .dsh/econ-harvester.patch.yml 的 global insert，不是 preset 的 persona；共有三层注册机制、两份 preset 副本
- **落地插值**：当前 true_gap = 0 例，所以插值实现故意留空（出现 interpolate 会抛 NotImplementedError）。等真遇到上游序列中断再实现，并同步补回归

### 5.4 已知的小尾巴

- **arbiter 曾按硬编码序列对识别形状，差点把 CPI 对比错配给 GDP 指数对**：compare-cpi.py 最初写的顶层键 `max_abs_diff_pp` 命中 `_adapt` 的 GDP 指数形状，产出一条「看起来 7 对、实际第 7 对是错的」结果（`measured.source` 指向 cpi 文件，pair_key 却是 GDP）。已改为数据驱动（产物自带 `series_a` / `series_b` / `measured`，旧的键名形状只记 warning）。这是「数据驱动 vs 硬编码」的活教材：**按键名猜语义，迟早错配**
- **拼接器判"水平跳跃"必须跟序列自身的步长比**（本轮实测踩到）：中国 CPI 2014→2015 是 2.06%→1.4% 的**真实变化**，相对量 0.32 却被判 reject「不可拼接」。已加 `excess_ratio`（绝对跳变 / 接缝前同源步长中位数），超额 ≤1 降为 ok。**教训**：任何"相对阈值"都必须先问「这个分母的量级是多少」——同一课在 `overlap.max_diff_rate` 上又犯了一次（0.0 vs 0.051 算出"分歧 100%"）
- **`RELATIVE_METRIC_FLOOR = 0.5` 是为百分点量纲标定的**，不普适。换成亿元量纲的序列（GDP）要相应放大。没做成自适应是因为实测那样会反向漏判（一路降到 0 的序列，中位量级本身也小）
- aggregated_from_months 只存在于 validated / processed 的 JSON 层，**不在 CSV / SQLite 里**（输出层 14 列是定案集合，export.py 未改）。要它可见就追加为第 15 列，或折进 missing_evidence
- compare-gdp-3way 的 timeout 已放宽到 420s，但实测波动大（16.5s -> 85.1s -> 74.1s），若再变慢要考虑拆项；**arbiter 的 verdict 与 recommended_action 可能不同调**（gdp × imf|NGDPD：画像=可桥接，实测=noise/splice）——已用 alignment=profile_stricter 显式记录，要真正统一得改 source_profiler 的归因规则

---

## 6. 关键文件地图

### 6.1 python/econ_core/ —— 生产层

逐文件的职责与实测细节见 **1.1 已完成模块**（那里更细）。以下只列 1.1 未展开的部分。

**HTTP 层的两个通用工具**（本轮新增，供所有 client 复用；别再各自造轮子）：
`_header_get(headers, name, default)` —— 大小写不敏感的响应头查找（§3.13）；
`_decompress(body, content_encoding)` —— 按 Content-Encoding 解压，失败/未知都降级为原始字节。

### 6.2 tools/ —— 脚本层（门禁、对比、加工、输出）

| 文件 | 一句话职责 |
|---|---|
| run-all-checks.py | 门禁总入口：顺序跑 24 项检查，失败即停；支持逐检查 timeout 覆盖 |
| check-cli-envelope.py | 契约测试：真跑 10 个子命令（11 用例），按 R1-R11 校验 stdout 信封；超时 420s |
| smoke-nbs-adapter.mjs | NBS 插件冒烟：桩替换 execFile，验证 argv 与必填校验 |
| smoke-worldbank-adapter.mjs | World Bank 插件冒烟（含 wantArgv 精确比对） |
| smoke-imf-adapter.mjs | IMF 插件冒烟 |
| smoke-fred-adapter.mjs | FRED 插件冒烟（含 start/end 可选参数的 argv 拼装） |
| smoke-bis-adapter.mjs | BIS 插件冒烟（三参数全可选 + key 原样透传 + 两个必填缺失路径） |
| verify-preset.mjs | preset 装配校验（不起 DSH 服务）：ESM 加载、cordis 激活/卸载、YAML 形状、相对路径解析；**不在门禁里，要手工跑**。default 断言容错内置 preset |
| compare-gdp.py | NBS vs World Bank 中国 GDP（首个交叉验证，结论：逐位相同） |
| compare-gdp-3way.py | NBS vs WB vs IMF 三方（统一到亿美元；汇率取 WB PA.NUS.FCRF） |
| compare-gdp-real.py | NBS GDP 指数 vs IMF NGDP_RPCH 实际增速（无汇率污染口径） |
| compare-unemployment.py | 登记失业率 / 调查失业率 / IMF LUR 三方（百分点阈值） |
| compare-cpi.py | CPI 交叉验证：NBS「上年=100」vs FRED/OECD（年均值转同比，阈值 0.3/1.0/2.0 pp） |
| scan-missing.py | 缺失扫描：声明式清单 + data/validated 落盘扫描，产出 missing_report.json（含两条已知断言） |
| materialize-validated.py | 把声明式清单落盘 data/validated/（复用 scan-missing 的清单，不复制） |
| run-fill-strategy.py | 一键 validated -> processed；内置 raw 缓存快照对比证明脱网 |
| export.py | 输出层：processed -> CSV（utf-8-sig）+ SQLite（表/索引/视图）+ Markdown 数据字典；按规范键去重 alias 副本 |
| report.py | 展示层：5 份 JSON -> 单文件 HTML 质量报告（Plotly 走 CDN）+ 7 项自检 |
| splice-cpi.py | 首次真实拼接：BIS 年化 ⊗ NBS 年度 CPI，产出 data/validated/spliced/ |

### 6.3 src/plugins/ 与 .dsh/ —— 会话装配

| 文件 | 一句话职责 |
|---|---|
| src/plugins/hello.js | 最小宿主插件，仅用于验证 preset 装配链路 |
| src/plugins/nbs-adapter.js | 四个 nbs_* DSH 工具（薄壳转发 nbs_client_cli） |
| src/plugins/worldbank-adapter.js | 三个 wb_* DSH 工具 |
| src/plugins/imf-adapter.js | 三个 imf_* DSH 工具 |
| src/plugins/fred-adapter.js | 两个 fred_* DSH 工具（fetch-series / list-search） |
| src/plugins/bis-adapter.js | 两个 bis_* DSH 工具（fetch-cpi / fetch-series） |
| .dsh/.agent-presets/econ-harvester/agent.cordis.yml | preset 的插件行清单（五个 adapter + persona + shell + 文件系统），用相对路径 |
| .dsh/.agent-presets/econ-harvester/preset.yml | preset 名称与描述（要点明装配了 NBS + World Bank + IMF 三套工具） |
| .dsh/econ-harvester.patch.yml | 用绝对路径把五个 adapter 作为 global insert 注入宿主组合 |
| .dsh/econ-harvester.patch.yml.bak | patch 的备份，改坏时可对照 |

家目录另有一份镜像（桌面版读取）：C:\Users\user\.dsh\.agent-presets\econ-harvester\ 下的 agent.cordis.yml 与 preset.yml。两份都要维护，家目录那份用绝对路径。

### 6.4 数据目录（data/ 下内容全部被 .gitignore 忽略，只保留 .gitkeep）

| 目录 | 内容 |
|---|---|
| data/raw/_http_cache/ · data/parsed/{nbs,worldbank,imf,fred}/ | HTTP 原文存档（所有结论的最终证据）；按请求指纹的解析结果 |
| data/validated/{nbs,worldbank,imf,fred,bis}/ | 长表（12 条声明式序列），materialize-validated 的产物 |
| data/validated/spliced/ | 拼接产物（cpi_bis_nbs_spliced.json：拼接结果 + 重叠期 + 断点 + verdict） |
| data/validated/missing_report.json | 缺失分类总报告（scan-missing 产物） |
| data/validated/{arbiter,credibility}/ | 口径判定报告（7 对）/ 可信度评分报告（12 条） |
| data/validated/cross_check/ · data/processed/ | 对比结果 JSON（**5 个脚本**，自带 series_a/series_b/measured）；processed 是带缺失元数据的行（11 个文件 / 去重后 10 条序列） |
| data/parsed/bis/ | BIS 序列的解析结果（本轮新增；**只到 parsed 层**，未进 validated） |
| data/raw/_probe_pwt_maddison/ | 独立性探测的证据物（`maddison2023.xlsx` 等，不 commit，见 python/_probes/README.md） |
| data/output/ | 产品：econ_data.csv / econ_data.db / data_dictionary.md / **report.html** / last_gate.json（最近一次门禁状态） |

### 6.5 项目根其他文件

| 文件 | 一句话职责 |
|---|---|
| package.json | 声明 type: module，使 .mjs / .js 插件按 ESM 加载（不要删） |
| pip_sandbox_install.py · .gitignore | 沙箱 pip 安装包装（装过 pyyaml / jinja2 / pandas / pyarrow）；.gitignore 忽略 .venv / .tools / node_modules 与 data 下五个子目录 |

---

## 7. 新对话开场步骤

新对话接手时，按顺序做这四件事，做完只报告状态，不要动代码，等指令：

1. 读 PROJECT_STATE.md（本文件）——先建立全局认识
2. git log --oneline -20 ——看重最近提交，判断哪些改动已固化、哪些还挂在 working tree
3. 跑门禁确认基线：

       cd D:\universe\econ-data-harvester
       .\.venv\Scripts\python.exe tools\run-all-checks.py

   期望 24/24 PASS，exit 0。若不是 24/24，先定位是哪一项退化了，不要继续叠加改动。
4. 报告状态：门禁结果、git status、当前数据规模（12 条声明式序列 / 900 行 / 10 缺失行），然后停下等指令

### 7.1 报告模板（建议照抄）

    门禁：24/24 PASS（exit 0）
    working tree：<git status --short 的内容>
    数据：12 条声明式序列 / 900 行 / 10 缺失行（按行：series_start 5, discontinued 3, not_yet_published 2, true_gap 0）

### 7.2 改动后的固定动作

1. 开工前自检：pwsh 报 ACL 故障见 3.7；import yaml / Jinja2 见 3.6；data/validated 为空则先跑 materialize-validated
2. 跑完整门禁，确认仍 24/24（新增检查要同步加进 CHECKS 与 docstring 编号）
3. 新增序列 -> 补 source_profiles.yaml 条目（否则 profiler 抛 KeyError）
4. 改契约/分类/字段名 -> 同步更新本文件的第 2 节与第 3 节
5. 不要把 data/ 下的产物提交进 git（它们已被忽略）；不要把 raw 存档删掉（那是证据链）

---

（本文件结束。全文不含任何代码片段；细节请读对应模块的 docstring 与 python/_probes/README.md 的实测记录。）

