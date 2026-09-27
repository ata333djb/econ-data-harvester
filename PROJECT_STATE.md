# PROJECT_STATE.md —— EconDataHarvester 状态快照

- 生成时间：2026-09-27
- 面向对象：**新对话的 Agent**。读完之后应当能直接继续工作，不需要回看任何历史对话。
- 维护规则：每完成一轮实质改动就更新本文件。**只写状态**，不写历史对话，不粘贴代码，只给路径与一句话职责。

权威性顺序（冲突时以序号小的为准）：

1. 门禁 tools/run-all-checks.py 的**实际输出**（当前应为 19/19 PASS）——这是唯一硬标准
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

**规范化层**

- python/econ_core/normalize.py —— 长表规范化层。统一 14 列 schema + row_sha16 行指纹；含四个入口：normalize_observations（NBS）、normalize_worldbank_observations、normalize_imf_observations、normalize_cpi_wide（后三个是第四阶段新增，用于把 IMF / CPI 宽表纳入统一入口）
- python/econ_core/series_key.py —— series_key 规范化（别名解析 + 大小写变体）；export.py 与 report.py 共用的**去重依据**（**本轮新增**）

**CLI 与 DSH 插件**

- python/econ_core/nbs_client_cli.py —— NBS 四子命令 CLI（list-provinces / get-default-indicator / get-catalog-tree / fetch-indicator）
- python/econ_core/worldbank_client_cli.py —— World Bank 三子命令 CLI（fetch-indicator / list-indicators / list-countries）
- python/econ_core/imf_client_cli.py —— IMF 三子命令 CLI（fetch-indicator / list-indicators / list-countries）
- src/plugins/nbs-adapter.js —— 注册 nbs_fetch_indicator / nbs_get_catalog_tree / nbs_get_default_indicator / nbs_list_provinces 四个工具（薄壳转发 CLI）
- src/plugins/worldbank-adapter.js —— 注册 wb_fetch_indicator / wb_list_indicators / wb_list_countries 三个工具
- src/plugins/imf-adapter.js —— 注册 imf_fetch_indicator / imf_list_indicators / imf_list_countries 三个工具
- src/plugins/hello.js —— 最小宿主插件，只用来证明 preset 装配链路可激活（不参与业务）

**验证与分析层**

- python/econ_core/cross_validation.py —— 两条序列逐期比对；差异率四段阈值（一致/可接受/警告/冲突）+ summary 三值结论（可直接拼接/需人工复核/不可拼接）
- python/econ_core/missing.py —— 缺失检测与**四分类**：true_gap / not_yet_published / discontinued / series_start（判定优先级：先尾部 >=3 年，再尾部 <3 年，再头部，最后中段）
- python/econ_core/fill_strategy.py —— 填补策略执行器：把决策写成行级元数据，**当前不执行任何插值**（出现 interpolate 直接抛 NotImplementedError）
- python/econ_core/source_profiler.py —— 来源画像：profile_series / compare_profiles / explain_divergence（**本轮新增**）
- python/econ_core/source_profiles.yaml —— **人工编纂**的来源画像知识库：3 个发布机构 + 10 条指标口径 + 18 个字段语义 + 口径家族表
- python/econ_core/arbiter.py —— 口径判定仲裁：把「画像判定」与「实测差异（读 cross_check 产物）」配成一条记录，给出 同口径 / 可桥接 / 不可拼接 / 人工复核 四值判定 + alignment（画像与实测是否同调）
- python/econ_core/credibility.py —— 五维可信度评分（Expertise 0.25 / Provenance 0.20 / Timeliness 0.10 / Transparency 0.15 / Coherence 0.30）-> 0-100 分 + high/medium/low（**本轮新增**）

**输出层**

- tools/export.py —— processed → CSV（data/output/econ_data.csv）+ SQLite（data/output/econ_data.db，含 observations 表 / idx_key_period 索引 / series_summary 视图）+ Markdown 数据字典（data/output/data_dictionary.md）
- tools/report.py —— 把 5 份 JSON 报告合成**单文件 HTML**（data/output/report.html，非技术同事可直接看）

**文档**

- python/econ_core/README.md —— 生产层已知上游事实（失业率两条口径、登记失业率 2022 起停更等）
- python/_probes/README.md —— 逆向探测脚本索引与已知观察（NBS 接口考古、缓存键缺陷等）
- PROJECT_STATE.md —— 本文件

