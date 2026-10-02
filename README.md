# EconDataHarvester

**把中国宏观数据从五个官方源取回来，并且证明它没被取错。**

[English](README.en.md) · MIT · Python 3.12+

---

## 这是什么

一个命令行工具 + 一套数据可信度基础设施。给它一个指标名（`CPI`），它会去
**国家统计局 / 世界银行 / IMF / FRED / BIS** 五个源把数据取回来，合并成一张干净的宽表，
然后回答一个多数取数脚本不会回答的问题：

> 这几个源说的是一回事吗？如果不是，差在哪里、为什么、该信谁？

它不插值、不猜、不静默丢数据。**每一行都能追到最初那几个字节的 HTTP 响应。**

```bash
python tools/edh.py list                              # 18 个指标 / 51 条源映射
python tools/edh.py fetch CPI --from 2020 --to 2024   # 四源取数 + 交叉验证
python tools/edh.py export CPI --from 2020 --to 2024  # 落进验证层，接上下游
```

---

## 为什么要做这个

中国宏观数据有两个别人不太提的麻烦：

**一、同一个指标，不同源的口径根本不一样。**
World Bank 的中国 CPI 是「2010=100」的定基指数，国家统计局的是「上年=100」的同比。
两个都叫 CPI，**但把它们画在一张图上就是错的**。这个项目把「单位是否真的可比」
当作一个需要判断的问题，而不是假设它们可比 —— `CPI` 的四源六对组合里，
真正能直接比对的只有 World Bank 与 BIS 一对。

**二、很多"独立来源"其实不是独立的。**
项目里花了一整轮去验证中国 CPI 到底有没有独立源（`docs/ENGINEERING_NOTES.md` §3.10）。
结论是**没有**：BIS 只做拼接与重定基，OECD 转载 NBS，PWT 与 Maddison 用 OECD。
最硬的一条证据是 OECD 的中国 CPI 同比与 IMF WEO 的 `PCPIPCH` **逐年逐位完全相同** ——
这是同一份 NBS 序列被两次转载的指纹。

**不知道这件事的人，会以为自己在做四源交叉验证，其实是在拿一份数据跟自己比。**

---

## 快速开始

