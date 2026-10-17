# P4 交付报告：agent 修改提案

> 依据 `docs/phase-1-collaborative-artifacts-handoff.md` §8-P4。
> 状态：代码与测试完成，等待用户实测。

---

## 1. 范围

### 1.1 已完成
- **`propose_document_patch` 工具**：只作用于文档作用域的**目标块**，接入现有工具循环；**只创建提案，绝不写正文**。
- **提案生命周期**：预览、接受、拒绝、冲突、关闭（取消）、幂等、撤销。
- **迟到响应防护**（三道）：上下文携带块版本、提案创建校验基线、接受时再次校验基线。
- **幂等**：服务端 operationId 由请求内容确定性生成，重复的相同工具调用被去重，不会产生两个提案。
- **最小操作摘要**：工具结果 `message` 为「为块 X 提出修改」，活动面板可见；不输出内部推理。
- **图表块入口**（P3 遗留）：文档面板 `＋ 图表块`，从当前回答已渲染的图表建块，显式绑定 `paperId + visualId` 并同时落证据。

### 1.2 未完成
- 仅支持**整块替换**，不支持插入/删除块或多块联合提案。
- 预览是左右对照（原文 vs 渲染后的提案），不是逐行 diff。
- 「取消」= 关闭预览（不做决定）+ 生成中可用的停止按钮；没有"撤销已发出的提案请求"的专门入口。

---

## 2. 门槛对照

门槛：**模型输出和迟到响应均不能绕过用户确认修改正文；失败不丢人工草稿。**

| 要求 | 实现 | 证据 |
|---|---|---|
| 模型输出不能直接改正文 | 工具只调用 `proposal.create`；正文只能由 `proposal.accept` 写入，而该操作只由用户点击触发 | `test_tool_creates_pending_proposal_without_touching_body`；HTTP 冒烟第 1 步 |
| 迟到响应不能覆盖新编辑 | ①上下文带 `revision`+`contentHash`；②`proposal.create` 校验 `expectedBase*`，不匹配 409 且不建提案；③`proposal.accept` 复核基线，不匹配标记 `conflicted` 且不写正文 | `test_late_response_cannot_propose_over_a_newer_edit`、`ProposalBaselineTests`；HTTP 冒烟第 3 步 |
| 失败不丢人工草稿 | 提案失败不触碰块与草稿缓存；草稿仍在 `localStorage` | 草稿机制未改动（P2 已测） |
| 审批状态不可伪造 | `acceptedBy` 由服务端写 `human`；接受请求只传 `proposalId`，不传正文/基线 | `test_accept_only_goes_through_proposal_operation` |

---

## 3. 契约

### 3.1 工具 `propose_document_patch`
仅在 `document_scope.mode == "propose_edit"` 且有 `blockId` 时提供，**不要求用户先选 Skill**。

```json
{
  "replacement_markdown": "完整的新正文",
  "replacement_title": "可选新标题",
  "explanation": "一到两句说明",
  "evidence_ids": ["ev_..."]
}
```
`workspaceId / documentId / blockId` 一律取自服务端解析的作用域，**不接受模型指定**。

### 3.2 `proposal.create` 新增可选基线校验
- `expectedBaseContentHash` / `expectedBaseBlockRevision`：与当前块不一致 → **409，且不创建提案、不写入任何状态**。

### 3.3 服务端操作入口
`run_server_operation(workspace_id, op, params)`：
- operationId = `agent_<sha256(op+params)[:40]>`（确定性 → 重复调用去重）。
- 读当前 revision 后立即提交；遇到"Workspace revision conflict"重试一次，**基线冲突不重试**。

---

## 4. 界面流程

1. 块底部 `✎ AI 修改` → 输入框上方出现「AI 修改模式（只提议，不改正文）」提示条。
2. 输入"把这段改得更严谨" → 发送。请求只带引用。
3. agent 调 `propose_document_patch` → 提案入库，**正文不动**。
4. 回答结束后自动同步：块上出现 `AI 提案 ×1 待确认`。
5. 点开 → 左「当前正文（未改动）」/ 右「提案正文（接受后才会写入）」+ 说明 + 基线版本。
6. **接受** → `proposal.accept` → 正文更新、`updatedBy=agent`、`AI 已采纳` 标记、可撤销。
7. **拒绝** → `proposal.reject`，正文不变。
8. 若编辑期间块被改过 → 显示「基线已变化：块在你确认前被改过，提案已标记为冲突」，正文保持用户版本。

---

## 5. 改动清单

| 文件 | 改动 |
|---|---|
| `backend/workspace.py` | `proposal.create` 增加 `expectedBase*` 校验；新增 `run_server_operation` 与确定性 operationId。 |
| `backend/agent.py` | 新增 `PROPOSE_DOCUMENT_PATCH_TOOL`；作用域上下文加基线统计与 `<editing_instructions>`；工具按作用域提供；`_execute_agent_tool` 处理提案工具；`run_agent` 传入基线。 |
| `InferTree.html` | 提案 overlay（HTML/CSS）、徽标、`✎ AI 修改`、预览/接受/拒绝/关闭、接受前记撤销、回答后同步提案（不打断编辑）、图表块入口 `＋ 图表块`。 |
| `tests/test_proposal_tool.py` | 新增 8 个后端测试。 |
| `tests/test_frontend_proposals.py` | 新增 12 个前端契约测试。 |
| `tests/test_frontend_document_scope.py` | +1 图表块入口测试。 |

---

## 6. 验证证据

### 6.1 测试
```
.\.venv\Scripts\python -m unittest discover -s tests
→ Ran 157 tests in 4.303s
→ OK      （P3 时 136，新增 21）
```
- `node --check` 内联脚本 → 退出码 **0**。
- 元素 ID 交叉校验：声明 99、引用 84、**缺失 0**。

### 6.2 真实 HTTP 提案生命周期（独立 8123 + 临时工作区 `p4smoke`，用后已删）
| # | 动作 | 结果 |
|---|---|---|
| 1 | 创建提案 | 提案 `pending`；**正文仍是 `original`，`updatedBy=human`** |
| 2 | 接受 | `accepted`；正文 → `proposed A`，`updatedBy=agent` |
| 3 | 期间人工改正文后再接受（迟到响应） | **HTTP 409**；提案 `conflicted`；正文保持 `human edit wins` |
| 4 | 拒绝 | `rejected`；正文不变 |

未读写 `data/workspaces/default`；结束后删除临时目录并确认 8123 无监听。

---

## 7. 已知限制

- 提案粒度是整块替换；没有"只改这一段"的局部提案。
- 预览非逐行 diff。
- 同一块有多个待确认提案时，徽标只打开第一个；暂不支持列表切换。
- 图表块要求该图表有关联论文（`paperId`），否则明确拒绝而不是猜。
- 提案不做"过期自动拒绝"，会一直保持 `pending` 直到用户决定。

---

## 8. 下一步：P5 验收与交付

- 跑全量回归 + 真实浏览器行为验收。
- 在用户允许时做一次**低成本真实模型 smoke test**（预计 1–2 次调用），验证工具链端到端。
- 更新 `README` 与 `docs/architecture.md`、`docs/handover.md` 的状态，补迁移/回退说明。
- 交付差异概览、测试证据、演示步骤，等待确认；**不自行推进知识画布阶段**。
