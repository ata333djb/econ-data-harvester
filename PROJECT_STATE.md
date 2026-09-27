# PROJECT_STATE.md —— EconDataHarvester 状态快照

- 生成时间：2026-09-27
- 面向对象：**新对话的 Agent**。读完之后应当能直接继续工作，不需要回看任何历史对话。
- 维护规则：每完成一轮实质改动就更新本文件。**只写状态**，不写历史对话，不粘贴代码，只给路径与一句话职责。
- **篇幅上限：≤720 行**（2026-09-27 四调：原 500 -> 550 -> 600 -> 700 -> 720）。
  **每次放宽都必须在这里写清"多出来的是哪个方向的哪一节"**，否则上限会一路变成没有上限：
  - 600 -> 700：方向 E 第一轮（指标目录），新增 §1.4 / §2.13 / §3.15-3.17 / §5.3① / §6 / §7 六处
  - 700 -> 720：方向 E 第二轮（`edh fetch`），新增 §2.14 + §3.18，并把 §1.4/§1.5 换成新一轮。
    **本轮先做过一轮压缩**（725 -> 711，压的是 §1.4/§2.14/§3.15-3.18 里与模块 docstring
    重复的部分），压不动了才放宽 20 行 —— 顺序是先压后放，不是直接放。
  本文件内容是坑 + 约定 + 教训，**删任何一条都会增加后人重踩的风险**，所以宁可放宽也不删条目。
  设立上限的目的是「新 Agent 能一次读完」（720 行中文约 10000-13000 token），**不是为了压而压**。

权威性顺序（冲突时以序号小的为准）：

1. 门禁 tools/run-all-checks.py 的**实际输出**（当前应为 24/24 PASS）——这是唯一硬标准
2. 本文件
3. 各模块 docstring —— 细节、实测证据、踩坑经过都写在那里

---

## 1. 当前进度快照

### 1.1 已完成模块（一行一个）

**采集层**（`python/econ_core/`，每个源 = client + CLI，配一个 `src/plugins/*-adapter.js` 薄壳）

- `http_client.py` —— 纯标准库 HTTP 客户端；原始响应按 url+method+body 指纹存档到
  `data/raw/_http_cache/`，是**所有**网络访问的唯一出口。对外两个通用工具：`_header_get`
  （大小写不敏感头查找，§3.13）、`_decompress`
- `nbs_client.py` / `nbs_client_cli.py` / `nbs-adapter.js` —— 国家统计局新版平台
  （POST getEsDataByIndicatorIdAndDa + GET 目录树/默认指标/省级列表）；四子命令，4 个 `nbs_*` 工具
- `worldbank_client.py` / `..._cli.py` / `worldbank-adapter.js` —— World Bank v2 REST；3 个 `wb_*` 工具
- `imf_client.py` / `..._cli.py` / `imf-adapter.js` —— IMF DataMapper v1；3 个 `imf_*` 工具
- `fred_client.py` / `..._cli.py` / `fred-adapter.js` —— FRED `fredgraph.csv` 免密钥端点；
  固定朴素 UA，按日期分布识别频率；2 个 `fred_*` 工具
- `bis_client.py` / `..._cli.py` / `bis-adapter.js` —— BIS SDMX 2.1；中国 CPI 月度 1995 起
  + 通用 SDMX 拉取；2 个 `bis_*` 工具

**规范化层**：`normalize.py`（统一 14 列 schema + row_sha16；四个入口：
`normalize_observations` / `normalize_worldbank_observations` / `normalize_imf_observations` /
`normalize_cpi_wide`）；`series_key.py`（键规范化 + 别名解析，export / report 共用的**去重依据**）

**验证与分析层**：`cross_validation.py`（逐期比对 + 四段阈值）· `missing.py`（缺失四分类）·
`fill_strategy.py`（决策写进行级元数据，**不插值**）· `source_profiler.py` + `source_profiles.yaml`
（画像 / **人工编纂**知识库）· `arbiter.py`（画像 × 实测 -> 四值判定）· `credibility.py`（五维评分）·
`splicer.py`（拼接：重叠期选源 + 三类断点 + 可选 rebase + 三值 verdict）

**用户产品层**（方向 E，从「开发者要写 Python」变成「用户输入指标名」）

- `catalog_data.yaml` + `catalog.py` —— 指标目录：18 个主流宏观指标 / **51 条源映射**，
  把「GDP 增速」这类说法映射到各源真正要的参数（NBS 的三个 UUID 对用户彻底隐藏）
- `fetcher.py` —— **取数适配层**：catalog 配置 -> 五个 client -> **统一 9 字段行**
  （`fetch_indicator` / `fetch_all_sources` / `cross_check`，§2.14）。五家 client 的调用方式、
  时间标签、数值字段、频率语义全不一样，差异全在这一层吃掉
- `tools/edh.py` —— 用户 CLI：目录侧 `list` / `list --source nbs` / `info CPI` / `summary`；
  取数侧 `fetch CPI --from 2020 --to 2024`（CSV 到 stdout、简报到 stderr、`--cross-check`）
- 清单是**人工编纂 + 机器验证**：每条映射都由 `python/_probes/probe_catalog_sources.py`
  真跑过取数（§2.13、§3.15）

**工具与输出层**（`tools/`）：`compare-{gdp,gdp-3way,gdp-real,unemployment,cpi}.py`（五条交叉验证）·
`splice-cpi.py`（真实拼接 + 三模式对比）· `smoke-{nbs,worldbank,imf,fred,bis}-adapter.mjs` ·
`export.py`（-> CSV + SQLite + 数据字典）· `report.py`（-> 单文件 HTML）·
`src/plugins/hello.js`（最小宿主插件，只证明装配链路可激活）

**文档**：`python/econ_core/README.md`（生产层已知上游事实）· `python/_probes/README.md`
（探测脚本索引 + 四源独立性判定，§3.10）

### 1.2 门禁：28 项（tools/run-all-checks.py，当前 28/28 PASS）

