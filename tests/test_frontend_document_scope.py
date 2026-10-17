"""P3 前端契约：文档作用域请求与证据引用。

门槛：
- 打开的是明确引用的论文，而不是另一分支的 PDF。
- 无关树分支内容不泄漏进文档请求。
"""

from pathlib import Path
import unittest


FRONTEND = Path(__file__).resolve().parents[1] / "InferTree.html"


class FrontendDocumentScopeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = FRONTEND.read_text(encoding="utf-8")

    def _fn(self, name: str) -> str:
        start = self.source.index(f"function {name}(")
        candidates = [
            index for index in (
                self.source.find("\nfunction ", start + 1),
                self.source.find("\n/* =", start + 1),
            ) if index != -1
        ]
        end = min(candidates) if candidates else len(self.source)
        return self.source[start:end]

    # ---- 作用域请求只发引用 -----------------------------------------------

    def test_document_scope_request_carries_only_the_question(self) -> None:
        fn = self._fn("buildDocumentScopeContext")
        self.assertIn("messages: [{ role: 'user', content: question }]", fn)
        self.assertIn("document_scope: node.documentScope", fn)
        # 只发引用，不发正文
        self.assertNotIn("block.markdown", fn)
        self.assertNotIn("buildContext(", fn)

    def test_run_answer_uses_document_scope_branch(self) -> None:
        fn = self._fn("runAnswer")
        self.assertIn("node.documentScope", fn)
        self.assertIn("buildDocumentScopeContext(node)", fn)
        self.assertIn("if (context.document_scope) body.document_scope = context.document_scope;", fn)

    def test_document_scope_never_inherits_branch_paper(self) -> None:
        fn = self._fn("runAnswer")
        self.assertIn("? (context.document_scope.paperId || null)", fn)
        self.assertIn(": (node.paperId || null);", fn)
        self.assertNotIn("body.paper_id = node.paperId", fn)

    def test_child_node_records_scope_for_provenance(self) -> None:
        fn = self._fn("buildChild")
        self.assertIn("if (opts.documentScope) child.documentScope = { ...opts.documentScope };", fn)

    def test_composer_clears_scope_after_send(self) -> None:
        fn = self._fn("sendBranch")
        self.assertIn("pendingDocScope", fn)
        self.assertIn("documentScope: docScope", fn)
        self.assertIn("if (docScope) clearPendingDocScope();", fn)
        # 有文档作用域时，父节点取当前选中节点而不是选区来源
        self.assertIn("docScope ? nodes[selectedId]", fn)

    def test_scope_bar_is_rendered_and_dismissible(self) -> None:
        self.assertIn('id="docScopeBar"', self.source)
        self.assertIn("docScopeBar: $('docScopeBar')", self.source)
        self.assertIn("function renderDocScopeBar()", self.source)
        self.assertIn("function clearPendingDocScope()", self.source)
        self.assertIn("clearPendingDocScope();", self._fn("closeDocPanel"))

    # ---- 明确引用论文 -----------------------------------------------------

    def test_single_paper_block_binds_exactly_that_paper(self) -> None:
        fn = self._fn("askAboutBlock")
        self.assertIn("blockPaperIds(block)", fn)
        self.assertIn("paperIds.length === 1 ? paperIds[0] : null", fn)
        self.assertIn("mode: 'followup'", fn)
        self.assertIn("workspaceId: WORKSPACE_ID", fn)

    def test_evidence_chip_opens_its_own_paper(self) -> None:
        fn = self._fn("openEvidence")
        self.assertIn("evidence.paperId", fn)
        self.assertIn("openPdfAtPage(evidence.page || 1", fn)
        # 不得回退到当前分支绑定的 PDF
        self.assertNotIn("paperForNode", fn)

    def test_evidence_chips_render_from_server_state(self) -> None:
        fn = self._fn("renderBlockElement")
        self.assertIn("block.evidenceIds", fn)
        self.assertIn("evidenceById[evidenceId]", fn)
        self.assertIn("openEvidence(evidence)", fn)
        self.assertIn("evidence.locatorStatus === 'located'", fn)

    def test_evidence_state_is_loaded_with_documents(self) -> None:
        fn = self._fn("loadDocumentsFromState")
        self.assertIn("evidenceById =", fn)
        self.assertIn("state.evidence", fn)

    def test_attach_evidence_uses_both_operations_in_order(self) -> None:
        fn = self._fn("attachPageEvidence")
        self.assertIn("runOperation('evidence.upsert'", fn)
        self.assertIn("runOperation('block.evidence.set'", fn)
        self.assertLess(fn.index("evidence.upsert"), fn.index("block.evidence.set"))
        self.assertIn("locatorStatus: 'located'", fn)
        self.assertIn("pdfViewState.paperId", fn)

    def test_pdf_view_state_tracks_open_paper_and_page(self) -> None:
        fn = self._fn("openPdfAtPage")
        self.assertIn("pdfViewState = { paperId: selectedPaper.paperId, page: pageNumber }", fn)

    def test_blocks_offer_ask_and_attach_actions(self) -> None:
        fn = self._fn("renderBlockElement")
        self.assertIn("askAboutBlock(doc.id, block.id)", fn)
        self.assertIn("attachPageEvidence(doc.id, block.id)", fn)
        self.assertIn("就此提问", fn)
        self.assertIn("当前页为证据", fn)


if __name__ == "__main__":
    unittest.main()
