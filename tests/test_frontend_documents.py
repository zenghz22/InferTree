from pathlib import Path
import unittest


FRONTEND = Path(__file__).resolve().parents[1] / "InferTree.html"


class FrontendDocumentPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = FRONTEND.read_text(encoding="utf-8")

    def _module(self) -> str:
        start = self.source.index("/* ===================== 调研文档（P2） ===================== */")
        end = self.source.index("/* ===================== 启动 ===================== */", start)
        return self.source[start:end]

    def test_document_panel_structure_exists(self) -> None:
        for element_id in (
            "docPanel", "toggleDocs", "closeDocBtn", "docList", "newDocBtn",
            "docTitleInput", "docSaveStatus", "docBlocks", "addBlockBtn",
            "docUndoBtn", "docOutline", "addToDocBtn",
        ):
            self.assertIn(f'id="{element_id}"', self.source)

    def test_edits_go_through_operation_endpoint_with_base_revision(self) -> None:
        module = self._module()
        self.assertIn("async function runOperation(op, params = {})", module)
        self.assertIn("/operations`", module)
        self.assertIn("operationId", module)
        self.assertIn("baseRevision: workspaceRevision", module)
        self.assertIn("newOperationId()", module)

    def test_block_update_sends_block_baseline(self) -> None:
        module = self._module()
        self.assertIn("baseBlockRevision: block.revision", module)
        self.assertIn("baseContentHash: block.contentHash", module)
        self.assertIn("runOperation('block.update'", module)

    def test_stale_revision_is_retried_once_with_same_operation_id(self) -> None:
        module = self._module()
        self.assertIn("attempt < 1", module)
        self.assertIn("continue;", module)
        # 幂等重试必须复用同一个 operationId（在循环外生成）
        self.assertLess(
            module.index("const operationId = newOperationId();"),
            module.index("while (true)"),
        )

    def test_save_feedback_states_are_rendered(self) -> None:
        module = self._module()
        for text in ("保存中…", "已保存 · rev", "保存失败：", "未保存…"):
            self.assertIn(text, module)
        self.assertIn("function setDocStatus(text, kind)", module)

    def test_edits_autosave_without_model_call(self) -> None:
        module = self._module()
        self.assertIn("function scheduleBlockSave(", module)
        self.assertIn("setTimeout(", module)
        self.assertIn("async function saveBlock(docId, blockId, values)", module)
        # 编辑路径不应触发 Agent 请求
        self.assertNotIn("/api/agent/chat", module)

    def test_draft_cache_and_recovery(self) -> None:
        module = self._module()
        self.assertIn("inferTree:draft:v1:", module)
        self.assertIn("function cacheDraft(", module)
        self.assertIn("function readDraft(", module)
        self.assertIn("function clearDraft(", module)
        self.assertIn("恢复未保存草稿", module)

    def test_session_undo_replays_inverse_operations(self) -> None:
        module = self._module()
        self.assertIn("let docUndoStack = []", module)
        self.assertIn("function pushDocUndo(entry)", module)
        self.assertIn("async function undoDocument()", module)
        self.assertIn("kind === 'update'", module)
        self.assertIn("kind === 'add'", module)
        self.assertIn("kind === 'remove'", module)
        self.assertIn("kind === 'move'", module)

    def test_outline_is_deterministic_from_block_titles(self) -> None:
        module = self._module()
        self.assertIn("function renderDocOutline()", module)
        self.assertIn("block.title || '（无标题）'", module)
        # 大纲不得调用模型
        self.assertNotIn("agent/chat", module)

    def test_add_answer_or_selection_preserves_source_node(self) -> None:
        module = self._module()
        self.assertIn("async function addAnswerToDocument()", module)
        self.assertIn("sourceRefs: [{ nodeId: sourceNodeId }]", module)
        self.assertIn("localSelection", module)
        self.assertIn("摘录：", module)
        self.assertIn("runOperation('block.add'", module)
        self.assertIn("MAX_DOC_BLOCK_CHARS", module)

    def test_documents_are_loaded_from_workspace_state(self) -> None:
        self.assertIn("loadDocumentsFromState(result.state)", self.source)
        self.assertIn("function loadDocumentsFromState(state)", self.source)
        self.assertIn("state.documents", self.source)

    def test_figure_asset_failure_is_explicit_not_silent(self) -> None:
        module = self._module()
        self.assertIn("/visuals/${asset.visualId}.png", module)
        self.assertIn("图表资源缺失", module)
        self.assertIn("不会回退到其他图", module)

    def test_document_panel_is_hidden_by_default(self) -> None:
        self.assertIn('<section id="docPanel" aria-label="调研文档" hidden>', self.source)
        self.assertIn("function openDocPanel()", self.source)
        self.assertIn("function closeDocPanel()", self.source)
        self.assertIn("body.doc-open", self.source)

    def test_block_editor_supports_edit_and_preview(self) -> None:
        module = self._module()
        self.assertIn("doc-block-preview", module)
        self.assertIn("renderMd(mdInput.value)", module)
        self.assertIn("previewToggle.textContent = showPreview ? '编辑' : '预览'", module)

    def test_doc_state_is_separate_from_tree_state(self) -> None:
        module = self._module()
        # 打开文档不得改变会话树选择
        open_start = module.index("function openDocPanel()")
        open_end = module.index("function closeDocPanel()", open_start)
        self.assertNotIn("selectedId =", module[open_start:open_end])
        self.assertNotIn("saveTree()", module[open_start:open_end])


class FrontendSaveProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = FRONTEND.read_text(encoding="utf-8")

    def test_tree_save_sends_base_revision(self) -> None:
        self.assertIn("baseRevision: workspaceRevision", self.source)

    def test_tree_conflict_is_not_blindly_overwritten(self) -> None:
        self.assertIn("async function reconcileTreeConflict()", self.source)
        self.assertIn("deepEqualTree(serverTree, lastCommittedTree)", self.source)
        self.assertIn("检测到其他页面已修改会话树", self.source)
        self.assertIn("workspaceConflict = true", self.source)

    def test_local_storage_key_is_namespaced(self) -> None:
        self.assertIn("const TREE_STORAGE_KEY = `inferTree:tree:v2:${WORKSPACE_ID}`;", self.source)
        self.assertIn("safeLocalStorageGet('tree', 'null')", self.source)  # 一次性迁移旧键

    def test_tree_save_omits_global_active_paper(self) -> None:
        start = self.source.index("function currentWorkspaceState()")
        end = self.source.index("function stableStringify(", start)
        body = self.source[start:end]
        self.assertNotIn("active_paper", body)


if __name__ == "__main__":
    unittest.main()