| # | 检查 | 说明（类型都是 node / python） |
|---|---|---|
| 1-5 | smoke-{nbs,worldbank,imf,fred,bis}-adapter | 五个插件的 argv 拼装（**桩替换 execFile，不 spawn**） |
| 6 | check-cli-envelope | 真跑 12 个子命令（13 用例）校验 stdout 信封契约 R1-R11；timeout 420s |
| 7 | missing --test | 缺失四分类自检（纯离线） |
| 8 | source_profiler --test | 来源画像自检，8 场景（纯离线） |
| 9 | arbiter --test | 口径判定自检，7 场景（纯离线） |
| 10 | normalize --test | 规范化层自检（row_sha16 唯一性、i_name null 补齐） |
| 11 | cross_validation --test | 交叉验证四段阈值自检（纯离线） |
| 12-16 | compare-{gdp,gdp-3way,gdp-real,unemployment,cpi} | 五条交叉验证链路；3way/cpi timeout 420s |
| 17 | scan-missing | 缺失检测与分类，产出 `missing_report.json`；timeout 420s |
| 18 | materialize-validated | 声明式清单落盘 `data/validated/`；timeout 420s |
| 19 | run-fill-strategy | validated -> processed（零填充，**已脱网**） |
| 20 | credibility --test | 可信度评分自检，5 场景（纯离线） |
| 21 | export | processed -> CSV / SQLite / 数据字典（纯离线） |
| 22 | report | 5 份 JSON -> 单文件 HTML + 7 项自检（纯离线） |
| 23 | splicer --test | 拼接器自检，6 个必测场景 + 8 个边界（纯离线） |
| 24 | splice-cpi | 真实拼接：BIS 年化 ⊗ NBS 年度 CPI（读 validated + 拉一次 BIS）；timeout 420s |
| 25 | catalog --test | 指标目录自检：18 指标 / 51 源映射、别名唯一、必需键齐全（纯离线） |
| 26 | edh list | 用户 CLI 第一条命令：argparse + CJK 对齐 + 渲染 + 退出码（纯离线） |
| 27 | fetcher --test | 取数适配层自检：五源统一形状 / 年化手算复核 / 交叉验证 / KeyError 路径（**联网**，timeout 420s） |
| 28 | edh fetch smoke | 用户真敲的那条命令：四源合并 + CSV 到 stdout + 简报到 stderr（**联网**，timeout 420s） |

顺序**有依赖**：18 -> 19 -> 20 -> 21 -> 22 -> 23 -> 24。credibility 读 validated + processed +
cross_check 三样产物（不能挪到 arbiter 旁）；**splice-cpi 读 materialize-validated 产出的 NBS CPI 文件，
必须排在它之后**。网络密集型检查 timeout 放宽到 420s。25/26 是纯离线，27/28 是**联网**的，
且 25 -> 26 -> 27 -> 28 的次序有理由：先确认目录本身没坏，再验"按目录取数"这条路 ——
**目录对不等于取数对**（NBS 三个 UUID、WB 的 date_range、IMF 的预测值都在适配层）。

**五处是主动加的**（当时任务只要 23/23）：① 第 24 项 `splice-cpi` —— `splicer --test` 只证明
**合成**数据能拼，而「真实链路上真的接上了」只有跑真实脚本才算验过；② check-cli-envelope 里的
2 条 BIS 用例 —— 新源不进契约测试等于信封契约零覆盖（**只加条目，未改任何已有条目**）；
③ 第 25/26 项 —— 指标目录是**人工维护的 YAML**，没有断言就没有防漏网，
而 `edh list` 覆盖的是 `catalog --test` 摸不到的 argparse / 渲染 / 退出码；
④ 第 27/28 项 —— 同一课再往下一层：`fetcher` 是"目录配置 -> client 调用"的适配层，
`edh fetch` 又覆盖 CLI 与 CSV 渲染。
顺带记录：**FRED 不在 check-cli-envelope 覆盖里**（历史遗留，未动）。

### 1.3 当前数据规模

- **声明式序列数**：**12 条**（NBS 6 + World Bank 1 + IMF 2 + FRED 1 + **BIS 2**）
- **总行数**：**900 行**（CSV / SQLite；processed 910 / validated 948，口径差异见 §3.9）
- **按源分布（实测）**：**bis 748**（= 368 + 380）/ imf 69 / nbs 63 / fred 10 / worldbank 10
  —— BIS 一次接入就占了 83% 的行数
- **缺失行数**：10 行（全部来自 NBS/IMF 年度序列；**BIS 两条零缺失**），**一个都没有填补**
- **缺失分类（按行）**：series_start 5 / discontinued 3 / not_yet_published 2 / true_gap **0**
- **缺失动作（按行）**：leave_null 8 / wait 2 / interpolate 0
- **row_sha16 覆盖率**：900/900（全部唯一）
- 落盘链路：`data/validated/`（12 个长表 JSON + missing_report.json + spliced/）-> `data/processed/`
  （12 个带缺失元数据）-> `data/output/`（CSV + SQLite + 字典）
- **missing_report.json 的 n_series=13**（12 条声明式 + 1 条 selftest 残留）。这个数此前是 23，
  因为落盘扫描的去重**从来没生效**（§3.14）
- 知识库另有 **2 条只用于对比、未落盘**的序列（`nbs|gdp|index_prev_year_100`、`imf|NGDP_RPCH`），
  所以知识库规范键 > 落盘序列数

**指标目录（方向 E，与上面的落盘数据是两回事）**：18 个指标 / **51 条源映射**
（nbs 16 · worldbank 16 · imf 8 · fred 9 · bis 2）。51 条**逐条真跑过取数**；
另有 **3 条探过且确认对中国取不到数据**（`imf|BX_GDP`、`imf|BM_GDP` 各 0 行；
`worldbank|GC.DOD.TOTL.GD.ZS` 1960-2025 共 66 行全 null），**留痕但未收录**（§2.13）。
目录是**声明**、不是已落盘数据：`edh fetch` 落地之前，1.3 上半部分的 900 行不受影响。

12 个声明式 series_key + 1 个别名（知识库必须与之一一对应）：

    1. nbs|gdp|cny_100m                        7. worldbank|NY.GDP.MKTP.CN
    2. nbs|cpi|全国居民消费价格指数（上年=100） (%)   8. imf|NGDPD
    3. nbs|cpi|城市居民消费价格指数（上年=100） (%)   9. imf|LUR（起点 2017）
    4. nbs|cpi|农村居民消费价格指数（上年=100） (%)  10. fred|CHNCPIALLMINMEI（月度 -> 年均值）
    5. nbs|registered_unemployment（停更于 2021）  11. bis|WS_LONG_CPI|M.CN.771（同比 %，368 期）
    6. nbs|surveyed_unemployment（起点 2018）      12. bis|WS_LONG_CPI|M.CN.628（指数 2010=100，380 期）
    别名：NBS|000000000000|db8e5a86c08246e79b1b11251927e740 -> 指向第 1 条（不计入 12 条）

### 1.4 最近一轮新增（方向 E 第二轮：`edh fetch` —— 从"能看清单"到"能拿数据"）

- **`python/econ_core/fetcher.py`** —— 取数适配层，把五家 client 的差异全吃掉：
  `fetch_indicator()` / `fetch_all_sources()` / `cross_check()` / `annualize_rows()`，
  `last_notes()` 报**每个源这一轮怎么了**（沿用五个 client 的 `last_meta()` 惯例）
