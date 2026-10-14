# InferTree

InferTree 0.11.0 是一个本地运行的树状 Agent GUI，重点面向论文阅读、审稿、结构化追问、自动论文工作流和可重复 Agent 评测。浏览器只连接本机 FastAPI 后端；模型、Tavily 和下载工具都由后端调用，API Key 不会发送到前端。

## 快速开始

1. 复制 `config.example.toml` 为 `config.local.toml`。
2. 填写模型网关的 `base_url`、`api_key`，按需填写 Tavily Key。
3. 双击项目根目录的 `InferTree.exe`。
4. 浏览器打开 `http://127.0.0.1:8000`，在输入框上方选择本次请求的模型、Skill 和思考设置后提问。

不要直接双击 `InferTree.html`。论文上传、Agent、联网搜索和 Skill 都依赖本地后端。接口文档位于 `http://127.0.0.1:8000/docs`。

## 已实现功能

### 树状对话 GUI

- 使用浅色极简主题；背景、面板、文字、边框和强调色可通过 `config.local.toml` 自定义。
- 每次追问创建一个子节点，可以从任意历史节点继续分支。
- 模型、Skill、审阅模式、思考控制、PDF 上传和论文工作流均位于输入框上方；这些设置只作用于新建节点，不再占用全局标题栏。
- 在右侧对话中高亮一段文字即可发起局部追问；引用会自动加入模型 Prompt，并从原文所属节点创建带专属颜色的分支。
- 当前节点只继承根到父节点的祖先路径，兄弟分支不会进入请求。达到字符预算后，程序优先保留最近轮次、压缩较早长回答，并加入只含旧问题的主题索引；输入框上方和 Agent 活动会显示保留轮数及是否压缩。
- 当前路径达到两轮对话后，右侧出现轮次跳转轨道：悬停预览用户问题、点击平滑跳到该轮开头，并跟随阅读位置高亮。
- 支持节点选择、折叠、平移、缩放、适应视图和侧栏宽度调整。
- 支持重新回答和删除子树；危险操作需要二次点击确认。
- 支持最多 50 步撤销/重做。
- 支持导出当前路径为 Markdown、导出整棵树为 JSON，以及导入经过结构校验的 JSON。
- 对话树和各节点的论文绑定自动保存到后端默认 Workspace；浏览器 `localStorage` 保留为快速启动和后端不可用时的回退副本。
- 从旧版本首次刷新时，若默认 Workspace 尚不存在，会自动把当前浏览器中的对话树迁移进去；已有 Workspace 会作为后续启动的主副本。
- 浏览器存储空间不足时会压缩持久化的思考/活动记录，但不会阻断当前 Agent 请求；刷新后残留的“生成中”节点会标记为已中断。

树结构的主要价值是组织分支和缩小单次请求上下文；它不会自动让多个分支共享模型内部状态，也不等同于多 Agent 调度器。

### 后端 Agent

- 浏览器只调用本地 `/api/agent/chat`，不会直接访问模型厂商。
- 后端注入 `config.local.toml` 中的 Agent 指令，并控制模型白名单、上游模型名和工具定义。
- 每轮请求自动扫描 `skills/*/SKILL.md`，让模型看到所有已安装 Skill 的名称和描述。默认的“Skill：自动”通过 `activate_skill` function tool 让模型按当前请求选择一个 Skill；激活成功后，后端才渐进加载完整指令、参考文件和对应工具。也可以显式选择或关闭 Skill。
- 使用 OpenAI-compatible Chat Completions，支持流式回答、工具调用和最多 8 轮工具循环。
- 自动生成节点短标题；标题请求使用同一选定模型，并尽量降低或关闭思考。
- 对连接失败、超时、上游 HTTP 错误、空响应、流中断和工具轮次超限给出持久化错误，而不是一直卡住。
- 支持停止正在生成的回答。

### 可视化执行过程

后端通过 `infertree.activity.v1` SSE 事件报告操作，前端会实时显示并持久化：

- Agent 开始、完成、失败或取消；
- 每一轮模型调用；
- Tavily 联网搜索；
- 论文下载；
- 论文图表扫描和截图；
- 自动 Skill 的选择与激活理由；
- 工具耗时、结果数量和安全摘要。
- 本次祖先路径上下文的总轮数、保留轮数、字符预算和压缩状态。

活动详情不会包含 API Key、完整模型请求、隐藏推理或整篇论文内容。