### 1.2 门禁：19 项（tools/run-all-checks.py，当前 19/19 PASS）

| # | 检查 | 类型 | 说明 |
|---|---|---|---|
| 1 | smoke-nbs-adapter | node | NBS 插件 argv 拼装（桩替换 execFile，不 spawn） |
| 2 | smoke-worldbank-adapter | node | World Bank 插件 argv 拼装 |
| 3 | smoke-imf-adapter | node | IMF 插件 argv 拼装 |
| 4 | check-cli-envelope | python | 真跑 10 个子命令（11 用例）校验 stdout 信封契约 R1-R11；timeout 420s |
| 5 | missing --test | python | 缺失四分类自检（纯离线，约 0.3s） |
| 6 | source_profiler --test | python | 来源画像自检，8 个场景（纯离线，约 0.4s） |
| 7 | arbiter --test | python | 口径判定自检，7 个场景（纯离线，约 0.3s） |
| 8 | normalize --test | python | 规范化层自检（含 row_sha16 唯一性、i_name null 补齐） |
| 9 | cross_validation --test | python | 交叉验证四段阈值自检 |
| 10 | compare-gdp | python | NBS vs World Bank 中国 GDP 端到端 |
| 11 | compare-gdp-3way | python | NBS vs WB vs IMF 三方（汇率取自 WB PA.NUS.FCRF）；timeout 420s |
| 12 | compare-gdp-real | python | NBS GDP 指数 vs IMF NGDP_RPCH 实际增速（无汇率污染） |
| 13 | compare-unemployment | python | 登记失业率 / 调查失业率 / IMF LUR 三方（百分点） |
| 14 | scan-missing | python | 缺失检测与分类扫描，产出 data/validated/missing_report.json；timeout 420s |
| 15 | materialize-validated | python | 声明式清单落盘 data/validated/；timeout 420s |
| 16 | run-fill-strategy | python | validated → processed（零填充，**已脱网**，约 0.4s） |
| 17 | credibility --test | python | 可信度评分自检，5 个场景（纯离线，约 0.6s） |
| 18 | export | python | processed → CSV / SQLite / 数据字典（纯离线） |
| 19 | report | python | 5 份 JSON → 单文件 HTML 质量报告 + 7 项自检（纯离线，约 1s） |

门禁的运行顺序**有依赖**：materialize-validated（15）-> run-fill-strategy（16）-> credibility（17）-> export（18）-> report（19）最后。credibility 读 validated + processed + cross_check 三样产物，所以不能挪到 arbiter 旁边（干净检出时会误报）。check-cli-envelope 与三个 compare 脚本是网络密集型，超时放宽到 420s。

### 1.3 当前数据规模

- **声明式序列数**：9 条（NBS 6 + World Bank 1 + IMF 2）。missing_report.json 里的 n_series=19 是**含重复**的：声明式 9 条 + 落盘扫描 10 条键，同一条序列被算两遍
- **总行数**：142 行（9 条落盘序列的全部年份合计）。历史提醒：早期写的 152 是**重复计数**（alias 副本 `nbs|000000000000|db8e...` 与 `nbs|gdp|cny_100m` 是同一条序列，被算了两遍）。**export.py 现在按规范键去重**，所以 CSV / SQLite / 数据字典也都是一致口径
- **缺失行数**：10 行（value 为 null / 空串 / NULL，**一个都没有填补**）
- **缺失分类（按行统计）**：series_start 5 / discontinued 3 / not_yet_published 2 / true_gap **0**
- **缺失动作（按行统计）**：leave_null 8 / wait 2 / interpolate 0
- **row_sha16 覆盖率**：142/142（全部唯一）
- 落盘位置：data/validated/（9 个长表 JSON + missing_report.json）→ data/processed/（10 个带缺失元数据的 JSON）→ data/output/（CSV + SQLite + 字典）
- 知识库另有 **2 条只用于对比、未落盘**的序列（nbs|gdp|index_prev_year_100、imf|NGDP_RPCH），所以知识库规范键 11 条 > 落盘序列 9 条

10 个 series_key（9 条声明式序列 + 1 条别名键；知识库必须与之一一对应）：