- **`tools/edh.py fetch`** —— `--region / --source / --from / --to / --frequency /
  --output(-o) / --format csv|json / --cross-check / --quiet / --allow-forecast`。
  **CSV 到 stdout、简报到 stderr**（stdout 永远可以直接 `> x.csv` 或进管道）
- **门禁 26 -> 28**：`fetcher --test`（联网，约 15 次请求）+ `edh fetch smoke`（真敲的那条命令）
- **本轮修掉 3 个由自检/实测抓出的 bug**（全在输出/簿记层，断言不会变红的那种）：
  `last_notes()` 被后一个源清空；`csv.writer` 行尾 + Windows stdout 翻译导致**每行多一个空行**；
  `unit` 字符串直接比会**误报"口径不同"**。逐条见 §3.18
- **交叉验证的立场（重要）**：`--cross-check` **不替用户做单位换算**。
  两侧 unit 不同时判 `口径不同`，并把**绝对差**标成"仅供参考"。CPI 四源实测 6 对里
  只有 **World Bank vs BIS 可比**（同为 2010=100，相对差 0.337% -> 可直接拼接），
  其余 5 对因基期/口径不同被判不可比。这是**结论不是缺陷**：NBS 是「上年=100」、
  FRED 是「2015=100」、BIS 重定基 2010=100，硬算出来的差异率没有意义（§3.10/§5.4）

### 1.5 更早几轮（压缩存档）

- **方向 E 第一轮（指标目录）**：`catalog_data.yaml`（18 指标 / 51 源映射）+ `catalog.py`
  （`list_indicators` / `get_indicator` / `get_source_config` + `--test`）+ `tools/edh.py`
  （`list` / `info` / `summary`）+ `_probes/probe_catalog_sources.py`（8 阶段取证脚本）；
  门禁 24 -> 26。51 条映射**逐条真跑过取数**，另有 3 条"探过、确认对中国取不到数据"留痕未收录。
  当轮踩的坑见 §3.15-3.17

- **方向 C 第五轮（rebase）**：`splicer.rebase()` 的 `ratio`/`difference` 两模式 + `splice(rebase=...)`；
  自检 46 -> 58 项。**关键结论：rebase 只调水平不调斜率，对当前 CPI 用例无效** ——
  触发「需桥接」的是 `trend_break`（magnitude 1.3590）不是 `level_jump`（excess_ratio 0.3378 本判 ok）；
  且 NBS 完全落在 BIS 跨度内 -> `applied=False`（`not_applied_reason: no_non_overlap`），
  故三模式 verdict 相同**不构成**"rebase 无效"的证据。当轮修掉 5 个 bug（§5.4 留两条）

- **方向 C 第四轮**：修 `http_client` 大小写缺陷（§3.13 闭环，顺带修掉一处 `KeyError`）；
  BIS 两条接进声明式清单打通 validated -> processed -> 导出（152 -> **900 行**）；
  新增 `splicer.py` + `tools/splice-cpi.py`（首次真实拼接 30 期 / 拼接点 2015）；
  修掉落盘扫描**去重从来没生效**的静默 bug（§3.14，n_series 23 -> 13）。门禁 22 -> 24/24
- **方向 C 第三轮**：四源独立性探测固化（§3.10/3.11/3.12）；新增 `bis_client.py` + CLI +
  adapter + smoke + 知识库 BIS 条目（自检 14/14）；门禁 21 -> 22/22；修 `verify-preset.mjs` 的
  default 断言（容错内置 preset `standard`，它**不在门禁里，要手工跑**）
- **更早**：`arbiter._adapt` 改成**数据驱动**（读产物自带的 `series_a`/`series_b`/`measured`，
  不再按键名猜形状），5 个对比脚本输出都补了这三个字段

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

### 2.13 指标目录（`catalog_data.yaml`）的规矩

- **它是人工编纂的，和 `source_profiles.yaml` 同类**，不是自动抽取的产物。加指标 = 手写一条
- **每条 source 映射必须真跑过一次取数**才算数。改完清单用
  `python/_probes/probe_catalog_sources.py verify` 复核（真跑 56 次请求，约 4 分钟），
  证据落 `data/raw/_probe_catalog/verified.json`
- **三个 NBS UUID 的含义**（实测，别再猜；详见 YAML 头部注释）：
  `indicator_id` = 指标叶节点自身 `_id`；`cid` = 它的父目录节点 `_id`（**取数时后端忽略**，§3.1）；
  `root_id` = **该目录第一条 level-2 类目**的 `_id`，三个目录各一个常量
  （月度 `3c9c4593…` / 季度 `1b1ce0cf…` / 年度 `71d41888…`）。**`root_id` 不是指标的祖先**
- **缺源 = 键不存在**，不写 `null`（同 §2.2 的精神）。`GOVERNMENT_DEBT` 下没有 `nbs` 键 = NBS 不提供
- **别名必须全局唯一**，加载时强制（冲突直接抛，不做"后来者覆盖"）。
  `display_name`（中文名）**也进可检索索引** —— 中文用户打的就是它
- **查询按显示宽度对齐**：中文列不能用 `len()` 补空格（`len("国内生产总值")==6` 但占 12 列）。
  `tools/edh.py` 的 `_display_width()` 是唯一的宽度函数
- 同源多频率/多口径用 `variants`（CPI 的 NBS 月度、失业率的 NBS 登记口径）。
  **`variants` 里的每条也要带齐该源的必需键**
- `name`（规范名，如 `GDP_PER_CAPITA`）与 `display_name`（中文）**分工固定**：
  规范名是键与排序依据，中文名是给人看的、且必须可检索

### 2.14 取数适配层（`fetcher.py`）的规矩

- **统一行 9 个字段，顺序固定**：`indicator / source / region / period / period_type /
  value / unit / frequency / series_name`。它同时是 `edh fetch` 的 CSV 列序 ——
  `ROW_FIELDS` 是唯一真源，改一处就要改另一处
- **五源的差异只在适配层出现**，别往 client 里加统一化逻辑：nbs 要 `dts` 且**必须显式给全**、
  wb 要 `date=lo:hi`、imf/bis **给不了范围**（取回后本地裁）、fred 有 `start/end`。
  时间标签用 `dt` 的 `YY`/`MM` 后缀**结构化还原**，不去解析中文 `dt_name`
- **默认窗口 = 最近 10 年**。不给默认值的话，"不限"在五个源上含义不同，合并成一张表的
  跨度会差几十年（NBS 不给 `dts` 就等于没取）