### 多模型与思考控制

网页输入框上方可以选择以下模型，浏览器只提交受限的 `model_id` 和统一的 `reasoning_control`；后端再翻译为厂商参数。

| 模型 | 前端控制 | 后端上游参数 |
| --- | --- | --- |
| Qwen3.8-Max | 开关；`low` / `medium` / `xhigh` | `enable_thinking`、`reasoning_effort` |
| Qwen3.8-Flash | 开关；`low` / `medium` / `xhigh` | `enable_thinking`、`reasoning_effort` |
| Kimi K3 | 始终思考；`low` / `high` / `max` | `reasoning_effort` |
| GLM-5.3 | 思考开关 | `thinking.type` |
| DeepSeek-V4-Pro-0813 | 开关；`low` / `high` / `max` | `thinking.type`、`reasoning_effort` |
| DeepSeek-V4-Flash | 开关；`low` / `high` / `max` | `thinking.type`、`reasoning_effort` |

当前内置上游名称适配现有多模型网关。厂商直连接口常使用不同大小写或别名，可以在 `[models.<id>]` 中覆盖 `name`、`base_url` 和独立 `api_key`。`GLM-5.3` 已由当前网关提供，但智谱官网型号页尚未在开发时得到确认，因此 InferTree 不为它虚构思考强度档位。

### 联网搜索

- Tavily 作为后端 function tool 使用，模型判断何时需要搜索。
- 搜索开始、完成或失败会显示在活动时间线中。
- 搜索结果作为工具结果返回模型，不会被前端伪装成模型知识。
- 未配置或关闭 Tavily 时，Agent 仍可对话，但无法按标题搜索论文或核查最新外部信息。

### PDF 上传与解析

- 支持浏览器上传 PDF，单文件最大 50 MB。
- 文件按 SHA-256 内容哈希保存，相同 PDF 不会重复存储。
- 使用 PyMuPDF 提取逐页文本，并保留稳定页码供引用和审稿。
- 右侧提供可折叠的同源 PDF 阅读器；回答中的“第 N 页”“page N”引用会变成可点击的页码跳转。
- “阅读 PDF”按钮只读取当前分支最近绑定的论文，并附带标题缩写；无论文的无关分支会禁用该按钮。
- 当前全文审阅最多接受 250,000 个提取字符；超过上限会明确拒绝，而不是静默截断。
- 上传成功的论文绑定到上传时选中的节点，并由该节点的后续分支继承；无关分支不会获得该论文。

### Paper Downloader

没有本地 PDF 时，可以直接选择 Paper Reviewer 并提供 arXiv ID、开放 PDF URL、DOI 或明确标题；Reviewer 会自动组合 `paper-downloader`。也可以显式选择 Paper Downloader。

下载器会：

- 对明确的 arXiv ID 或开放仓库 PDF URL 直接下载；
- 对 DOI 或论文标题先通过 Tavily 搜索和核对；
- 只允许 HTTPS 和公开学术仓库白名单；
- 重新校验每次重定向，拒绝离开白名单；
- 验证 PDF 文件头并执行 50 MB 上限；
- 按 SHA-256 保存和去重；
- 对 arXiv 使用官方 Metadata API 获取可靠标题；
- 下载成功后自动通知前端，并只绑定到发起下载的回答节点。

当前白名单包括 arXiv、OpenReview、ACL Anthology、PMLR、CVF、NeurIPS 和 JMLR。下载器不会绕过登录、验证码、robots 限制、付费墙或其他访问控制；没有可靠开放版本时会要求用户上传 PDF。

### Paper Reviewer

`paper-reviewer` 支持以下程序控制的模式，而不只是前端标签：

| 模式 | 行为 |
| --- | --- |
| 自动 | 根据请求选择适合的审阅方式 |
| 论文概览 | 总结问题、方法、证据和主张，不给录用建议 |
| 完整审稿 | 加载完整 rubric，输出优点、问题、复现性、置信度和暂定建议 |
| 专项核查 | 只检查用户指定的主张或维度 |
| 故事讲授 | 以教授讲课方式循序渐进解释背景、相关工作、方法和实验 |

Reviewer 会要求关键判断对应论文页码，区分论文证据、外部证据和 Reviewer 推断，并把论文文本视为不可信数据而非系统指令。

### Paper Visual Extractor