不需要装 Python 也能用 —— 见下面的[分发版](#不想装-python用分发包)。

```bash
git clone <this-repo> && cd econ-data-harvester

python -m venv .venv
.venv/Scripts/activate            # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt

export PYTHONPATH=$PWD/python      # Windows PowerShell: $env:PYTHONPATH="$PWD\python"
export PYTHONIOENCODING=utf-8      # Windows 控制台缺这个会乱码

python tools/edh.py list
python tools/edh.py fetch CPI --from 2020 --to 2024 --output cpi.csv
```

`fetch` 把 **CSV 打到 stdout、人话简报打到 stderr**，所以重定向永远得到干净的数据：

```bash
python tools/edh.py fetch CPI --from 2020 --to 2024 > cpi.csv   # 只有 CSV
```

---

## 它产出什么

| 产物 | 在哪 | 是什么 |
|---|---|---|
| 长表 JSON | `data/validated/<source>/` | 14 列固定 schema，每行带 `row_sha16` 指纹 |
| 带缺失元数据 | `data/processed/` | 每个缺口有分类与建议动作，**但值仍然是 null** |
| CSV / SQLite / 数据字典 | `data/output/` | 给下游用 |
| 单文件 HTML 报告 | `data/output/report.html` | 全部图表/样式内联，可离线打开、可直接发人 |
| 交叉验证结论 | `data/validated/cross_check/` | 每个源对的逐期差异 |
| 口径判定 | `data/validated/arbiter/` | 画像 × 实测 → 四值判定 |
| 可信度评分 | `data/validated/credibility/` | 五维加权 0-100 |

---

## 数据是怎么流的

```
五源 HTTP ──> 原始响应按内容指纹存档 ──> 解析 ──> 14 列长表
             data/raw/_http_cache/           data/validated/
                                                    │
              缺失四分类 ──> 填补策略（只标记，不插值）
              data/validated/missing_report.json ──> data/processed/
                                                    │
                        CSV / SQLite / 数据字典 / HTML 报告
                        data/output/
   旁路：知识库(source_profiles.yaml) + 血缘 ──> 画像 ──> 判定 ──> 评分
```

**关键设计：`data/raw/` 是证据链的根。** 每个 HTTP 响应按 `url+method+body` 的指纹存成
`<sha16>.bin` + 同名 `.meta.json`。所以任何一条结论都能回到原始字节，
而且重跑时能证明"结论变了是因为数据变了，不是因为我改了代码"。

---

## 几个刻意的取舍

这些是项目里反复坚持、也值得别人抄走的东西：

**不插值，永远不。** 缺失就是 `null`。缺失被分成四类（`series_start` / `discontinued` /
`not_yet_published` / `true_gap`），每类给出建议动作（`leave_null` / `wait` / `interpolate`），
但 `interpolate` 目前**没有任何代码路径会返回它** —— 真出现会直接抛 `NotImplementedError`。
宁可让用户看见洞，也不要给一个看起来完整的假表。

**"行数"不等于"有数据"。** 国家统计局对没发布的期会返回**占位行**：日期有、值是空串。
所以探测和校验一律数**非空值个数**。第一版按行数判，把 3 条映射判成通过，其中 1 条是假通过。

**交叉验证不替用户做单位换算。** 单位不同就判「口径不同」，把绝对差标成"仅供参考"，
**绝不硬报一个"冲突"**。字符串相等也不够用：`指数（2010=100）` 与 `指数（2010=100，月度）`
是同一口径，而 `指数（上年=100）` 必须与它们保持不同。

**HTTP 只用标准库。** 没有 `requests`、没有 `httpx`。整个网络层是 `urllib` + `ssl`，
依赖只有 PyYAML 与 Jinja2。这不是洁癖 —— 起因是开发机上 Windows 的 Schannel 凭证库不可用，
只有自带 OpenSSL 的解释器能连上数据源，项目索性把这条约束固化了下来。

**门禁真打网络。** `tools/run-all-checks.py` 有 30 项检查，其中多项**真的去请求五个源**
并断言响应内容。所以它慢（约 3 分钟）而且会受上游维护窗口影响 ——
CI 里跑的是纯离线子集，完整门禁在发版前本地跑。**这是刻意的**：
一个全部用 mock 的门禁，证明不了"今天还取得到数"。

---

## 开发

```bash
python tools/run-all-checks.py        # 30 项门禁，约 3 分钟，失败即停
```

改任何东西之后都应该跑它。门禁是**可重复的** —— 同一天连跑两次必须都是 30/30；
做不到就意味着出现了"后置依赖"（某项读的产物由排在它后面的项生成），
这个坑踩过一次，记在 `docs/ENGINEERING_NOTES.md` §3.20。

项目布局：

```
python/econ_core/     运行时包（24 文件 / ~8.9k 行）：五个 client、规范化、缺失、
                      画像、判定、评分、拼接、目录、取数适配、导出
python/_probes/       取证脚本（19 个）：当初怎么把 API 参数试出来的，留档
tools/                加工与门禁（17 文件 / ~4.8k 行）
src/plugins/          五个薄壳 adapter：argv 翻译 + stdout 信封翻译
docs/ENGINEERING_NOTES.md   工程日志：为什么这么做、踩过什么、什么被证伪了
```

**`docs/ENGINEERING_NOTES.md` 是这个项目最有意思的部分。** 它记录了被证伪的假设
（"按键名猜语义，迟早错配"）、静默 bug（"落盘扫描的去重从来没生效，统计数字有一半是重复计数"）、
以及明确写下"勿重开"的结论。**如果你只想读一份文件，读它。**

---

## 不想装 Python？用分发包

`tools/make-dist-zip.py` 会打出一个**内嵌 Python 的免安装包**：

```
edh.bat list
edh.bat info CPI
edh.bat fetch CPI --from 2020 --to 2024 --output cpi.csv
edh.bat pip install <package>        # 需要额外包时
```

解压即用，不需要装 Python、不需要配环境变量。目标是让不写代码的人也能拿到数据
（`dist/README.md` 是不出现代码/架构/门禁的那一版说明）。
重建方式见 `PACKAGING.md`。

---

## 可选：agent harness 集成

`.dsh/` 与 `tools/verify-preset.mjs` 是原作者把本项目接进其 AI Agent harness 的装配层。
**它们没有包含在公开仓库里** —— 里面写死了作者机器上的绝对路径，对其他人没有意义。
如果你在别处看到这两个路径的引用，忽略即可，不影响任何数据功能。

---

## 已知限制（请先读这段再决定要不要用）

- **只覆盖中国。** 五个源的取数逻辑都是按中国（`CHN` / `全国`）写死的，
  取别的国家需要改 `catalog_data.yaml`，部分 client 还要改代码。
- **18 个指标不是"全部经济学"。** 目录是人工编纂的，每条映射都真跑过取数才算数，
  所以扩张很慢 —— 这是刻意的，见 §2.13。
- **依赖上游 API 的稳定性。** 五个源都是公开接口，随时可能改参数或限流。
  项目对这些变化很敏感（也因此有了 `_probes/` 的取证记录），但没有 SLA。
- **交叉验证是启发式，不是裁判。** 它能告诉你"这两个源在某年差了 3 个百分点"，
  不能告诉你"哪个对"。判定是基于人工编纂的画像 + 实测差异，`unknown` 是允许的结论。
- **不是数据供应商的替代品。** 没有版本化数据集、没有历史修订追踪、没有技术支持。
- **`arbiter` 的判定阈值是人定的**（例如 `RELATIVE_METRIC_FLOOR = 0.5` 只适配百分点量纲），
  换量纲需要重新标定。

---

## 数据许可

**代码是 MIT，数据不是。** 程序运行时取回的数据版权属于各自发布机构
（NBS / World Bank CC BY 4.0 / IMF / FRED / BIS），再分发前请自行确认条款 —— 详见 `LICENSE`。

## 许可

MIT © 2026 ignshion
