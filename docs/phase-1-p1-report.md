# P1 交付报告：v2 数据层与 operation 接口

> 依据 `docs/phase-1-collaborative-artifacts-handoff.md` 的 P1 门槛与评审约束。
> 状态：待用户与同行评审确认；**未替换日常使用服务，未迁移真实 Workspace**。
> 日期：2026-10-15

---

## 1. 范围

### 1.1 已完成（P1 范围）
- v2 数据模型：`state.documents / evidence / proposals / dedup` 四个子状态，与 `tree / active_paper` 并存。
- 校验与完整 round-trip（含块 contentHash 服务端复算校验）。
- `baseRevision` 冲突保护：树 PUT 与 operation 共用同一读—比较—写原子机制与 revision。
- 统一 operation 接口：`POST /api/workspaces/{id}/operations`，10 个单一职责 op。
- 幂等：`operationId + 请求内容指纹 + 结果摘要 + 提交 revision`，与状态更新**原子落盘**，FIFO 500 窗口。
- v1→v2 兼容迁移（幂等；磁盘版本在保存成功后才切换）。
- 论文索引改为**树 + 文档证据的并集**。
- 隔离测试（临时目录 + `patch(WORKSPACE_ROOT)`，不触碰真实数据）。

### 1.2 明确未实现（按 P1 范围排除）
- 文档 UI、知识画布、LivingCanvas 改动、`propose_document_patch` 工具（P4）。
- 前端保存协议适配（评审要求放在 **P2 第一项**）。
- 真实 Workspace 迁移与生产备份（本阶段一律用临时目录验证）。

---

## 2. 数据契约

沿用单文件 `workspace.json` envelope，`schema_version: 2`：

```jsonc
{
  "schema_version": 2,
  "revision": 42,
  "state": {
    "tree": { "rootId": "...", "selectedId": "...", "nodes": { } },
    "active_paper": null,           // 旧数据兼容字段，省略时不清空，非全局论文指针
    "documents": { "<docId>": { } },
    "evidence":   { "<evId>":  { } },
    "proposals":  { "<propId>": { } },
    "dedup":      { "<operationId>": { "fingerprint": "...", "result": { }, "committed_revision": 5, "ts": "..." } }
  }
}
```

关键实现约定（回应评审约束）：

- **规范化 hash**：`contentHash = sha256(stable_json({"type", "title", "body"} + figure 的 {"assetRef"}))`，固定字段、固定序列化（`sort_keys` + `separators`），消除「title + "\n" + markdown」的串接歧义；图表块的 `assetRef` 纳入变更检测。服务端在写入时计算、在加载时复算校验（不信任客户端/模型传来的 hash）。
- **冲突模型**：
  - 树 PUT：`baseRevision` 匹配 → **顶层替换** `state.tree`，其余子状态原样保留（非递归合并）；不匹配 → 409 + `current_revision`。
  - `block.update`：额外校验 `baseBlockRevision + baseContentHash`。
  - `proposal.accept`：校验提案捕获的 `baseBlockRevision + baseContentHash`；不匹配 → 提案标 `conflicted` 并 409。`proposal.reject` **不校验**块基线。
- **幂等**：重复 `operationId` 且指纹一致 → 先于版本校验识别为重复，返回「原提交 revision」与「当前 revision」两个字段，不因旧 baseRevision 误报冲突；同 ID 不同内容 → 409 拒绝。窗口 500 FIFO，淘汰后**不可**通过换新基线重放（客户端契约，见第 7 节）。

---

## 3. 改动清单

| 文件 | 改动 |
|---|---|
| `backend/workspace.py` | 重写为 v2：新增子状态校验、`_block_content_hash`、`apply_operation`/`_execute_operation`、`_papers_manifest` 并集；`save_workspace` 增加 baseRevision 与顶层合并；`load_workspace` 增加版本检测与迁移。保留 tree 校验、`_atomic_write_json`、`_WRITE_LOCK`、目录迁移不变。 |
| `backend/main.py` | 新增 `POST /api/workspaces/{id}/operations`；导入 `apply_operation`。 |
| `tests/test_workspace.py` | 现有测试的 PUT 增加 `baseRevision`（首次 0、第二次 1）。 |
| `tests/test_workspace_v2.py` | 新增 17 个行为测试。 |
| `LivingCanvas` | **未改动**。 |

---

## 4. 与 P0 计划 / 评审约束的偏差