- 可显式选择 `paper-visual-extractor`，也可在 Reviewer 中请求主机制图或实验结果图表。
- 扫描 Figure、Fig. 和 Table caption；常规文本块检测失败时，会使用带真实页坐标的保守行级回退。
- 根据 caption 关键词、图表类型、编号和页码，将候选分为主机制图、主结果、支持实验和其他，并提供可解释的分数、置信度、命中证据及分类内排名。
- 模型综合排序证据选择相关图表，通常提取 1 张完整机制图与 1–2 张主结果图表，最多渲染 4 张 PNG 截图并插入对话。
- 截图会显示页码、标签和 caption，供回答引用。
- 当前分类是 caption/版面启发式，不冒充像素级视觉理解；证据不足时会明确报告限制，不按 Figure 1/Table 1 盲选。

### 一键论文工作流

在输入框填写论文标题、DOI、arXiv ID 或 URL，选择“终稿：故事讲授”或“终稿：详细报告”，然后点击“论文工作流”。如果当前分支已绑定 PDF，也可以留空输入框直接运行。

工作流按顺序创建一条可检查、可分支的节点链：

1. 搜索、核对并下载开放 PDF（当前分支已有 PDF 时跳过）；
2. 读取正文并分析背景、相关工作、方法、实验和结论；
3. 生成初步同行评审；
4. 提取并分析主机制图与主结果图表；
5. 综合为故事式讲授或详细报告。

开始前会显示预计节点数与模型请求数并要求确认。每一步使用自动 Skill 路由，继承上一步绑定的 PDF 和祖先路径上下文；任一步报错、超时、被停止或未能获得本地 PDF 时，流程立即停止，不会继续生成失去依据的后续节点。流程节点在树上使用虚线和菱形标记。

### Agent 评测体系

网页顶部的“评测”按钮会打开本地评测控制台。当前支持：

- 版本化 JSON 测试套件，测试定义与运行记录分离；
- 不调用模型的 `fixture` 套件，用于验证 grader、持久化和 UI；
- 调用真实 Agent 的 `agent` 套件，可同时选择多个模型并重复 1–3 次；
- `no_error`、非空输出、包含/排除、前缀、正则、工具调用、产物数量和延迟 grader；
- 按权重合并分项分数，并按用例阈值判断是否通过；
- 后台串行运行、单样本超时、运行取消、逐样本原子保存和服务重启后的中断识别；
- 总体及分模型通过率、平均分、平均延迟、失败 grader 和可见回答查看；
- 真实模型运行前显示预计样本数并二次确认，避免意外消耗额度。

内置 `grader-smoke` 是零成本本地验证；`agent-core` 包含后端身份、基础正确性、Skill 发现和引文指令隔离四类核心回归。自定义套件放入 `evals/suites/{suite-id}.json`，修改测试用例不需要改后端代码。

评测运行仅保存模型可见回答、结构化 Agent 活动、产物元数据、延迟与 grader 结果，不保存 API Key、完整上游请求或隐藏推理。运行记录位于 `data/evals/runs/`，不会写入 Workspace 对话树。

当前分类主要依据 caption 和版面位置，不等同于视觉模型对像素内容的完整理解；模糊分类应视为暂定结果。

### Markdown、公式与图片

模型回答支持：

- GitHub Flavored Markdown；
- 标题、列表、表格、引用、代码块和任务列表；
- DOMPurify 清理后的安全 HTML；
- MathJax LaTeX：`$...$`、`$$...$$`、`\(...\)` 和 `\[...\]`；
- 论文截图和普通 Markdown 图片。

Marked、DOMPurify 和 MathJax 当前从 jsDelivr 加载。CDN 不可用时，回答仍会以可读 Markdown/纯 TeX 回退，但增强渲染可能不可用。

## 配置

最小配置示例：

```toml
[agent]
name = "InferTree Agent"
instructions = "You are a careful, helpful assistant."

[model]
base_url = "https://your-multi-model-gateway.example/v1"
api_key = ""
default = "deepseek-v4-pro-0813"
connect_timeout_seconds = 10
read_timeout_seconds = 45
use_system_proxy = false

[ui.theme]
background = "#FAF2E0"
panel = "#FFFCF5"
text = "#17324A"
border = "#D8CDB8"
accent = "#0E61AC"

[context]
max_chars = 60000
recent_turns = 4
max_message_chars = 12000
outline_chars = 4000

[tools.tavily]
enabled = false
api_key = ""
max_results = 5
```