- **月度年化 = 年均值** + `aggregated_from_months`（月数）；**年度的绝不反向拆成月度**（§2.5）
- **IMF 默认截到今年**（它含预测值，实测排到 2031）；要看预测得显式 `--allow-forecast`
- **NBS 占位行照收**（`v` 空串 -> `value=None`）：用户要看见"官方这期没发布"，
  而不是一张比真实年份少的表。简报的「空缺」行数就是它
- **`fred` / `bis` 序列国别写死在 ID 里**，故 `region != CHN` 时**直接报错**，
  而不是返回中国的数却标成别国
- **`--cross-check` 不替用户做单位换算**：`unit_match=False` 时判「口径不同」并把绝对差
  标成"仅供参考"，绝不硬报一个"冲突"（`_unit_key()` 的规矩见 §3.18 第三条）
- **stdout 是数据、stderr 是人话**（同 §2.2）：CSV 走 stdout，简报走 stderr，
  所以 `edh fetch CPI > cpi.csv` 永远得到干净的 CSV

---

## 3. 已知的坑（不要再踩）

### 3.1 NBS 的 cid 参数被后端忽略

把 `cid` 改成任意错值，返回的观测**逐字节相同**（同一 raw 存档 sha256）。原因：
`getEsDataByIndicatorIdAndDa` 实际按 `id`(=tree_node_id) + `rootId` + `da` + `dts` 选序列，
响应里根本没有 cid 字段。处置：**不改签名**，只在 `nbs_client` docstring 里记实测证据。
副作用（已知未修）：cid 参与 parsed 落盘的文件名指纹，所以错 cid 会写出**另一个指纹的副本**。

### 3.2 境外源会拒绝 Chrome UA（IMF 的 Akamai、FRED）

用 http_client 默认的 Chrome 128 UA 请求 `api.imf.org` -> **403 AkamaiGHost**（连站点根也 403）；
`fredgraph.csv` 直接 `RemoteDisconnected`（重试 3 次全失败）。**只把 UA 换成
`python-urllib/3.12`** 就立刻 200 —— 方向与「伪装成浏览器」相反。已固化在
`imf_client.IMF_HEADERS` 与 `fred_client.FRED_HEADERS`，**不要顺手改回 Chrome**。
**接新的境外源时先试朴素 UA。** 同源坑：`/v1/{indicator}/{country}` 的 country 路径段
**不做服务端过滤**（实测 `/NGDPD/CHN` 仍返回 229 个国家），过滤必须在客户端做。

### 3.3 NBS 的 i_name 有时是 null

2024 年 GDP 行的 `i_name` 为 null，导致 `indicator_name` 为空。处置：`normalize._fill_indicator_names`
让**同一 indicator_id 的所有行共享同一个名称**（组内首个非空值；整组都空才回落 indicator_id），
并在填充后**重算 row_sha16**。衍生事实：Parquet 没有 null 字符串语义，回读会变 NaN，
**严格保真请读 JSON 版本**。

### 3.4 中文文件名绝不能用「剥非 ASCII」的方式安全化

早期 `_safe()` 把非 ASCII 全替换成下划线，于是 `nbs|cpi|全国 / 城市 / 农村` 三条序列被压成
同一个文件名，**后写的静默覆盖先写的**（丢数据）。正确做法：只替换 Windows 非法字符
（`\ / : * ? " < > |` 与控制符），**保留中文**。自我验证：`fill_strategy` 会复读 processed 文件核对
decisions 数 == 缺口数，正是这条检查抓到了那次覆盖。

### 3.5 data/output/* 必须留在 .gitignore

注意 `data/output/*` 与顶级 `output/*` 是**两个不同的**路径，早期只忽略了后者，
导致 CSV/DB/字典会被误提交。现在 `.gitignore` 里 raw / parsed / validated / processed / output
五个 data 子目录都有条目，靠 `!data/**/.gitkeep` 保留目录。

### 3.6 venv 依赖（重建环境时必看）

venv **原本没有 PyYAML**；`source_profiler` 加载知识库需要它，已用
`pip_sandbox_install.py install pyyaml` 安装。新机器需补装：**pyyaml（知识库必需）、
jinja2（HTML 报告必需）**；pandas + pyarrow（仅 `write_validated_parquet` 需要）。
**缺依赖时报错会直接给出安装命令，不会静默降级。**

### 3.7 ⚠️ 本机沙箱 ACL runner 故障（环境问题，不是策略拒绝）

- 现象：workspace-write 下 pwsh 可能直接失败，报 `Runner failure: windows-acl-run: --temp is not
  an existing directory: C:\Users\user\AppData\Local\Temp\dsh-<随机后缀>`
- 含义：ACL 受限令牌 runner 起不来（临时目录不存在）。涉及 `Temp\dsh-*`（每命令一个随机目录，重建单个没用）
- 处置：**先试普通模式；失败即带 `sandbox_permissions=danger-full-access` + justification 重试一次**；一次被拒即终局，不绕路
- 读文件类工具（read/edit/write）**不受影响**，只有 pwsh 子进程受影响
- 写 workspace 之外的文件要 danger-full-access 审批；审批无人应答会挂到墙钟上限（实测约 10 分钟）才失败，且**不会部分生效**

### 3.8 其他容易踩的（按层归类）

**NBS 取数**：调查失业率**只在月度树（code=1）**，年度树里没有（登记失业率反过来）；
实测从 2018-01 起才有数据（请求 2017 也只回 2018+）；登记失业率 2022/23/24 的 v 是**空串**
（节点还在但不更新）；目录树里 13 个「失业」节点绝大多数是失业保险基金/参保人数，不是失业率。

**CPI 宽表**：`yData[].value` 是**字符串数组**（如 `"101.4"`），必须过 parse_value，
否则会被当成「全缺失」并误判 discontinued；`yData[].du` 是**数据单位 id，三条序列共用同一个值**，
不能直接当 indicator_id（统一入口用 `du#序号` 保证唯一）。

**IMF**：数据**含预测值**（实测 CHN 的 NGDPD / LUR 都到 2031），做实际值比对必须自己截年份；
`values` 里有一个空字符串键（值为 null），解析时要跳过。

**落盘扫描**：识别长表行必须要求 **period 与 value 同时存在**，否则会把对比脚本的结果行
（只有 period + nbs_usd_100m 等）误当序列、整段判成缺失；去重必须按产物自带的 series_key（§3.14）。

**测试方法学**：`smoke-*.mjs` 用**桩**替换 execFile，只能验证 argv 拼装，**验证不了真实 CLI 的
信封契约**（契约由 check-cli-envelope.py 真跑子进程覆盖）；沙箱下 node 的 `child_process` 管道
stdio 会 spawn EPERM，所以 smoke 必须走桩；网络密集型检查耗时波动 5-6 倍，各自 420s 超时；
`shutil.which("node")` 在本机解析到含空格的路径，失败块里的命令要加引号才能粘贴执行。

