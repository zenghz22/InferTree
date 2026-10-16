# P0 基线核实与实施计划

> 依据 `docs/phase-1-collaborative-artifacts-handoff.md` 的 P0 门槛产出。
> 状态：待用户与同行评审确认；**确认前不动生产代码**。
> 日期：2026-10-15

---

## 1. 基线核实结果（真实命令与证据）

| 项 | 结果 | 证据 |
|---|---|---|
| 测试 | `Ran 75 tests in 2.386s` → **OK**，进程退出码 **0** | `$env:PYTHONDONTWRITEBYTECODE=1; .\.venv\Scripts\python -m unittest discover -s tests` |
| 工作树 | 干净；仅 `docs/phase-1-collaborative-artifacts-handoff.md` 为**未跟踪** | `git status --short`（该文件是评审输入，保持未跟踪，不随开发提交） |
| 最近提交 | `ec92ad8` docs、`b8a8aed` UI 重心反转、`4b0cf79` 程序菜单… | `git log --oneline` |
| 运行方式 | `InferTree.exe` 启动器 → Uvicorn → `http://127.0.0.1:8000`；测试用 `.venv` | `README.md` |
| 无基线失败 | 是 | 上表 |

> PowerShell 把 unittest 的 stderr 圆点当作 `NativeCommandError` 打印，但 `$LASTEXITCODE=0` 且输出 `OK`，属已知展示噪音，非失败。

---

## 2. 关键代码事实（逐条核对评审风险表）

全部**确认为真**，并定位到具体位置：

| 评审风险 | 核实 | 位置 |
|---|---|---|
| `_validated_state` 只保留 `tree` 与 `active_paper`，新字段会被丢弃 | ✅ 确认 | `backend/workspace.py:174-190`（`validated = {"tree":…,"active_paper":…}`） |
| revision 自增但**不校验客户端 baseRevision** | ✅ 确认 | `backend/workspace.py:227-256`（`save_workspace` 直接 `previous.revision + 1`） |
| 论文清单只从树节点派生，文档引用会丢 | ✅ 确认 | `backend/workspace.py:53-88`（`_papers_manifest` 只遍历 `state["tree"]["nodes"]` + `active_paper`） |
| 前端全量 PUT 无 baseRevision，`active_paper` 被硬编码为 `null` | ✅ 确认 | `InferTree.html:1121-1147`（`currentWorkspaceState()` / `flushWorkspaceSave()`） |
| localStorage 键无 schema/workspace 标识，旧快照可能覆盖新状态 | ✅ 确认 | `InferTree.html:1184/1218/1227`（裸键 `tree`）；规格 6.2.9 要求改 |
| 前后端双重上下文预算漂移 | ✅ 确认 | 前端 `buildContext`(`InferTree.html:1295`) 组装 + 后端 `_bounded_conversation_messages` 再校验 |
| 原子写已有基础 | ✅ 可用 | `_atomic_write_json` + `_WRITE_LOCK`（单进程锁，规格接受） |

---

## 3. artifact / visual URL 稳定性（只读实测结论）

**结论：URL 稳定、确定性，跨会话/重启不变，无需「最小解析映射」补丁。**

- 图表 URL 形如 `/api/papers/{paper_id}/visuals/{visual_id}.png`。
- `paper_id` = PDF 的 **SHA-256 内容哈希**（`main.py` 上传去重；内容寻址）。
- `visual_id` = `p{页码}-{figure|table}-{编号}`，由 caption 扫描**确定性**派生，无时间戳/随机数（`backend/paper_visuals.py:61-63,335,345`）。
- 服务端 `GET /api/papers/{paper_id}/visuals/{filename}` → `visual_file()`（`main.py:205-207`）。
- PNG 存放在 `data/visuals/{paper_id}/`，**可从 PDF 重建**（`render_paper_visuals` 幂等重渲染），故可视为可重建缓存。

**落地含义**：`EvidenceRef` 只需存 `paperId + visualId`（或 `assetRef`），URL 按规则重建；不必持久化完整 URL，也无需改动图表识别算法（满足规格 4.4 / 1.3「不大改算法」）。

---

