# InferTree

一个单文件、零依赖的**树状大模型推理网页**。把对话从线性历史改写成一棵可分支的树：每个节点是一次模型回答，节点之间的边是用户提问；从任意历史节点都能拉出新的分支去追问。

## 特性

- **树状对话**：横向 Xmind 风格布局，根在左、分支向右。每个非叶子节点可一键折叠/展开（`−` / `+N`）。
- **流式问答**：标准 OpenAI 兼容 SSE 协议，逐字渲染；可随时停止。
- **Markdown 渲染**：模型回答按 Markdown 显示（标题/列表/代码块/表格），通过 `marked` 解析。
- **DeepSeek 深度思考**：支持 `reasoning_content`，在回答前展示可折叠的"💭 思考过程"。
- **联网搜索**：通过 function calling + Tavily 实现；模型自动判断是否需要搜索，结果喂回继续作答，气泡显示已搜索的关键词。
- **自动标题**：每轮回答结束后用一次小请求让模型用 4–12 个字概括，写到节点框上。
- **并发安全**：生成期间可自由浏览其他节点，正在生成的节点在树上带旋转动画；不允许同时发起第二个请求。
- **节点操作**：从任意节点分支追问、删除子树（二次确认）、重新作答（弃后续子树，二次确认）。
- **可调布局**：树画布与会话栏之间可拖拽分隔条；树画布支持滚轮缩放、拖拽平移、适应视图。
- **导入导出**：整棵树 JSON 互导；当前路径导出 Markdown。
- **本地持久化**：所有数据（树、设置、API Key）都存在浏览器 localStorage。

## 使用

1. 双击打开 `index.html`（无需安装、无需构建）。
2. 点右上角 **设置**，填：
   - **Base URL**：到 `/v1` 为止，如 `https://api.deepseek.com/v1`、`https://api.openai.com/v1`。
   - **模型名**：如 `deepseek-v4-pro`、`gpt-4o-mini`。
   - **API Key**：你的密钥。
   - 可选：System Prompt、深度思考（含 effort）、联网搜索（需 [Tavily](https://tavily.com) Key）。
3. 在右侧输入框提问，回车发送。新节点会挂在当前选中的节点下；点击树上任意历史节点即可从那里继续分支。

## 数据模型

```
node = { id, parentId, query, content, reasoning, searches, title, children, collapsed }
```

- `query`：进入该节点的那条**边**（用户请求）。
- `content`：该轮模型**回答**；根节点存 System Prompt。
- 历史不整段保存，由 `buildMessages()` 沿路径回溯拼出 `system → user → assistant → ...`。

## 项目结构

```
InferTree/
├── index.html   # 全部代码（HTML/CSS/JS 单文件）
└── README.md
```

依赖：仅在运行时通过 CDN 加载 [`marked`](https://github.com/markedjs/marked) 解析 Markdown；离线时自动回退为纯文本。

## 隐私

- API Key、对话树、设置都只存在你本机浏览器的 localStorage。
- 请求只发往你填写的 Base URL（以及开启搜索时的 Tavily 接口）。
- 不上传任何第三方分析/统计。