1. nbs|gdp|cny_100m
2. nbs|cpi|全国居民消费价格指数（上年=100） (%)
3. nbs|cpi|城市居民消费价格指数（上年=100） (%)
4. nbs|cpi|农村居民消费价格指数（上年=100） (%)
5. nbs|registered_unemployment（停更于 2021）
6. nbs|surveyed_unemployment（起点 2018）
7. worldbank|NY.GDP.MKTP.CN
8. imf|NGDPD
9. imf|LUR（起点 2017）
10. NBS|000000000000|db8e5a86c08246e79b1b11251927e740（**别名**，指向第 1 条）

### 1.4 最近一轮新增（方向 D 第四轮：图表内联）

- tools/report.py —— Plotly 2.27.0 **内联**进 HTML（首次联网拉一次落 raw 缓存，之后离线复用）；新增 `--offline`（无缓存即报错，不联网）
- 报告 46KB -> **3.6MB**，从此**断网双击也能看**（这是「发给同事就能用」的最后一个硬缺口）
- 自检第 1 项阈值 30KB -> 3MB；第 3 项改为「无外部 script 引用 + 有内联标记」
- 注意：Plotly bundle 自己内部含 1 处字符串 cdn.plot.ly（topojsonURL 默认值），不是外部引用

---

## 2. 关键约定（未来必须遵守）

### 2.1 运行环境（硬编码，不要改成 PATH 里的 python）