## 4. 已确认的三项决策

1. **文档上下文后端为主**：文档作用域请求（followup / propose_edit）由**后端**从 Workspace 解析块正文与证据并组装上下文，前端只传 `workspaceId + documentId + blockId + anchor + 模式`。避免前端 `buildContext` 与后端预算再分化。
2. **URL 稳定性**：见第 3 节，已确认稳定。
3. **P0 交付物 = 本计划**，评审确认后再动代码。

---

## 5. 数据契约落地（schema v2 → 实际 JSON）

沿用 `workspace.json` 单文件 envelope，`schema_version: 2`：

```jsonc
{
  "schema_version": 2,
  "workspace_id": "default",
  "revision": 42,                 // 单调递增，任何写入 +1
  "updated_at": "…",
  "state": {
    "tree": { "rootId": "...", "selectedId": "...", "nodes": { ... } },  // 原结构兼容
    "active_paper": null,
    "documents": { "<docId>": { ... } },   // v2 新增，空对象向后兼容
    "evidence":   { "<evId>":  { ... } },
    "proposals":  { "<propId>": { ... } },
    "dedup": { "<operationId>": "<timestamp>" }   // 幂等去重，容量上限 500，FIFO 淘汰
  }
}
```

各实体字段按规格第 5 节原样落地（`ResearchDocument / DocumentBlock / EvidenceRef / DocumentAnchor / DocumentPatchProposal`），不重复罗列。补充实现约定：

- **ID**：应用生成，沿用现有 `uid()` 或等价短随机串；身份不依赖标题/数组位置/坐标。
- **contentHash**：**服务端**按固定规则计算（`sha256(title + "\n" + markdown)`，figure 块对 `caption`），不信任模型/客户端传来的 hash。
- **baseRevision**：客户端写请求携带其最后一次读到的 `revision`；服务端在 `_WRITE_LOCK` 内读—比较—写，不匹配返回 **409 + 当前 revision**，不写任何状态。
- **每块/每提案的版本**：`DocumentBlock.revision + contentHash`、`DocumentPatchProposal.baseBlockRevision + baseContentHash` 用于细粒度冲突判定（见第 7 节）。
- **文档 schema version 与 LivingCanvas 的 World schema version 相互独立**；本阶段不存任何画布坐标。

---

## 6. API 设计选择

### 6.1 树保存（改造现有 PUT，不新增端点）
`PUT /api/workspaces/{id}` 增加 `baseRevision` 字段：
- 携带且匹配 → 服务端**只合并 `state.tree` + `active_paper`，保留 documents/evidence/proposals/dedup**，revision +1。
- 缺字段（旧客户端）→ **409**（满足 D04），不静默放行覆盖。
- 树保存「仅对 tree 最后写者胜」，**永不触碰文档子状态**，从根上消除「树自动保存覆盖提案」。

### 6.2 文档/提案写入（新增统一 operation 端点）
`POST /api/workspaces/{id}/operations`，body `{ operationId, baseRevision, op: "…", …}`，服务端单入口校验。op 词汇：

| op | 说明 | 冲突校验 |
|---|---|---|
| `document.create` | 新建文档 | baseRevision |
| `block.add` | 从回答/选区加入块（带 sourceRefs + evidenceIds） | baseRevision + operationId 幂等 |
| `block.update` | 改标题/正文/排序/移出/采纳提案 | baseRevision + 目标块 blockRevision/contentHash |
| `block.remove` | 移出文档（不删来源会话/论文/图表） | 同上 |
| `proposal.accept` / `proposal.reject` | 接受/拒绝提案 | baseRevision + baseBlockRevision/contentHash |

- **幂等**：`operationId` 记录到 `state.dedup`，重复到达不重复应用（U04/A01）。
- **撤销**：`block.update` 的逆操作是一个**新的有版本写入**，不回退全局 revision（U01）。
- 选统一端点而非多个专用端点的理由：所有写走同一条 `_WRITE_LOCK` 读—比较—写路径，冲突与幂等语义集中，符合规格 6.1「所有状态写入经过同一权威持久化入口」。

