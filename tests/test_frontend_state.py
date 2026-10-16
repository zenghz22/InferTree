from pathlib import Path
import unittest


FRONTEND = Path(__file__).resolve().parents[1] / "InferTree.html"


class FrontendGenerationStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = FRONTEND.read_text(encoding="utf-8")

    def test_storage_failure_is_handled_without_blocking_agent(self) -> None:
        start = self.source.index("function saveTree()")
        end = self.source.index("function newTree()", start)
        save_tree = self.source[start:end]
        self.assertIn("catch (error)", save_tree)
        self.assertIn("compactTreeForStorage()", save_tree)
        self.assertIn("return false", save_tree)

    def test_stop_is_enabled_before_persistence_and_rendering(self) -> None:
        start = self.source.index("async function runAnswer(node)")
        end = self.source.index("function setBusy(b)", start)
        run_answer = self.source[start:end]
        busy = run_answer.index("setBusy(true)")
        save = run_answer.index("saveTree()")
        render = run_answer.index("renderTree()")
        self.assertLess(busy, save)
        self.assertLess(busy, render)

    def test_branch_and_regenerate_use_guarded_runner(self) -> None:
        self.assertGreaterEqual(self.source.count("await startAnswerSafely("), 2)
        self.assertIn("node.title = '前端出错'", self.source)

    def test_assistant_render_state_is_shared_across_render_blocks(self) -> None:
        start = self.source.index("function addMsg(")
        end = self.source.index("function activityDetail(", start)
        add_msg = self.source[start:end]
        first_assistant_block = add_msg.index("if (role === 'assistant')")
        self.assertLess(add_msg.index("const activityList"), first_assistant_block)
        self.assertLess(add_msg.index("const hasRunning"), first_assistant_block)
        self.assertLess(add_msg.index("const hasS"), first_assistant_block)

    def test_multi_turn_path_builds_clickable_turn_navigation(self) -> None:
        self.assertIn('id="turnNav"', self.source)
        self.assertIn("function renderTurnNav(path)", self.source)
        self.assertIn("turns.length < 2", self.source)
        self.assertIn("button.addEventListener('click', () => jumpToTurn(node.id))", self.source)
        self.assertIn("behavior: 'smooth'", self.source)
        self.assertIn("updateActiveTurnNav", self.source)

    def test_light_cream_and_blue_theme_tokens_are_present(self) -> None:
        self.assertIn("--bg: #FAF2E0", self.source)
        self.assertIn("--accent: #0E61AC", self.source)

    def test_theme_is_loaded_from_safe_backend_endpoint(self) -> None:
        self.assertIn("async function loadUiTheme()", self.source)
        self.assertIn("fetch('/api/ui-config')", self.source)
        self.assertIn("--accent-contrast", self.source)

    def test_markdown_keeps_soft_line_breaks_inside_paragraphs(self) -> None:
        self.assertIn("marked.use({ breaks: false, gfm: true })", self.source)
        self.assertIn(".msg .c { white-space: normal; }", self.source)
        self.assertNotIn("marked.use({ breaks: true, gfm: true })", self.source)

    def test_header_is_single_line_and_paper_name_is_compacted(self) -> None:
        self.assertIn("flex-wrap: nowrap", self.source)
        self.assertIn("function setPaperStatus(paper, suffix = '')", self.source)
        self.assertIn("compactText(fullName)", self.source)
        self.assertIn("`阅读 PDF · ${compactText(", self.source)

    def test_selected_conversation_text_creates_local_followup(self) -> None:
        self.assertIn("function captureConversationSelection()", self.source)
        self.assertIn("已选定段落局部追问", self.source)
        self.assertIn("child.localExcerpt = excerpt.text", self.source)
        self.assertIn("nodes[excerpt?.sourceNodeId] || nodes[selectedId]", self.source)
        self.assertIn("modelQueryForNode(node)", self.source)
        self.assertIn("local-followup", self.source)

    def test_workspace_uses_backend_with_local_storage_fallback(self) -> None:
        self.assertIn("function loadWorkspace()", self.source)
        self.assertIn("function flushWorkspaceSave(keepalive = false)", self.source)
        self.assertIn("fetch(`/api/workspaces/${WORKSPACE_ID}`", self.source)
        self.assertIn("继续使用浏览器 localStorage", self.source)
        self.assertIn("window.addEventListener('pagehide'", self.source)

    def test_pdf_reader_links_page_citations(self) -> None:
        self.assertIn('id="pdfPanel"', self.source)
        self.assertIn("function openPdfAtPage(page = 1, paper = null)", self.source)
        self.assertIn("function linkPaperCitations(container, paperId)", self.source)
        self.assertIn("/file#page=${pageNumber}&zoom=page-width", self.source)
        self.assertIn("className = 'page-citation'", self.source)

    def test_paper_binding_is_branch_scoped_not_global(self) -> None:
        self.assertIn("function paperForNode(nodeId = selectedId)", self.source)
        self.assertIn("function bindPaperToNode(nodeId, paper)", self.source)
        self.assertIn("const branchPaper = paperForNode(parent.id)", self.source)
        self.assertIn("child.paper = { ...branchPaper }", self.source)
        self.assertNotIn("active_paper: null", self.source)
        self.assertIn("baseRevision: workspaceRevision", self.source)
        self.assertIn("TREE_STORAGE_KEY", self.source)
        self.assertIn("reconcileTreeConflict", self.source)
        self.assertIn("function hydratePaperMetadata(paperId)", self.source)
        self.assertIn("/metadata`", self.source)
        self.assertNotIn("function getActivePaper()", self.source)
        self.assertNotIn("storeActivePaper(", self.source)

    def test_tree_context_uses_ancestor_path_budget_and_reports_compaction(self) -> None:
        self.assertIn("function buildContext(targetId, extraQuery)", self.source)
        self.assertIn("ancestor-path-budget-v1", self.source)
        self.assertIn("Earlier ancestor turns were omitted by the context budget", self.source)
        self.assertIn("context_meta: context.stats", self.source)
        self.assertIn("上下文：${detail.included_turns}/${detail.total_turns} 轮", self.source)
        self.assertIn("function contextBadge(nodeId = selectedId)", self.source)

    def test_eval_console_requires_confirmation_for_live_model_runs(self) -> None:
        self.assertIn('id="evalPanel"', self.source)
        self.assertIn("async function startEvalRunFromUi()", self.source)
        self.assertIn("将运行 ${samples} 个真实 Agent 样本并消耗模型额度", self.source)
        self.assertIn("fetch('/api/evals/runs'", self.source)
        self.assertIn("function renderEvalRun(run)", self.source)
        self.assertIn("cancelCurrentEval", self.source)

    def test_prompt_controls_live_with_the_composer_not_the_header(self) -> None:
        header = self.source[self.source.index("<header>"):self.source.index("</header>")]
        composer = self.source[self.source.index('<div id="composer">'):self.source.index("</div>\n  </div>\n</div>", self.source.index('<div id="composer">'))]
        for control_id in ("modelSelect", "skillSelect", "skillModeSelect", "thinkOn", "uploadPaperBtn"):
            self.assertNotIn(f'id="{control_id}"', header)
            self.assertIn(f'id="{control_id}"', composer)

    def test_auto_skill_is_default_and_forwarded_to_backend(self) -> None:
        self.assertIn('<option value="auto">Skill：自动</option>', self.source)
        self.assertIn("els.skill.value === 'off' ? ''", self.source)
        self.assertIn("['auto', 'paper-reviewer'].includes(selectedSkill)", self.source)


if __name__ == "__main__":
    unittest.main()