完整模板见 `config.example.toml`。注意：

- TOML 布尔值必须写成小写 `true` 或 `false`。
- `base_url` 应是 OpenAI-compatible 基础路径，通常以 `/v1` 结尾。
- 主题颜色必须是六位十六进制值；非法字段会单独回退到内置颜色。修改后刷新网页即可，无需重启 Agent。
- `[context]` 控制树路径上下文：总字符预算、优先完整保留的最近轮数、单条消息上限和旧问题索引上限。前端先按同一策略组装，后端再次执行权威校验。
- 网页 System Prompt 属于对话树根节点；`[agent].instructions` 是后端始终注入的全局 Agent 指令。
- `config.local.toml` 已被 Git 忽略，不应提交或发送给他人。

## 数据、隐私与安全边界

- 服务仅监听 `127.0.0.1:8000`。
- 模型和 Tavily API Key 只保存在 `config.local.toml`。
- `/api/models` 和 `/api/ui-config` 只返回浏览器所需的安全元数据，不返回 Key、Base URL 或真实上游配置。
- 浏览器保留对话回退副本和界面偏好，不保存模型或 Tavily Key。
- 上传或下载的论文统一按 SHA-256 存放在 `data/papers/`，不同 Workspace 可以安全引用同一份 PDF，无需重复保存。
- 默认 Workspace 位于 `data/workspaces/default/`：`workspace.json` 保存对话树，`papers.json` 汇总节点引用的论文，`artifacts/` 预留给报告等派生产物。旧版 `data/workspaces/default.json` 会在首次读取时自动迁移。
- 生成的论文截图位于 `data/visuals/`；它们可以由 Workspace 节点引用，但不会混入原始 PDF 论文库。
- 评测定义位于 `evals/suites/`，本机运行记录位于已忽略的 `data/evals/runs/`。
- `InferTree.exe` 不包含 API Key，运行时读取本地配置。
- PDF 文本、caption、网页搜索结果和下载内容均被视为不可信输入。

## 默认超时与限制

| 项目 | 默认值 |
| --- | ---: |
| 模型连接超时 | 10 秒 |
| 模型流读取超时 | 45 秒 |
| 浏览器流无活动超时 | 70 秒 |
| Tavily 请求读取超时 | 20 秒 |
| Agent 工具循环 | 最多 8 轮 |
| PDF 上传/下载 | 最大 50 MB |
| 全文审阅上下文 | 最大 250,000 提取字符 |
| 树路径对话上下文 | 默认 60,000 字符；最近 4 轮优先完整保留 |
| 单个 Workspace | 最大 12 MB / 5000 节点 |
| 单次图表渲染 | 最多 4 张 |
| 撤销历史 | 最多 50 步 |
| 单个评测套件 | 最多 50 个用例 |
| 单次评测运行 | 最多 100 个样本 / 每用例最多重复 3 次 / 同时仅 1 个运行 |
| Agent 评测单样本超时 | 套件配置，允许 10–300 秒；内置核心套件为 90 秒 |

