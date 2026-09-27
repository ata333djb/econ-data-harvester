# PACKAGING.md —— 打包与分发（内嵌 Python 方案）

> 这份文档只讲**怎么把项目打成用户可直接用的包**。
> 全局状态/坑/约定在 `PROJECT_STATE.md`（分发包相关见它的 §2.15 与 §3.19）。
> 本文件回答的是：**怎么重建 `dist/`、怎么重打 zip、当前卡在哪。**

---

## 1. 打包目标

**让用户电脑上不装 Python 也能用 `edh list / info / fetch`。**

- **方案**：内嵌官方 Python embeddable 发行包（**不是 PyInstaller**）
- **产物**：`dist/econ-data-harvester-v0.2.zip`（zip 内**不含 `dist/` 这一层**，解压即见 `edh.bat`）
- **选这个方案的理由**：分发的是 `.py` 源码 —— 改代码只需重跑一次打包脚本、用户重下 zip；
  PyInstaller 每改一次都要重打包，且杀软误报率高
- **代价**：包大 22 MB（`embedded-python/`），zip 压到 11.9 MB，可接受

### dist/ 结构

```
dist/
  edh.bat                  启动器（纯 ASCII；设 PYTHONPATH / ECON_HTTP_ALLOW_NON_VENV / chcp 65001）
  README.md                面向非技术用户（149 行，UTF-8 带 BOM）
  LICENSE                  MIT + 数据许可说明
  embedded-python/         Python 3.12.7 embeddable + Lib/site-packages/PyYAML 6.0.3（77 文件）
  python/econ_core/        运行时包（不含 _probes / __pycache__）
  tools/edh.py             用户 CLI
  examples/                report.html + econ_data.csv + data_dictionary.md
  .build/                  构建脚手架（不进 zip）
  .tmp/                    构建期临时目录（不进 zip）
```

`dist/` 已被 `.gitignore` 忽略（**只打 zip 分发，不进 git**）。

---

## 2. 任务状态

**A–G 七项已全部完成**（原计划里 C–G 是"待办"，实际在同一轮里做完了并跑通了干净环境测试）。
下面逐项记录**结论与证据**，不是待办清单。

| 任务 | 状态 | 关键结果 |
|---|---|---|
| A 依赖分析 | ✅ 完成 | 第三方包**只有 2 个**：PyYAML（必需）、pandas（可选） |
| B 内嵌 Python | ✅ 完成（**pip 除外**，见 §4） | Python 3.12.7 完整；PyYAML 6.0.3 已可用 |
| C `edh.bat` | ✅ 完成 | 纯 ASCII、非 ASCII 字节 = 0 |
| D 目录结构 | ✅ 完成 | 见 §1；`dist/` 已 gitignore |
| E 用户 README | ✅ 完成 | 149 行（上限 150），无代码/架构/门禁术语 |
| F 打 zip | ✅ 完成 | 93 条目、12,497,887 B、**全部正斜杠** |
| G 干净环境测试 | ✅ 完成 | 解压到 `D:\tmp\edh-test`，**7 步全过** |

### Task A 结论（这是打包的核心依据）

用 `tools/analyze-deps.py` 从 `tools/edh.py` 做 AST import 闭包（**含函数体内的 import**），
扫到 **12 个文件**：

```
python/econ_core/{__init__,bis_client,catalog,cross_validation,fetcher,
                  fred_client,http_client,imf_client,nbs_client,normalize,
                  worldbank_client}.py
tools/edh.py
```

| 包 | 位置 | 作用域 | 判定 |
|---|---|---|---|
| **PyYAML** | `catalog.py:132` | `load_catalog()`（**所有命令都要走**） | **必需** |
| **pandas** | `normalize.py:784` | `write_validated_parquet()`（不在 edh 路径上） | 可选，**未装** |

**最小依赖集 = { PyYAML }。** 三个常见疑问的明确答案：

- **pandas 不必要** —— `fetcher` 只用到 `normalize.parse_value()`；实测：内嵌 Python
  **没有 pandas**，`list` / `info` / `fetch` 全部正常
- **Jinja2 不必要** —— 用它的 `report.py` **不在闭包里**，`edh` 不 import 它；
  示例报告是预先生成好的静态 HTML。**Jinja2 未安装**
- **除 PyYAML 外没有别的第三方包** —— 闭包里其余 22 个模块全是 stdlib