### 6.3 agent 请求增加文档作用域（`POST /api/agent/chat`）
新增可选字段，后端据此从 Workspace 解析上下文，不信任客户端伪造：

```jsonc
"document_scope": {
  "workspace_id": "default",
  "mode": "followup" | "propose_edit",   // 无此字段 = 现有行为，完全兼容
  "document_id": "…",
  "block_id": "…",
  "block_revision": 12, "block_content_hash": "…",
  "anchor": { "quote": "…", "prefix": "…", "suffix": "…" }
}
```

服务端解析块正文 + 证据元数据后，按规格 7 的优先级组装上下文，并做**最终预算控制**。

---

## 7. 模块修改计划（P1–P5）

| 阶段 | 改动文件 | 关键点 |
|---|---|---|
| **P1** | `backend/workspace.py`、`backend/main.py`、`tests/` | schema v2 校验/round-trip；baseRevision 409；operation 端点骨架；迁移；`_papers_manifest` 改树+文档**并集**；localStorage 键加 schema/workspace 标识（前端 P2 跟进） |
| **P2** | `InferTree.html`、`backend/main.py` | 文档列表/创建/加入/编辑预览/排序/移出/会话内撤销；保存状态机（未保存/保存中/已保存/失败/冲突）；草稿恢复；Context Tree 焦点隔离 |
| **P3** | `backend/agent.py`、`backend/main.py`、`InferTree.html` | `document_scope` 后端解析上下文；EvidenceRef 落地（paperId+visualId 重建 URL）；anchor + 局部追问；多论文引用 |
| **P4** | `backend/agent.py`、`InferTree.html` | `propose_document_patch` 工具（目标/基线由服务端绑定）；pending→accept/reject/conflicted 生命周期；预览 diff；幂等/取消/迟到响应保护 |
| **P5** | `README.md`、`docs/`、`tests/` | 回归+浏览器验收；更新旧文档状态；迁移/回退说明；交付报告 |

**不变更**：`backend/paper_visuals.py`、`paper_parser.py`、`paper_downloader.py`（算法不动）；**不改 LivingCanvas**。

---

## 8. 迁移与回退方案

1. **备份**：进入 P1 前，用只读脚本把 `data/workspaces/default/` 与 `data/papers/` 复制到 `data/backups/pre-v2-<ts>/`（含 schema、时间、代码版本；不含 API key）。开发/迁移测试一律用副本。
2. **v1→v2 迁移**：`load_workspace` 读到 v1 时，为 `state` 补空 `documents/evidence/proposals/dedup`，保存原 tree 全量字段，不删任何节点/支线；转换幂等，保存成功后才把 `schema_version` 写为 2。
3. **回退边界**：
   - 回退旧二进制**不能**直接写 v2 文件；需先恢复备份或用「仅树」导出。
   - 承诺边界：回退代码 + 恢复备份 = 回到 v1 可读状态；**不声称**旧代码能安全读 v2 文件。
   - 导出区分「仅对话树」与「完整 Workspace」（T02）。
4. **损坏文件**：保留原文件并报错，不以「新建空工作区」掩盖。

---

## 9. 与旧文档的同步（不删除）

进入 P1 后同步 `docs/architecture.md` 与 `docs/handover.md` 中冲突条款（按规格第 2 节修订），并把「步骤 5：2D 思维导图」标记为 **deferred / out-of-scope**。旧文档保留，仅在状态与链接上对齐，避免两份「唯一依据」互相矛盾。

---

## 10. 待评审确认的开放项

1. **operation 端点 vs 多个专用端点**：我推荐统一 operation 端点（见 6.2）。是否同意？
2. **树 PUT 的合并语义**：我推荐「树 PUT 只合并 tree，永不触碰文档子状态」（见 6.1）。是否同意？
3. **进入 P1 的授权**：本计划确认后，我是否按 P1 先实现「v2 数据层 + round-trip/冲突/迁移测试」并交你与评审过目，再动 UI（P2）？

> 未在本计划中覆盖的验收项（D/U/E/C/A/T/R 矩阵）将在对应阶段以「测试名 = 矩阵 ID」的方式落地，见 P1 起每阶段门槛。
