# EconDataHarvester

**把宏观数据从官方源取回来，并且证明它没被取错。**

[English](README.en.md) · MIT · Python 3.12+

---

## 这是什么

一个命令行工具 + 一套数据可信度基础设施。给它一个指标名（`CPI`），它会去
把数据取回来，并且合并成一张干净的宽表，
然后回答一个多数取数脚本不会回答的问题：

> 这几个源说的是一回事吗？如果不是，差在哪里、为什么、该信谁？

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
| 单文件 HTML 报告 | `data/output/report.html` | 全部图表/样式内联，可离线打开、可直接发给别人 |
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


## 许可

MIT © 2026 ignshion