### 3.9 缺失统计有两个口径，别混用（也不要把大写 NBS 目录当 bug）

- **按行**（本文件 1.3 用的口径）：10 行 = series_start 5 + discontinued 3 + not_yet_published 2
- **按缺口段**（data/validated/missing_report.json 的 `by_classification_series` 与
  `by_classification_gaps`，**两者数值相同**）：`not_yet_published 2 / discontinued 1 / series_start 2`，
  合计 **5**，与 `n_gaps=5` 自洽。**此前记的 4/2/4（=现值的两倍）是 §3.14 修「去重从来没生效」之前的
  重复计数快照，「同一缺口被算两次」这个现象已经不存在了** —— 段口径与行口径的差异（5 vs 10）
  来自缺口被合并成段，不是来自重复扫描
- 报数时必须写明口径；run-fill-strategy 打印的决策数是**段**口径，processed 行里的元数据才是行口径
- data/processed/ 下 NBS 序列落在**大写 NBS/** 目录，worldbank / imf 是小写。fill_strategy 已做 .lower()，这是 Windows 复用旧目录名的历史遗留，干净检出不复现

### 3.10 中国 CPI 无独立源（实测结论，勿重开）

四个候选（BIS / OECD / PWT 11.0 / Maddison 2023）**全部不是独立编制** ——
不是「还没找到」，是**采集者只有一个**。判定依据与对比表见 `python/_probes/README.md`。
三条最硬的证据：

1. BIS CPI 页 FAQ 原文：*"Consumer price indices are predominantly compiled by national statistical offices."*（BIS 只做拼接 + 重定基）
2. OECD 的中国 CPI 同比与 IMF WEO `PCPIPCH` **逐年逐位完全相同** —— 同一份 NBS 序列被两次转载的签名
3. OECD 自编 PPP 全家桶（`DF_PPP*` / `DF_PP_CPL_M`）里**中国完全缺席**；唯一发布的那个中国 PPP 与 World Bank `PA.NUS.PPP` 前 7 年 abs diff = 0.000000

**真正的独立测量在价格水平维度（ICP），不在 CPI 维度。** 见 §5.3。

### 3.11 PWT / Maddison 的 TLS 证书链问题

`dataverse.nl` / `www.rug.nl` 的 TLS 链**不完整**（缺 GEANT/HARICA 中间证书，叶证书无 AIA），
而 venv 里**没有 CA bundle**（无 certifi）。实测对照：`sdmx.oecd.org` / `stats.bis.org` 同一解释器
TLSv1.3 正常，只有 Groningen 那两个域失败。

**这不是 UA 问题**，与 §3.2 的「境外源拒 Chrome UA」是两回事。附带：`dataverse.nl` 的
`datafile` 端点还会**中途截断响应**（5,839,841 字节的 `pwt110.xlsx` 只拿到 ~2.9–4.1 MB），
`Range` 续传也未必补全。接 PWT / Maddison 需要「断点续传 + Content-Length 校验」。

### 3.12 OECD SDMX 的 format 白名单差异

structure 服务与 data 服务的 `format` 白名单**不同**，同一个值在一个服务上合法、另一个 406：

- structure 接受：`structure, xml-structure-3.0.0, sdmx-3.0, json-structure-2.0.0`（**`jsondata` 恒 406**）
- data 接受：`genericdata, jsondata, structurespecificdata, csv, csvfile, csvfilewithlabels, xml-data-3.0.0, json-data-2.0.0, csv-data-2.0.0`

另外：**`/data/` 拒绝不完整密钥**（`.../DF_PRICES_ALL,1.0/CHN` -> 403 `expecting 8 got 1`，
维度位必须全给）；**限流是真的**（约 15 次快速请求即 429，`Retry-After: 0` 不可信，
需 9~12s 间隔 + 退避）；**没有机器可读的 provenance 字段**（来源只能靠数值比对反推 ——
这正是 §3.10 第 2/3 条证据的来源）。

### 3.13 ⚠️ http_client 漏解压「头名全小写」的 gzip（已修）

**根因**：`_decompress` 的入参来自 `resp_headers.get("Content-Encoding")` —— **大小写敏感**。
BIS（FusionEdgeServer）回的响应头名是**全小写**（缓存 meta 原始证据：
`headers = {'content-encoding': 'gzip', ...}`，故 `get('Content-Encoding') -> None`），
取到 None 就**不解压**，gzip 二进制被当 JSON 解析。`_headers_to_dict()` 用 `msg.items()`
原样搬运头名，所以**凡返回小写头名的服务器都会中招**（NBS/WB/IMF/FRED 恰好没踩到）。

实测对照（同一 URL 同一 UA，只改 Accept-Encoding）：不带或 `identity` -> 200 + 46932 字节明文；
`gzip` -> 200 + 8201 字节 gzip + 小写头；http_client 默认 `gzip, deflate` -> **崩**。

**修法**：新增 `_header_get(headers, name, default)` 做**大小写不敏感**查找，三处调用点改用它。
其中 `encoding` 推导那处原本还有 **`resp_headers["Content-Type"]` 下标访问** —— 头名小写时会
**KeyError 让整次请求失败**（比"取不到 charset"严重，当时只报了大小写那半）。
验证：`get_json()` 现在直取 BIS 拿到 dict；`_decompress` 对 gzip/GZIP/deflate/裸 deflate/None/
未知 `br`/坏 gzip 七种输入逐个验过，行为与修前约定一致。
`bis_client._decode_body` 的 magic 兜底保留，重新定位为「兜住回了压缩流却不声明头的服务器」。

### 3.14 ⚠️ scan_materialized 的去重从来没生效（静默重复计数，已修）

**根因**：`scan_materialized()` 用 `missing._auto_series_key` 推行键，得到
`source|region_code|indicator_id`；而声明式清单用的、`materialize-validated.py` **写进落盘文件**的
是规范键。实测**12 个落盘文件里 12 个都对不上**（`nbs_gdp_cny_100m`：`nbs|gdp|cny_100m`
vs `nbs|000000000000|db8e5a86…`）。于是 `if key in declared_keys: 跳过` **永远不命中** ——
同一序列被扫两遍、缺口段算两遍。

**修法**：改为**产物自带的 `series_key` 优先，缺失才退回 `_auto_series_key`** ——
这正是 `fill_strategy._scan_validated_series()` 与知识库 `fields.series_key` 早就写明的规则。

| 指标 | 修前 | 修后 |
|---|---|---|
| `n_series` | 23 | **13** |
| 缺口段合计 | 10 | **5** |

即**此前约一半的统计是重复计数**。两条断言修前修后都 PASS，所以门禁抓不到 —— 它只在**数字**上错。

### 3.15 ⚠️ 「行数」不等于「有数据」——探测/校验一律数非空值

NBS 对**没有发布的期**会回**占位行**：`dt_name` 有值（"2024年1月"）、`v` 是**空串**、
`du` / `i` 也全空。所以 `len(raw)` 正常而内容全空。第一版 catalog 终验就是按行数判的，
把 3 条映射判成通过，其中 **1 条是真假通过**（NBS 外商直接投资：2024 年 12 行全空）。

- **判据改成「非空值个数」**后，三个"有行无值"的序列立刻分成了两类：
  - **`industrial_production` / `retail_sales`：没问题**（12 期里 10 期有值）——
    空的是 **1 月**，NBS 1-2 月合并发布，**这是发布制度不是缺数据**
  - **`fdi`（NBS）**：2015/2018 全年 12 期都有值，**2021/2024 全空** -> 真停更。
    精确到日：实测 2014-01..**2019-11** 共 71 个非空期，末值 124394，2020-01 起全占位
    （证据 `diag_range.json`）。已按"有数据但停更"收录并在 YAML 写明 `data_until: 2019-11`
- **"某个窗口全空"不等于"这个源不支持"**，必须换窗口再确认一次。实测三例：
  `worldbank|GC.DOD.TOTL.GD.ZS` 换到 **1960-2025** 共 66 行**仍全 null**，
  `imf|BX_GDP` / `imf|BM_GDP` 对 CHN 直接 **0 行** —— 这才敢下"源不支持"的结论。
  三条**留痕不删除**（probe 脚本标 `expect_unavailable`，进 `verified.json`），后人不必重探
- 同源坑：`世界银行 /v2` 对没有数据的年份回 `value: null` 的**行**（不是不返回行），
  所以 WB 侧同样只能数非空值

### 3.16 中文列宽不能用 `len()`；`display_name` 必须可检索

- `len("国内生产总值") == 6`，但终端里占 **12 列**。用 `len()` 补空格会让中文列全部串位。
  `tools/edh.py` 的 `_display_width()` 按 `unicodedata.east_asian_width` 判断 W/F 记 2，
  是全项目唯一的宽度函数 —— 再写表格时复用它
- 别名索引起初只收了 `name` + `aliases`，**漏了 `display_name`**，
  于是中文用户最可能打的 `居民消费价格指数` **查不到**（自检场景 [2] 当场抓出）。
  中文名是最自然的输入，必须进索引

### 3.17 NBS 目录树里的 name 带尾随空格

`getCatalogsAndIndexTree` 返回的 `name` 大量带**尾随空格**（如 `"人均国内生产总值 (元) "`）。
用 `^...$` 锚定匹配会**一个都命中不了**（第一次跑 `nbs_targets` 16 个键里 8 个全空）。
正则结尾一律用 `\s*$`。同源：`get_catalog_tree(cid)` 的节点字段是
`publicrelease_web_dacatalog_id`，把它当 `dt` 传进去反而取不到数（实测 0 行），
**只有 `dt=""` 是对的**，别自作聪明去填数据表 id。

### 3.18 ⚠️ 三个"看着对、其实错"的输出层坑（方向 E 第二轮实测）

都发生在**输出/簿记**层，不是取数逻辑错 —— 所以不会让任何断言变红，只会让用户看到错的东西。
三个都是自检/实测抓出来的（细节见 `fetcher.py` / `tools/edh.py` 的同名 docstring）：

- **模块级"最近一次状态"被循环里的下一次调用清空**：`fetch_indicator` 开头 `_NOTES.clear()`，
  于是 `fetch_all_sources` 每取一个源就抹掉前一个源的记录（4 个源显示 1 个）。
  **清空要放在公开入口**，内部函数只追加 + 另给 `reset_notes()`。同源坑：任何
  `last_meta()` 风格的模块级状态都要问"谁负责清、什么时候清"
- **`csv.writer` 默认行尾 + Windows stdout 翻译 = 每行多一个空行**：
  默认 `lineterminator="\r\n"`，Windows 的 `sys.stdout` 再把 `\n` 翻成 `\r\n`
  -> `\r\r\n`，实测 130 行变 260 行。修法 `lineterminator="\n"`。
  **验收要看行数，不是看"有没有输出"**
- **用字符串相等判"单位是否可比"会误报**：unit 是人工写的自由文本，同口径因频率不同
  写法不同（WB `指数（2010=100）` vs BIS `指数（2010=100，月度）`），`==` 会把
  **本来可比**的一对判成"口径不同"。但也不能粗暴归一化 —— `指数（上年=100）` 与
  `指数（2010=100）` **必须**保持不同。`_unit_key()` 只剥频率词，**基期数字原样保留**

## 4. 架构图

### 4.1 主干（从上到下）

    采集层                 规范化层            分层落盘
    ------------------------------------------------------------------
    http_client.py  --->  normalize.py  --->  data/raw（HTTP 原文）
    + 五个 client                              data/parsed（按请求指纹）
    (nbs/wb/imf/fred/bis)                      data/validated（长表）
                                        |
                    missing.py（四分类）-> fill_strategy.py（决策，不插值）
                                        |
                    export.py -> data/output（CSV / SQLite / 字典）；report.py -> report.html

    旁路：source_profiles.yaml + validated 血缘 -> source_profiler -> arbiter -> credibility
    拼接：splicer.py（+ tools/splice-cpi.py）-> data/validated/spliced/
    产品：catalog_data.yaml -> catalog.py -> tools/edh.py（list / info；fetch 下一轮）
    横切：tools/run-all-checks.py —— 26 项门禁，任何改动后必跑

### 4.2 每层职责与产物

| 层 | 模块 | 输入 | 产物 |
|---|---|---|---|
| 采集 | http_client + 五个 client | URL / 参数 | `data/raw/_http_cache/*.bin`（HTTP 原文） |
| 规范化 | normalize.py | 原始观测 | `data/parsed/<source>/…`；`data/validated/<source>/*.json` |
| 缺失分类 | missing.py | 长表行 | 缺口 + 四分类（无独立落盘，进 missing_report.json） |
| 策略 | fill_strategy.py | validated + missing_report | `data/processed/<source>/…_processed.json` |
| 拼接 | splicer.py + tools/splice-cpi.py | 两条同指标序列 | `data/validated/spliced/`（结果 + 重叠期 + 断点 + verdict） |
| 输出 | export.py / report.py | processed | `data/output/`：CSV / SQLite / 字典 / report.html |
| 画像 | source_profiler + arbiter + credibility | 知识库 + validated + missing_report | 内存画像与落盘报告 |
| **目录** | catalog.py + catalog_data.yaml + tools/edh.py | 用户输入的指标名 | （无落盘产物）stdout 表格 / JSON；下一轮起 `edh fetch` 走采集层 |

**目录层在整条链的入口**：它不生产数据，只把「指标名」翻译成「哪一层、什么参数」。
`fetch` 做出来之后，链路是 目录 -> 采集 -> 规范化 -> …；目录层自己**不碰** data/ 任何一层。

**拼接层的位置**：语义上在 validated 之后、processed 之前，但**本轮刻意不接进 processed**
（`fill_strategy` 不认识拼接产物，硬接要动它的扫描规则）。拼接产物落在 `data/validated/spliced/`，
它没有 `value` 键的顶层 rows 结构，所以不会被落盘扫描误当序列（§3.8）。

### 4.3 落盘信封结构

- **raw**：`<sha16>.bin` + 同名 `.meta.json`（url / status / headers / 请求体）
- **parsed**：`request / raw_cache / data / fetched_at`
- **validated**：`name / row_count / written_at / columns / rows`，外加 materialize-validated 追加的
  `series_key` 与 `expected_periods`
- **processed**：`rows / missing_report / decisions / processed_at`

### 4.4 插件与 preset 装配

- 五个 adapter 行写在 `.dsh/.agent-presets/econ-harvester/agent.cordis.yml`，与 persona 等行并列
- 插件是**薄壳**：只做 argv 翻译与 stdout 信封翻译，采集/解析全在 Python 侧
- 不使用 `defineTool`（项目根没有 node_modules），手写同形状定义对象；**不使用 JS 模板字符串**
- `.dsh/econ-harvester.patch.yml` 用绝对路径把五个 adapter 作为 global insert 注入

## 5. 下一步待办

### 5.1 / 5.2 方向 A（已完成，压缩存档）

- **第二轮 arbiter**：产出 `arbiter.py` + 报告落盘 `data/validated/arbiter/`（7 对 + `_index.json`）；
  实测差异读 `cross_check/*.json` **不重跑取数**；高影响 unknown -> 人工复核，alignment 记画像与实测是否同调
- **第三轮 credibility**：`credibility.py` + `data/validated/credibility/`（12 条 + `_index.json`）；
  五维加权 -> 0-100 + high/medium/low。当前 9 high / 3 medium；两条只对比不落盘的序列 provenance 只有 30

### 5.3 待办（按优先级）

- **① 方向 E 第三轮：`edh export` / 落盘（高，本轮的直接续作）** —— 现在 `edh fetch`
  只把数据**打给用户**（stdout / `--output`），**不落任何一层** data/。两条路：
  ① 让产物进 `data/validated/`（要解决**命名冲突**：目录的 `CPI` vs 知识库的
  `nbs|cpi|全国居民消费价格指数（上年=100） (%)` 是两套键，得定映射规则，否则
  `source_profiler` 会因查不到 key 抛 KeyError）；
  ② 保持轻量，只加 `--format parquet` / 多指标批量取数（`edh fetch GDP CPI M2`）。
  **倾向前者** —— 它才把"用户产品"和既有的验证/画像/报告链路接上
- **② 多指标批量取数（中）** —— 目录能查 18 个指标，但 `edh fetch` 一次只取一个。
  批量要先定"多指标的 CSV 怎么合"（`indicator` 列已在，主要是窗口/频率取交集的问题）
- **③ 拼接产物进 processed / 导出（中）** —— 现在只到 `data/validated/spliced/`。要进导出链路，
  得先让 `fill_strategy` 认识拼接产物（它现在只认 `rows` 长表）
- **④ `RELATIVE_METRIC_FLOOR` 按量纲配置（低）** —— 见 §5.4，0.5 只适配百分点量纲
- **⑤ ICP 2021 单独立项** —— CPI 维度已证不可达（§3.10），但**价格水平**维度的独立测量存在：
  世界银行 ICP 是各经济体**自己采集**一篮子代表品，2021 轮中国**参加了**（NBS 2024-05 自行发布过结果）。
  可用它验 PWT 的 `pl_gdpo` 或 OECD `DF_TABLE4` 的中国 PPP —— 一方官方采集、一方多边化处理，
  这才是真交叉验证。**立项前需先解 §3.11 的 TLS 证书链**（PWT 侧）
- **⑥ `GOVERNMENT_DEBT` 只有单源，无法交叉验证（低）** —— 目录实测 NBS 无此指标、
  World Bank 对中国全 null，只剩 IMF `GGXWDG_NGDP`。要做交叉验证得引新源（BIS 债务证券？
  财政部？），本轮范围外，先记着
- **方向 C 已完成五轮**（全部结项）：① FRED CPI ② 加独立源 -> 探测判定**不可达，勿重开**
  ③ 接 BIS ④ 修 http_client + 拼接器 ⑤ rebase。**方向 C 至此收尾**
- **方向 D 已完成四轮**：HTML 报告 / 血缘 + 门禁状态 / 修 export alias 重复 / 图表内联；
  后续可选导出 PDF、挂 CI（CI 用 `report.py --test --offline`，冷检出要先预热缓存）
- **未做**：PROV-JSON 血缘；落地插值（true_gap = 0 例，故意留空，出现 `interpolate` 会抛 NotImplementedError）
- **方向 B（暂停，需单独立项）**：桌面版装配链路。本会话真正生效的是 `.dsh/econ-harvester.patch.yml`
  的 global insert，不是 preset 的 persona；共有三层注册机制、两份 preset 副本

### 5.4 已知的小尾巴

- **`splice()` 的参数 `rebase` 遮蔽了同名函数 `rebase()`**：函数体内 `rebase` 是字符串，
  直接 `rebase(...)` 会抛 `TypeError: str object is not callable`。**实现体因此改叫 `rebase_series`，
  `rebase` 保留为公开别名**（`rebase = rebase_series`）。新增同名参数时先想清楚是否遮蔽了模块级名字。
- **rebase 只调水平、不调斜率，`trend_break` 修不了。** 当前 CPI 拼接的 verdict=需桥接是
  **`trend_break`** 触发的（magnitude 1.3590），**不是 `level_jump`**（它 excess_ratio 仅 0.3378，本判 ok）。
  `splice()` 的断点检测里两类是独立项，rebase 只可能影响 `level_jump`。**别指望 rebase 能改善本用例。**
- **rebase 的落点是 B 的非重叠期 —— 若 B 被 A 的时间跨度完全包住，就是空操作**（`applied=False`，
  记 `not_applied_reason: no_non_overlap`）。本 CPI 用例正是这种形状。**"没执行"与"执行了但无效"必须分开读。**
- **任何"相对阈值"都要先问分母的量级**：拼接器判水平跳跃时，中国 CPI 2014→2015 是 2.06%→1.4% 的
  **真实变化**，相对量 0.32 却一度被判 reject「不可拼接」。已加 `excess_ratio`（绝对跳变 / 接缝前同源
  步长中位数），超额 ≤1 降为 ok。同一课在 `overlap.max_diff_rate` 上又犯一次（0.0 vs 0.051 算出"分歧 100%"）。
- **`RELATIVE_METRIC_FLOOR = 0.5` 是为百分点量纲标定的**，不普适（亿元量纲要放大）。没做成自适应是因为
  实测那样会**反向漏判**（一路降到 0 的序列，中位量级本身也小）。
- **`_grade()` 的阈值边界是严格 `>`**：人工造 5% 台阶时 `|105-100|/100` 浮点上是 0.04999999999999999，
  会判 **ok**。要造明确落在 warn 带的夹具，取 6%~9%。
- **arbiter 曾按键名猜语义**：compare-cpi 的顶层键 `max_abs_diff_pp` 命中 `_adapt` 的 GDP 指数形状，
  产出过一条错配的第 7 对。已改数据驱动 —— **按键名猜语义，迟早错配**。
- aggregated_from_months 只在 validated / processed 的 JSON 层，**不在 CSV / SQLite 里**（14 列是定案集合）。
- compare-gdp-3way 耗时波动大（16.5s -> 85.1s）；**arbiter 的 verdict 与 recommended_action 可能不同调**
  （已用 `alignment=profile_stricter` 显式记录）。

## 6. 关键文件地图

- **`python/econ_core/`** —— 生产层。逐文件职责见 §1.1（那里更细），此处不重复。
  **两个 YAML 是人工编纂的知识文件，不是生成物**：`source_profiles.yaml`（来源画像，§2.8）与
  `catalog_data.yaml`（指标目录，§2.13）。改它们不改代码，但都要跑各自的自检。
- **`tools/`** —— 门禁与加工层：
  `run-all-checks.py`（门禁总入口，28 项，失败即停，支持逐检查 timeout 覆盖）·
  `edh.py`（**用户 CLI**：`list` / `info` / `summary` 纯离线，`fetch` **联网** —— §2.14）·
  `check-cli-envelope.py`（契约测试，真跑 12 子命令 / 13 用例，R1-R11，420s）·
  `smoke-{nbs,worldbank,imf,fred,bis}-adapter.mjs`（五个插件的 argv 桩测）·
  `verify-preset.mjs`（preset 装配校验，**不在门禁里，手工跑**）·
  `compare-{gdp,gdp-3way,gdp-real,unemployment,cpi}.py`（五条交叉验证）·
  `scan-missing.py`（声明式清单 + 落盘扫描 -> missing_report.json，含两条已知断言）·
  `materialize-validated.py`（清单落盘 validated，**复用 scan-missing 的清单不复制**）·
  `run-fill-strategy.py`（validated -> processed，内置 raw 快照对比证明脱网）·
  `splice-cpi.py`（真实拼接 + 三模式 rebase 对比）·
  `export.py`（processed -> CSV/SQLite/字典）· `report.py`（5 份 JSON -> 单文件 HTML）
- **`src/plugins/` 与 `.dsh/`** —— 会话装配：
  五个 adapter（nbs / worldbank / imf / fred / bis，都是**薄壳**：argv 翻译 + stdout 信封翻译）+
  `hello.js`（最小宿主插件，只证明装配链路可激活）·
  `.dsh/.agent-presets/econ-harvester/agent.cordis.yml`（插件行清单，五个 adapter + persona + shell + fs，用**相对路径**）·
  `preset.yml`（preset 名称与描述）· `.dsh/econ-harvester.patch.yml`（**绝对路径**把五个 adapter 作为
  global insert 注入宿主组合 —— 这是 `wb_*`/`imf_*`/`fred_*`/`bis_*` 工具能在会话里出现的机制）·
  家目录另有一份镜像（桌面版读取），**两份都要维护**
- **`data/`**（内容全被 .gitignore 忽略，只保留 `.gitkeep`）：
  `raw/_http_cache/`（HTTP 原文，所有结论的最终证据）· `parsed/<source>/`（按请求指纹）·
  `validated/<source>/`（12 条声明式长表）· `validated/{spliced,arbiter,credibility,cross_check}/` ·
  `validated/missing_report.json` · `processed/`（带缺失元数据）· `output/`（csv / db / 字典 /
  **report.html** / last_gate.json）· `raw/_probe_pwt_maddison/`（独立性探测证据物，不 commit）·
  `raw/_probe_catalog/`（**指标目录的取证物**：`verified.json` 逐条映射的取数结果、
  `nbs_index.json` 三个目录树拍平、`diag_range.json` 停更期、`diag_wb.json` 宽窗口复核）
- **项目根**：`package.json`（声明 `type: module`，使 .mjs/.js 插件按 ESM 加载，**不要删**）·
  `pip_sandbox_install.py` + `.gitignore`（忽略 .venv/.tools/node_modules 与 data 下五个子目录）

---

## 7. 新对话开场步骤

接手时按顺序做这四件事，**做完只报告状态，不要动代码，等指令**：

1. 读本文件 —— 建立全局认识
2. `git log --oneline -20` —— 判断哪些改动已固化、哪些还挂在 working tree
3. 跑门禁确认基线（期望 **28/28 PASS，exit 0**；若不足，先定位退化的那一项，不要叠加改动）：

       cd D:\universe\econ-data-harvester
       .\.venv\Scripts\python.exe tools\run-all-checks.py

4. 报告状态（照抄此模板）：

       门禁：28/28 PASS（exit 0）
       working tree：<git status --short 的内容>
       数据：12 条声明式序列 / 900 行 / 10 缺失行（按行：series_start 5, discontinued 3, not_yet_published 2, true_gap 0）
       目录：18 个指标 / 51 条源映射（3 条探过并确认不可用，未收录）

**改动后的固定动作**：① 开工前自检（pwsh ACL 故障见 §3.7；yaml/Jinja2 见 §3.6；validated 为空先跑
materialize-validated）；② 跑完整门禁确认仍 28/28（新增检查要同步加进 `CHECKS` 与 docstring 编号）；
③ 新增序列必须补 `source_profiles.yaml` 条目（否则 profiler 抛 KeyError）；
**③b 动 `catalog_data.yaml` 必须跑 `probe_catalog_sources.py verify` 复核**（§2.13）；
④ 改契约/分类/字段名要同步更新本文件第 2、3 节；⑤ **不要把 data/ 下的产物提交进 git**，
**不要删 raw 存档**（那是证据链）。

---

（本文件结束。细节请读对应模块的 docstring 与 `python/_probes/README.md` 的实测记录。）