## 本地 API

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/api/health` | 服务、配置和活动协议状态 |
| `GET` | `/api/models` | 浏览器安全的模型目录 |
| `GET` | `/api/ui-config` | 浏览器安全的主题与上下文预算配置 |
| `GET` | `/api/skills` | 已安装 Skill 的安全发现目录 |
| `GET` | `/api/evals/suites` | 列出评测套件及预计用例数 |
| `GET` | `/api/evals/suites/{suite_id}` | 获取套件安全元数据 |
| `POST` | `/api/evals/runs` | 启动本地或真实 Agent 评测 |
| `GET` | `/api/evals/runs` | 获取最近的评测运行 |
| `GET` | `/api/evals/runs/{run_id}` | 获取进度、分数和样本结果 |
| `POST` | `/api/evals/runs/{run_id}/cancel` | 取消正在运行的评测 |
| `POST` | `/api/agent/chat` | Agent 对话和 SSE 流 |
| `POST` | `/api/papers` | 上传并解析 PDF |
| `GET` | `/api/papers/{paper_id}/metadata` | 读取节点论文标题和页数 |
| `GET` | `/api/papers/{paper_id}/file` | 在同源阅读器中读取原始 PDF |
| `GET` | `/api/papers/{paper_id}/visuals/{filename}` | 获取论文截图 |
| `GET` / `PUT` | `/api/workspaces/{workspace_id}` | 加载或原子保存 Workspace |
| `GET` | `/api/workspaces/{workspace_id}/papers` | 获取 Workspace 的节点论文索引 |
| `GET` | `/api/skills/paper-reviewer` | Reviewer Skill 元数据 |
| `GET` | `/api/skills/paper-visual-extractor` | Visual Skill 元数据 |
| `GET` | `/api/skills/paper-downloader` | Downloader Skill 元数据 |

## 项目结构

```text
InferTree/
├─ InferTree.exe              Windows 无终端启动器
├─ InferTree.html             单页树状 GUI
├─ launcher.py                启动器源代码
├─ config.example.toml        配置模板
├─ config.local.toml          本地密钥配置，不提交
├─ backend/                   FastAPI、Agent、PDF 和工具实现
├─ skills/
│  ├─ paper-reviewer/
│  ├─ paper-visual-extractor/
│  └─ paper-downloader/
├─ tests/                     单元与接口测试
├─ evals/suites/              版本化 Agent 评测定义
└─ data/
   ├─ papers/                按内容哈希去重的 PDF 论文库
   ├─ visuals/               从 PDF 渲染的图表截图
   ├─ workspaces/default/
   │  ├─ workspace.json      对话树、节点状态和版本号
   │  ├─ papers.json         节点引用的论文索引
   │  └─ artifacts/          Workspace 派生产物预留目录
   ├─ evals/runs/            本地评测记录（运行后生成，不提交）
   └─ launcher.log           启动诊断（首次启动后生成）
```

## 开发运行

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
Copy-Item .\config.example.toml .\config.local.toml
.\.venv\Scripts\python -m uvicorn backend.main:app --reload
```

然后访问 `http://127.0.0.1:8000`。

## 测试

```powershell
$env:PYTHONDONTWRITEBYTECODE = "1"
.\.venv\Scripts\python -m unittest discover -s tests -v
```

当前测试覆盖模型白名单与思考参数翻译、结构化 SSE 错误、Skill 加载、树路径上下文预算、评测套件与 grader、异步运行持久化、PDF 上传与读取、Workspace 校验、目录迁移、论文索引和原子持久化、论文下载安全边界、图表定位与分类、启动器健康检查和接口隐私。

## 常见故障

- 节点一直显示“生成中”且“停止”不可用：先按 `Ctrl+F5` 强制刷新。当前版本会在请求开始前启用“停止”，并把启动阶段异常直接写入节点。
- 状态栏显示后端不可用：确认通过 `InferTree.exe` 启动，并访问 `http://127.0.0.1:8000/api/health`。
- 回答显示 `MODEL_CONNECTION_ERROR` 或 HTTP 502：本地 Agent 已收到请求，但无法连接 `config.local.toml` 中的模型 `base_url`。检查服务地址、网络/VPN、防火墙和代理配置；可用 `Test-NetConnection <模型域名> -Port 443` 验证 TCP 连接。
- 使用环境变量代理时，将 `use_system_proxy` 设为 `true` 并重新启动后台服务；若没有配置 `HTTP_PROXY`/`HTTPS_PROXY`，仅切换该布尔值不会创建代理。

## Windows 启动器

项目根目录已有 `InferTree.exe`。双击后，它会：

1. 检查本地健康接口；
2. 必要时使用 `.venv\Scripts\pythonw.exe` 后台启动 Uvicorn；
3. 最多等待 10 秒；
4. 打开默认浏览器；
5. 将启动诊断写入 `data/launcher.log`。

重建启动器：

```powershell
.\.venv\Scripts\python -m pip install -r requirements-dev.txt
.\.venv\Scripts\pyinstaller --clean --onefile --windowed --name InferTree --icon icon.ico launcher.py
Copy-Item -Force .\dist\InferTree.exe .\InferTree.exe
```

不要使用 `Start-Process -Wait` 测试 `InferTree.exe`：启动器创建长期运行的后台服务，PowerShell 可能一直等待整个进程树。应直接启动，然后对 `/api/health` 做最多 10 秒的有界轮询。

`build/`、`dist/`、`*.spec`、Python 缓存、日志、PID、论文库和生成截图均已在 `.gitignore` 中按用途忽略；正常使用应保留根目录的 `InferTree.exe`、`.venv` 和本地配置。