> ⚠️ **分析器本身的坑（会少报依赖）**：`fetcher.py` 里那行
> `from . import (bis_client, catalog, ...)` 是"相对导入 + 多名字"，
> 第一版只解析了 `econ_core` 本身、没把每个名字展开成子模块，
> 于是**八个 client 一个都没进闭包**。改 `analyze-deps.py` 时要盯住这一点。

### Task B 结论

| 项 | 结果 |
|---|---|
| 下载 | `python-3.12.7-embed-amd64.zip`，11,062,583 B<br>sha256 `0d57bb6cb078b74d23dbfe91f77d6780d45bed328911609f1f7ee2ba1606bf44` |
| 解压 | `dist/embedded-python/`，77 个文件，`python.exe` / `python312.zip` 齐 |
| `_pth` 改写 | 已改为 4 行：`python312.zip` / `.` / `Lib\site-packages` / `import site` |
| **PyYAML** | **6.0.3 已可用**（实测 `import yaml` 通过，`safe_load` 正常） |
| Jinja2 | **未装**（不需要，见 Task A） |
| pandas | **未装**（不需要，见 Task A） |
| pip | **未装**（见 §4） |

**关于下载方式（重要）**：本机 `Invoke-WebRequest` / curl 等走 .NET-WinHTTP 的下载**一律 TLS 失败**
（`基础连接已经关闭: 接收时发生错误`）。**必须**用 `tools/download.py`（走 venv 的 OpenSSL 栈）：

```powershell
.\.venv\Scripts\python.exe tools\download.py <url> <输出路径>
```

---

## 3. 怎么重建（重打一次 zip）

改完任何 `.py` 之后**都要重打**，否则用户拿到的还是旧代码：

```powershell
cd D:\universe\econ-data-harvester
$env:PYTHONIOENCODING="utf-8"

# 1) 同步运行时代码进 dist/（改了 econ_core 或 edh.py 就要做）
Copy-Item python\econ_core -Destination dist\python\ -Recurse -Force
Remove-Item dist\python\econ_core\__pycache__ -Recurse -Force -ErrorAction SilentlyContinue
Copy-Item tools\edh.py -Destination dist\tools\ -Force

# 2) 重打 zip（白名单 + 正斜杠 + 自检）
.\.venv\Scripts\python.exe tools\make-dist-zip.py
```

**打包范围是白名单**（`make-dist-zip.py` 里的 `DIST_ITEMS`），不是"dist/ 下所有东西"——
`.build/`、`.tmp/`、`get-pip.py`、下载缓存都是脚手架，不该进用户包。脚本里有自检断言这件事。

### 从零重建 embedded-python（换机器 / 换 Python 版本时）

```powershell
# 1) 下载解压
.\.venv\Scripts\python.exe tools\download.py `
    https://www.python.org/ftp/python/3.12.7/python-3.12.7-embed-amd64.zip `
    dist\python-3.12.7-embed-amd64.zip
Expand-Archive dist\python-3.12.7-embed-amd64.zip -DestinationPath dist\embedded-python -Force

# 2) 改 _pth（原文最后一行是 "#import site"，要改成 import site 并加 Lib\site-packages）
#    见 §2 Task B 的四行结果