- **解释器**：D:\universe\econ-data-harvester\.venv\Scripts\python.exe
- **工作目录（cwd）**：D:\universe\econ-data-harvester（项目根）
- **PYTHONPATH**：D:\universe\econ-data-harvester\python（让 econ_core.* 可解析）
- **PYTHONIOENCODING**：utf-8（否则 Windows 控制台会乱码）
- **为什么必须用 venv**：本机 Schannel 凭证库不可用，非 OpenSSL 栈会 TLS 失败；http_client.assert_venv() 会强制拦截
- Node 侧脚本用系统 node（tools/*.mjs 通过 shutil.which("node") 或直接 node 命令）

### 2.2 CLI 契约（三个 *_client_cli.py 共同遵守）

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

---

## 3. 已知的坑（不要再踩）

### 3.1 NBS 的 cid 参数被后端忽略

- 现象：把 fetch_indicator_data 的 cid 改成任意错值，返回的观测**逐字节相同**（同一 raw 存档 sha256[:12]）
- 原因：getEsDataByIndicatorIdAndDa 实际按 id（tree_node_id）+ rootId + da + dts 选序列；响应里根本没有 cid 字段
- 处置：**不改签名**，只在 nbs_client.fetch_indicator_data 的 docstring 里记录了实测证据
- 副作用（已知未修）：cid 参与 parsed 落盘的文件名指纹，所以错 cid 会写出**另一个指纹的副本**，内容与正版相同。详见 python/_probes/README.md 的「已知观察」

### 3.2 IMF 的 Akamai 会 403 掉 Chrome UA

- 现象：用 http_client 默认头（Chrome 128 UA）请求 api.imf.org 的 DataMapper -> HTTP 403 AkamaiGHost Access Denied（连站点根 https://www.imf.org/ 也 403）
- 解法：**只把 User-Agent 换成 python-urllib/3.12**，其余默认头保留，同一 URL 立刻 200 + application/json。方向与「伪装成浏览器」相反
- 已固化在 imf_client.IMF_HEADERS。**不要顺手改回 Chrome**
- 同源坑：/v1/{indicator}/{country} 的 country 路径段**不做服务端过滤**（实测 /NGDPD/CHN 仍返回 229 个国家），过滤必须在客户端做

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

**展示层**

- shutil.which("node") 在本机解析到带空格的路径（D:\New Folder\node.EXE），失败块里的命令必须给含空格的参数加引号才能复制粘贴执行

### 3.9 缺失统计有两个口径，别混用（也不要把大写 NBS 目录当 bug）

- **按行**（本文件 1.3 用的口径）：10 行 = series_start 5 + discontinued 3 + not_yet_published 2
- **按缺口段**（data/validated/missing_report.json 的 by_classification_*）：series_start 4 / discontinued 2 / not_yet_published 4，且声明式与落盘扫描各扫一遍，同一缺口被算两次
- 报数时必须写明口径；run-fill-strategy 打印的决策数是**段**口径，processed 行里的元数据才是行口径
- data/processed/ 下 NBS 序列落在**大写 NBS/** 目录，worldbank / imf 是小写。fill_strategy 已做 .lower()，这是 Windows 复用旧目录名的历史遗留，干净检出不复现

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

    横切: tools/run-all-checks.py —— 19 项门禁，任何改动后必跑

### 4.2 每层职责与产物

| 层 | 模块 | 输入 | 产物 |
|---|---|---|---|
| 采集 | http_client.py + 三个 client | URL / 参数 | data/raw/_http_cache/*.bin（HTTP 原文） |
| 规范化 | normalize.py | 原始观测 | data/parsed/<source>/<endpoint>_<sha16>.json；data/validated/<source>/*.json |
| 缺失分类 | missing.py | 长表行 | 缺口 + 四分类（无独立落盘，进 missing_report.json） |
| 策略 | fill_strategy.py | validated + missing_report | data/processed/<source>/<name>_processed.json |
| 输出 | export.py | processed | data/output/econ_data.csv、econ_data.db、data_dictionary.md |
| 画像 | source_profiler.py | 知识库 + validated + missing_report | 内存画像（供下一轮 Arbiter 消费） |

### 4.3 落盘信封结构（三层各有固定形状）

- **raw**：data/raw/_http_cache/<sha16>.bin 加同名 .meta.json（url / status / headers / 请求体）
- **parsed**：request / raw_cache / data / fetched_at
- **validated**：name / row_count / written_at / columns / rows，另有两个由 materialize-validated 追加的字段 series_key 与 expected_periods
- **processed**：rows / missing_report / decisions / processed_at

### 4.4 插件与 preset 装配

- 三个插件行（nbs-adapter / worldbank-adapter / imf-adapter）写在 .dsh/.agent-presets/econ-harvester/agent.cordis.yml，与 persona 等行并列
- 插件是**薄壳**：只做 argv 翻译与 stdout 信封翻译，采集/解析逻辑全在 Python 侧
- 插件不使用 defineTool（项目根没有 node_modules），手写同形状定义对象；**不使用 JS 模板字符串**
- .dsh/econ-harvester.patch.yml 用绝对路径把三个 adapter 作为 global insert 注入——这是 wb_* / imf_* 工具能在会话里出现的机制

---

## 5. 下一步待办

### 5.1 方向 A 第二轮：口径判定 Arbiter —— 已完成

- 产出：python/econ_core/arbiter.py + 报告落盘 data/validated/arbiter/（**6 对** + _index.json）
- 实测差异读 cross_check/*.json **不重跑取数**；高影响 unknown -> 人工复核，低影响 unknown -> 保留原判定并记 knowledge_gaps；alignment 记画像与实测是否同调（见 5.4）

### 5.2 方向 A 第三轮：可信度评分 —— 已完成

- 产出：python/econ_core/credibility.py + 报告落盘 data/validated/credibility/（11 条 + _index.json）
- 五维加权：Expertise 0.25 / Provenance 0.20 / Timeliness 0.10 / Transparency 0.15 / Coherence 0.30 -> 0-100 分 + high/medium/low
- 当前分布：8 high / 3 medium / 0 low；最高 nbs|surveyed_unemployment 95.5，最低 imf|NGDP_RPCH 77.25。两条只用于对比、未落盘的序列 provenance 只有 30（没有 series_file）

### 5.3 方向 D 与之后的待办

- **方向 D 已完成四轮**：① HTML 质量报告（8 节）② 数据血缘 + 真实门禁状态 ③ 修 export 的 alias 重复 ④ **图表内联（报告自包含，断网可看）**
- **方向 D 后续（可选）**：导出 PDF / 把报告挂到 CI（CI 用 `report.py --test --offline` 可复现，但要**先预热缓存**：冷检出没有 raw 存档，首次仍得联网拉一次 Plotly；门禁本身仍用不带 --offline 的 `report.py --test`）
- **方向 A 第四轮（可选）**：拼接断点检查 / PROV-JSON 溯源导出 —— 还没开始；注意系统至今**从未真正拼接**过序列，断点检查暂时没有对象
- **方向 C（下一轮主线）**：横向加源，范围锁定「一个源 + 一个指标」——把 **CPI** 接进交叉验证（当前 3 条 CPI 序列零验证，占落盘序列的 1/3）
- **方向 B（已暂停，需单独立项）**：桌面版装配链路。实测本会话真正生效的是 .dsh/econ-harvester.patch.yml 的 global insert，不是 preset 的 persona；共有三层注册机制、两份 preset 副本
- **落地插值**：当前 true_gap = 0 例，所以插值实现故意留空（出现 interpolate 会抛 NotImplementedError）。等真遇到上游序列中断再实现，并同步补回归

### 5.4 已知的小尾巴

- aggregated_from_months 只存在于 validated / processed 的 JSON 层，**不在 CSV / SQLite 里**（输出层 14 列是定案集合，export.py 未改）。要它可见就追加为第 15 列，或折进 missing_evidence
- compare-gdp-3way 的 timeout 已放宽到 420s，但实测波动大（16.5s -> 85.1s），若再变慢要考虑拆项
- arbiter 的 verdict 与 recommended_action 可能不同调（gdp × imf|NGDPD：画像=可桥接，实测=noise/splice）。两个字段各自独立、不强行统一，但已用 alignment=profile_stricter 显式记录；要真正统一得改 source_profiler 的归因规则

---

## 6. 关键文件地图

### 6.1 python/econ_core/ —— 生产层

逐文件的职责与实测细节见 **1.1 已完成模块**（那里更细）。以下只列 1.1 未展开的部分。

### 6.2 tools/ —— 脚本层（门禁、对比、加工、输出）

| 文件 | 一句话职责 |
|---|---|
| run-all-checks.py | 门禁总入口：顺序跑 19 项检查，失败即停；支持逐检查 timeout 覆盖 |
| check-cli-envelope.py | 契约测试：真跑 10 个子命令（11 用例），按 R1-R11 校验 stdout 信封；超时 420s |
| smoke-nbs-adapter.mjs | NBS 插件冒烟：桩替换 execFile，验证 argv 与必填校验 |
| smoke-worldbank-adapter.mjs | World Bank 插件冒烟（含 wantArgv 精确比对） |
| smoke-imf-adapter.mjs | IMF 插件冒烟 |
| verify-preset.mjs | preset 装配校验（不起 DSH 服务）：ESM 加载、cordis 激活/卸载、YAML 形状、相对路径解析 |
| compare-gdp.py | NBS vs World Bank 中国 GDP（首个交叉验证，结论：逐位相同） |
| compare-gdp-3way.py | NBS vs WB vs IMF 三方（统一到亿美元；汇率取 WB PA.NUS.FCRF） |
| compare-gdp-real.py | NBS GDP 指数 vs IMF NGDP_RPCH 实际增速（无汇率污染口径） |
| compare-unemployment.py | 登记失业率 / 调查失业率 / IMF LUR 三方（百分点阈值） |
| scan-missing.py | 缺失扫描：声明式清单 + data/validated 落盘扫描，产出 missing_report.json（含两条已知断言） |
| materialize-validated.py | 把声明式清单落盘 data/validated/（复用 scan-missing 的清单，不复制） |
| run-fill-strategy.py | 一键 validated -> processed；内置 raw 缓存快照对比证明脱网 |
| export.py | 输出层：processed -> CSV（utf-8-sig）+ SQLite（表/索引/视图）+ Markdown 数据字典；按规范键去重 alias 副本 |
| report.py | 展示层：5 份 JSON -> 单文件 HTML 质量报告（Plotly 走 CDN）+ 7 项自检 |

### 6.3 src/plugins/ 与 .dsh/ —— 会话装配

| 文件 | 一句话职责 |
|---|---|
| src/plugins/hello.js | 最小宿主插件，仅用于验证 preset 装配链路 |
| src/plugins/nbs-adapter.js | 四个 nbs_* DSH 工具（薄壳转发 nbs_client_cli） |
| src/plugins/worldbank-adapter.js | 三个 wb_* DSH 工具 |
| src/plugins/imf-adapter.js | 三个 imf_* DSH 工具 |
| .dsh/.agent-presets/econ-harvester/agent.cordis.yml | preset 的插件行清单（三个 adapter + persona + shell + 文件系统），用相对路径 |
| .dsh/.agent-presets/econ-harvester/preset.yml | preset 名称与描述（要点明装配了 NBS + World Bank + IMF 三套工具） |
| .dsh/econ-harvester.patch.yml | 用绝对路径把三个 adapter 作为 global insert 注入宿主组合 |
| .dsh/econ-harvester.patch.yml.bak | patch 的备份，改坏时可对照 |

家目录另有一份镜像（桌面版读取）：C:\Users\user\.dsh\.agent-presets\econ-harvester\ 下的 agent.cordis.yml 与 preset.yml。两份都要维护，家目录那份用绝对路径。

### 6.4 python/_probes/ —— 逆向考古脚本（不参与生产链路，只读证据，不要删）

- README.md —— 18 个 probe_*.py 的索引、每个的结论、缓存键缺陷的已知观察
- probe_*.py（实测 18 个）—— 端点发现 / 参数语义 / da 来源 / 缓存键缺陷 / 修复回归

### 6.5 数据目录（data/ 下内容全部被 .gitignore 忽略，只保留 .gitkeep）

| 目录 | 内容 |
|---|---|
| data/raw/_http_cache/ | HTTP 原文存档（.bin 加 .meta.json），所有结论的最终证据 |
| data/parsed/{nbs,worldbank,imf}/ | 按请求指纹的解析结果 |
| data/validated/{nbs,worldbank,imf}/ | 长表（9 条声明式序列），materialize-validated 的产物 |
| data/validated/missing_report.json | 缺失分类总报告（scan-missing 产物） |
| data/validated/{arbiter,credibility}/ | 口径判定报告（6 对）/ 可信度评分报告（11 条） |
| data/validated/cross_check/ | 三个对比脚本的结果 JSON |
| data/processed/{nbs,worldbank,imf}/ | 带缺失元数据的行（10 个文件） |
| data/output/ | 产品：econ_data.csv / econ_data.db / data_dictionary.md / **report.html** / last_gate.json（最近一次门禁状态） |

### 6.6 项目根其他文件

| 文件 | 一句话职责 |
|---|---|
| PROJECT_STATE.md | 本文件 |
| package.json | 声明 type: module，使 .mjs / .js 插件按 ESM 加载（不要删） |
| pip_sandbox_install.py | 本沙箱环境的 pip 安装包装，装 pyyaml / pandas / pyarrow 用它 |
| .gitignore | 忽略 .venv / .tools / node_modules，以及 data 下五个子目录的内容 |

---

## 7. 新对话开场步骤

新对话接手时，按顺序做这四件事，做完只报告状态，不要动代码，等指令：

1. 读 PROJECT_STATE.md（本文件）——先建立全局认识
2. git log --oneline -20 ——看重最近提交，判断哪些改动已固化、哪些还挂在 working tree
3. 跑门禁确认基线：

       cd D:\universe\econ-data-harvester
       .\.venv\Scripts\python.exe tools\run-all-checks.py

   期望 19/19 PASS，exit 0。若不是 19/19，先定位是哪一项退化了，不要继续叠加改动。
4. 报告状态：门禁结果、git status、当前数据规模（9 条声明式序列 / 142 行 / 10 缺失行），然后停下等指令

### 7.1 报告模板（建议照抄）

    门禁：19/19 PASS（exit 0）
    working tree：<git status --short 的内容>
    数据：9 条声明式序列 / 142 行 / 10 缺失行（按行：series_start 5, discontinued 3, not_yet_published 2, true_gap 0）
    下一步待办：方向 D 后续（可选）「图表内联 / 导出 PDF / 挂 CI」

### 7.2 改动后的固定动作

- 开工前自检：pwsh 报 ACL 故障见 3.7；import yaml / Jinja2 见 3.6；data/validated 为空则先跑 materialize-validated

1. 跑完整门禁，确认仍 19/19（新增检查要同步加进 CHECKS 与 docstring 编号）
2. 新增序列 -> 补 source_profiles.yaml 条目（否则 profiler 抛 KeyError）
3. 改契约/分类/字段名 -> 同步更新本文件的第 2 节与第 3 节
4. 不要把 data/ 下的产物提交进 git（它们已被忽略）；不要把 raw 存档删掉（那是证据链）

---

（本文件结束。全文不含任何代码片段；细节请读对应模块的 docstring 与 python/_probes/README.md 的实测记录。）

