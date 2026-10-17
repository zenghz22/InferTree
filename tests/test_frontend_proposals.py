"""P4 前端契约：提案审阅。

门槛：模型输出与迟到响应都不能绕过用户确认修改正文。
"""

from pathlib import Path
import unittest


FRONTEND = Path(__file__).resolve().parents[1] / "InferTree.html"


class FrontendProposalTests(unittest.TestCase):
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

    def test_preview_overlay_structure_exists(self) -> None:
        for element_id in (
            "proposalOverlay", "proposalCurrent", "proposalNext",
            "proposalAcceptBtn", "proposalRejectBtn", "proposalCloseBtn",
            "proposalMeta", "proposalStatus",
        ):
            self.assertIn(f'id="{element_id}"', self.source)

    def test_preview_distinguishes_current_from_proposed(self) -> None:
        self.assertIn("当前正文（未改动）", self.source)
        self.assertIn("提案正文（接受后才会写入）", self.source)

    def test_preview_renders_sanitized_markdown(self) -> None:
        fn = self._fn("renderProposalOverlay")
        self.assertIn("renderMd(proposal.replacementMarkdown", fn)
        self.assertNotIn("innerHTML = proposal.replacementMarkdown", fn)

    def test_accept_only_goes_through_proposal_operation(self) -> None:
        fn = self._fn("acceptProposal")
        self.assertIn("runOperation('proposal.accept'", fn)
        # 接受路径不得直接改正文，也不得由客户端指定基线
        self.assertNotIn("block.update", fn)
        self.assertNotIn("baseContentHash", fn)
        self.assertNotIn("replacementMarkdown", fn)

    def test_reject_goes_through_proposal_operation(self) -> None:
        fn = self._fn("rejectProposal")
        self.assertIn("runOperation('proposal.reject'", fn)
        self.assertNotIn("block.update", fn)

    def test_accept_records_undo_before_committing(self) -> None:
        fn = self._fn("acceptProposal")
        self.assertLess(fn.index("pushDocUndo("), fn.index("runOperation('proposal.accept'"))
        self.assertIn("kind: 'update'", fn)

    def test_conflict_is_reported_not_claimed_as_success(self) -> None:
        fn = self._fn("acceptProposal")
        self.assertIn("proposal_conflicted", fn)
        self.assertIn("基线已变化", fn)
        catch_index = fn.index("catch (error)")
        # 成功提示只在 try 内；冲突处理在 catch 内。
        self.assertLess(fn.index("已采纳提案"), catch_index)
        self.assertGreater(fn.index("proposal_conflicted"), catch_index)
        self.assertGreater(fn.index("基线已变化"), catch_index)

    def test_revision_mode_is_explicit(self) -> None:
        fn = self._fn("requestBlockRevision")
        self.assertIn("mode: 'propose_edit'", fn)
        self.assertIn("setPendingDocScope", fn)
        scope_bar = self._fn("renderDocScopeBar")
        self.assertIn("只提议，不改正文", scope_bar)

    def test_pending_proposals_surface_on_the_block(self) -> None:
        fn = self._fn("renderBlockElement")
        self.assertIn("proposalsForBlock(block.id)", fn)
        self.assertIn("openProposalPreview(pendingProposals[0].id)", fn)
        self.assertIn("requestBlockRevision(doc.id, block.id)", fn)
        filter_fn = self._fn("proposalsForBlock")
        self.assertIn("item.status === 'pending'", filter_fn)

    def test_proposals_are_loaded_from_workspace_state(self) -> None:
        fn = self._fn("loadDocumentsFromState")
        self.assertIn("proposals =", fn)
        self.assertIn("state.proposals", fn)

    def test_refresh_after_answer_does_not_clobber_active_editing(self) -> None:
        fn = self._fn("refreshProposalsAfterAnswer")
        self.assertIn("closest('#docBlocks')", fn)
        self.assertIn("!editing", fn)

    def test_answer_refreshes_proposals_only_for_scoped_nodes(self) -> None:
        self.assertIn("if (node.documentScope) refreshProposalsAfterAnswer();", self.source)


if __name__ == "__main__":
    unittest.main()