# 3) 装 PyYAML —— 当前可行做法是**从 venv 复制**（pip 在本沙箱装不动，见 §4）
New-Item -ItemType Directory -Force -Path dist\embedded-python\Lib\site-packages | Out-Null
Copy-Item ".venv\Lib\site-packages\yaml" -Destination "dist\embedded-python\Lib\site-packages\" -Recurse -Force -Exclude "__pycache__"
Copy-Item ".venv\Lib\site-packages\pyyaml-6.0.3.dist-info" -Destination "dist\embedded-python\Lib\site-packages\" -Recurse -Force
```

**为什么能直接复制**：内嵌解释器与 venv 都是 **CPython 3.12 win_amd64**，ABI 一致，
`yaml/_yaml.cp312-win_amd64.pyd` 这个 C 扩展可以直接用。

---

## 4. 已知缺口 / 下一步从哪继续

### ① pip 没装进内嵌 Python（Task B 唯一未完成项）

- **现状**：`import pip` 在内嵌解释器里是 `ModuleNotFoundError`。功能不受影响（PyYAML 已可用），
  但用户无法自行 `pip install` 别的包
- **已试过、都失败的路径**：
  - `get-pip.py`（经 `dist/.build/patched-run.py` 修补 tempfile）—— 跑 20 分钟无输出后手动停掉
  - venv pip 的 `--target` 安装 —— 7 分钟超时
  - `pip download pyyaml --timeout 15 --retries 1 -v` —— **180 秒无输出**
- **已排除的原因**：
  - 不是网络 —— `urllib` 直连 PyPI `/simple/pyyaml/` 是 **HTTP 200 / 3.5 秒**
  - 不是 tempfile/沙箱 ACL —— 那部分已由 `pip_sandbox_install.py` 的 patch 处理
  - 不是 pip 装不上 —— `import pip` 与 `pip --version` 都正常（pip **26.2.1**）
- **结论**：卡在 pip 的 **HTTP 会话层**（不是启动、不是解析）。下一步该用
  `pip -vvv` 把日志落到文件、看它停在哪个 socket/重定向；或用
  `--index-url` 指向本地 wheel、或预先 `pip download` 好 wheel 再用
  `--no-index --find-links` 离线装。**优先级低** —— 核心功能不依赖它

### ② examples/ 与 report.html 的自述不完全对齐

`report.html` 的"八、数据下载"一节提到 `econ_data.db` 与 `missing_report.json`，
但 `examples/` 里只放了 3 个文件（按任务规格）。**不是断链**（那节是纯文本提及），
但用户可能去找。补进去约 +300 KB。**待定：要不要补**

### ③ 打包工具没进门禁（刻意）

`analyze-deps.py` / `download.py` / `make-dist-zip.py` 是**手工跑的构建脚本**，
没加进 `tools/run-all-checks.py`（门禁保持 28 项，一条没动）。
理由：门禁是"改代码后的回归网"，而打包是**发版动作**，频率不同。
若想让打包也受门禁保护，加一条"`make-dist-zip.py` 能跑通且自检通过"是合理的。

### ④ LICENSE 主体署名待确认

LICENSE 已按 **MIT** 落地，版权行写的是 `Copyright (c) 2026 EconDataHarvester contributors`
——**没有**写具体自然人/组织名。要写实名请直接改 LICENSE 第一段（`dist/LICENSE` 也要同步）。

---

## 5. 决策记录

| 决策 | 结论 | 理由 |
|---|---|---|
| 打包方式 | **内嵌 Python embeddable**，不用 PyInstaller | 改代码只改 `.py`，用户不用重下；无杀软误报 |
| **LICENSE** | **MIT（已定）** | 用户 2026-09-27 明确选定。仅覆盖**代码**；数据版权归各发布机构，已在 LICENSE 内单列一节说明 |
| pandas | **不装** | 不在 edh 闭包上（Task A 已证 + 实测） |
| Jinja2 | **不装** | 不在 edh 闭包上（`report.py` 不参与） |
| PyYAML | **装**（从 venv 复制） | `catalog.load_catalog()` 必需 |
| `ECON_HTTP_ALLOW_NON_VENV=1` | **在 edh.bat 里设** | 用户包无 `.venv`，该守卫会让 `fetch` 100% 失败；内嵌解释器自带 OpenSSL，实测 TLS 通（§PROJECT_STATE 3.19） |
| `chcp 65001` | **在 edh.bat 里设 + 结束恢复** | 控制台默认 936(GBK) 而进程发 UTF-8，不切中文全花（实测 936→65001→936） |
| zip 里含 `.py` 源码 | **是** | 分发形态决定"改代码 = 重打 zip"，不必重编 |

---

## 6. 验证清单（每次重打 zip 后跑一遍）

```powershell
# 1) 脱壳测试：解压到工作区**之外**（否则测不出路径假设）
$T = "D:\tmp\edh-test"
Remove-Item $T -Recurse -Force -ErrorAction SilentlyContinue
Expand-Archive -Path dist\econ-data-harvester-v0.2.zip -DestinationPath $T -Force
Set-Location $T

# 2) 从 936 码页开始（重要：这样才能验出中文是否真的不花）
cmd /c "chcp 936 >nul & .\edh.bat list"            # 期望 18 个指标，exit 0
cmd /c ".\edh.bat info CPI"                        # 期望完整详情，exit 0
cmd /c ".\edh.bat fetch CPI --from 2020 --to 2024 --output test.csv"   # 期望 exit 0

# 3) CSV 编码（记事本/Excel 靠 BOM 认中文）
#    期望：BOM = EF BB BF，131 行（表头 1 + 数据 130），中文完整

# 4) examples/report.html：外部 src= 资源应为 0（图表/样式全内联）
```

**验收要点**：`.bat` 非 ASCII 字节 = 0；zip 条目全部正斜杠；码页前后都是 936。