- 新增 `evidence.upsert` 与 `proposal.create` 两个 op（P0 计划未显式列出）：evidence/proposals 是独立权威子状态，必须经同一 operation 入口写入，`proposal.create` 由服务端**捕获**块基线（不信任客户端传入），供 P4 的 `propose_document_patch` 工具复用。
- 其余约束（单一职责 op、树 PUT 拒绝夹带文档字段、幂等字段、reject 不校验基线、accept 严格校验、active_paper 兼容语义）均按评审要求落实。

---

## 5. 迁移与回退

- **v1→v2 迁移**：`load_workspace` 读到 `schema_version: 1` 时，为 state 补齐空 `documents/evidence/proposals/dedup`，树/active_paper 全量保留；转换**幂等**（多次 load 结果一致），磁盘 `workspace.json` 在**下次成功保存**时才写为 v2。
- **未知版本**：`schema_version` 不在 {1,2} → 413 拒绝，不降级、不覆盖原文件。
- **备份**：本阶段未迁移真实 `data/workspaces/default`，也未对其做写操作；全部测试使用 `TemporaryDirectory` + patch。进入 P2 前，将按评审要求对**实际待迁移 Workspace** 做一致性备份（暂停写入或快照，覆盖图表资源重建路径），再适配前端。
- **回退边界**：**不声称**旧二进制可安全读 v2 文件；回退需恢复备份或用「仅树」导出。

---

## 6. 测试证据

**命令**（工作目录 `InferTree`，真实输出）：

```powershell
$env:PYTHONDONTWRITEBYTECODE = "1"
.\.venv\Scripts\python -m unittest discover -s tests
```

**结果**：`Ran 92 tests in 2.609s` → **OK**，进程退出码 **0**（75 原有 + 17 新增；无 mock 真实模型调用，本阶段全为后端单元/接口测试）。

**评审要求的行为证据 → 测试名**：

| 评审要求 | 测试 |
|---|---|
| v1→v2 幂等迁移 | `test_v1_to_v2_migration_is_idempotent` |
| 完整 round-trip | `test_full_round_trip_preserves_document_and_stable_hash` |
| 旧树 PUT 不丢文档 | `test_tree_put_does_not_lose_documents` |
| 过期版本拒绝 | `test_stale_revision_is_rejected_for_tree_and_operation` |
| 重复操作不重复执行 | `test_duplicate_operation_is_not_reexecuted` |
| 同 ID 不同请求被拒绝 | `test_same_operation_id_with_different_content_is_rejected` |
| 写入失败保留旧文件 | `test_failed_operation_preserves_previous_file` |
| 未知版本拒绝 | `test_unknown_schema_version_is_rejected_without_downgrade` |
| 文档独立引用论文仍进索引 | `test_document_only_paper_enters_index` |

补充覆盖：提案冲突（`test_proposal_accept_conflicts_when_block_changed`）、reject 不校验基线（`test_proposal_reject_does_not_require_baseline_match`）、accept 应用替换（`test_proposal_accept_applies_replacement`）、assetRef 不可改（`test_block_update_cannot_change_asset_ref`）、排序（`test_block_move_reorders`）、移出清理待处理提案（`test_block_remove_cleans_pending_proposals`）、树 PUT 夹带文档字段拒绝（`test_tree_put_with_document_fields_is_rejected`）、无树时 operation 拒绝（`test_operation_without_tree_is_rejected`）。

---

## 7. 已知限制

- **单进程本地部署**：并发保护靠 `threading.Lock`，未宣称跨进程安全（多进程需数据库/锁）。
- **papers.json 为派生文件**：写入顺序为「先权威 workspace.json、后派生 papers.json」；若 papers.json 写入失败，权威文档不受影响，`load_workspace` 会用当前 workspace.json 重建 papers.json（自愈）。
- **dedup 窗口边界**：500 条 FIFO；淘汰后的 operationId 不再可判重，客户端不得通过刷新基线重放。
- **撤销**：属 P2，本阶段未实现；`block.update` 等均是有版本写入（不回退全局 revision），已为撤销铺好语义。
- **active_paper**：保留为兼容字段，不参与文档/块定位。
- **树 PUT 的 409**：后端只返回冲突，不自动换基线重提交（由 P2 前端负责），从根上避免覆盖他页修改。

---

## 8. 评审待办 / 下一阶段

1. 请确认新增 `evidence.upsert`、`proposal.create` 两个 op 可接受（第 4 节）。
2. 请批准进入 **P2**：第一项即「前端保存协议适配」（补 `baseRevision`、处理 409、localStorage 键加 schema/workspace 标识），再实现文档 UI。
3. 图表 URL：已按评审「降低保证强度」处理——`EvidenceRef` 存 `paperId + visualId`，资源缺失明确报错、不静默匹配；本阶段未改识别算法。
