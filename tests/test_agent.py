import asyncio
import unittest

from fastapi import HTTPException

from backend.agent import (
    _bounded_conversation_messages,
    _execute_agent_tool,
    _paper_reviewer_skill_text,
    _upstream_request,
    run_agent,
)
from backend.config import AgentConfig, ContextConfig, ModelConfig, ModelProfile, TavilyConfig


class AgentRequestTests(unittest.TestCase):
    @staticmethod
    def config(base_url="https://example.test/v1"):
        return AgentConfig(
            name="Test Agent",
            instructions="Agent instructions",
            model=ModelConfig(
                base_url=base_url,
                name="server-model",
                api_key="secret",
                connect_timeout_seconds=0.2,
                read_timeout_seconds=0.2,
                use_system_proxy=False,
            ),
            tavily=TavilyConfig(enabled=False, api_key="", max_results=5),
        )

    def test_backend_owns_model_instructions_and_tools(self):
        config = self.config()
        request = _upstream_request(
            {
                "model": "browser-model",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
                "tools": [{"type": "browser-owned-tool"}],
            },
            config,
        )
        self.assertEqual("server-model", request["model"])
        self.assertEqual("system", request["messages"][0]["role"])
        self.assertIn("Group related sentences into coherent paragraphs", request["messages"][0]["content"])
        self.assertNotIn("tools", request)

    def test_model_always_sees_installed_skill_catalog(self):
        request = _upstream_request(
            {"messages": [{"role": "user", "content": "hello"}]}, self.config()
        )
        system = request["messages"][0]["content"]
        self.assertIn("<available_skills>", system)
        self.assertIn('<skill name="paper-reviewer">', system)
        self.assertIn('<skill name="paper-visual-extractor">', system)
        self.assertIn('<skill name="paper-downloader">', system)
        self.assertNotIn("# Paper Reviewer", system)

    def configured_profile(self, model_id, family, name):
        base = self.config()
        profile = ModelProfile(model_id, name, "Vendor", family, name)
        return AgentConfig(
            name=base.name,
            instructions=base.instructions,
            model=base.model,
            tavily=base.tavily,
            default_model_id=model_id,
            models={model_id: profile},
        )

    def test_qwen_reasoning_control_is_translated_server_side(self):
        config = self.configured_profile("qwen", "qwen", "qwen3.8-max")
        request = _upstream_request(
            {
                "model_id": "qwen",
                "messages": [{"role": "user", "content": "hello"}],
                "reasoning_control": {"enabled": True, "effort": "medium"},
                "thinking": {"type": "browser-value-must-not-pass"},
                "thinking_budget": 999999,
            },
            config,
        )
        self.assertEqual("qwen3.8-max", request["model"])
        self.assertTrue(request["enable_thinking"])
        self.assertEqual("medium", request["reasoning_effort"])
        self.assertNotIn("model_id", request)
        self.assertNotIn("reasoning_control", request)
        self.assertNotIn("thinking", request)
        self.assertNotIn("thinking_budget", request)

    def test_kimi_k3_is_always_thinking(self):
        config = self.configured_profile("kimi", "kimi", "kimi-k3")
        request = _upstream_request(
            {
                "model_id": "kimi",
                "messages": [{"role": "user", "content": "hello"}],
                "reasoning_control": {"enabled": True, "effort": "low"},
            },
            config,
        )
        self.assertEqual("low", request["reasoning_effort"])
        self.assertNotIn("thinking", request)
        with self.assertRaisesRegex(HTTPException, "always runs"):
            _upstream_request(
                {
                    "model_id": "kimi",
                    "messages": [{"role": "user", "content": "hello"}],
                    "reasoning_control": {"enabled": False},
                },
                config,
            )

    def test_deepseek_reasoning_control_supports_toggle_and_effort(self):
        config = self.configured_profile("deepseek", "deepseek", "deepseek-v4-pro")
        enabled = _upstream_request(
            {
                "messages": [{"role": "user", "content": "hello"}],
                "reasoning_control": {"enabled": True, "effort": "max"},
            },
            config,
        )
        self.assertEqual({"type": "enabled"}, enabled["thinking"])
        self.assertEqual("max", enabled["reasoning_effort"])
        disabled = _upstream_request(
            {
                "messages": [{"role": "user", "content": "hello"}],
                "reasoning_control": {"enabled": False},
            },
            config,
        )
        self.assertEqual({"type": "disabled"}, disabled["thinking"])
        self.assertNotIn("reasoning_effort", disabled)

    def test_glm_uses_only_documented_thinking_toggle(self):
        config = self.configured_profile("glm", "glm", "GLM-5.3")
        request = _upstream_request(
            {
                "messages": [{"role": "user", "content": "hello"}],
                "reasoning_control": {"enabled": True},
            },
            config,
        )
        self.assertEqual({"type": "enabled"}, request["thinking"])
        self.assertNotIn("reasoning_effort", request)

    def test_invalid_reasoning_effort_is_rejected(self):
        config = self.configured_profile("deepseek", "deepseek", "deepseek-v4-pro")
        with self.assertRaisesRegex(HTTPException, "Unsupported reasoning effort"):
            _upstream_request(
                {
                    "messages": [{"role": "user", "content": "hello"}],
                    "reasoning_control": {"enabled": True, "effort": "xhigh"},
                },
                config,
            )

    def test_stream_connection_failure_is_a_structured_sse_error(self):
        async def exercise():
            response = await run_agent(
                {
                    "messages": [{"role": "user", "content": "hello"}],
                    "stream": True,
                },
                self.config(base_url="http://127.0.0.1:9/v1"),
            )
            chunks = []
            async for chunk in response.body_iterator:
                chunks.append(chunk)
            return b"".join(chunks).decode("utf-8")

        body = asyncio.run(exercise())
        self.assertTrue(
            "MODEL_CONNECTION_ERROR" in body or "MODEL_TIMEOUT" in body,
            body,
        )
        self.assertIn("data: [DONE]", body)
        self.assertIn('"agent_activity"', body)
        self.assertIn('"action": "agent.run"', body)
        self.assertIn('"state": "failed"', body)

    def test_unknown_skill_is_rejected_before_model_call(self):
        with self.assertRaises(HTTPException) as caught:
            _upstream_request(
                {
                    "messages": [{"role": "user", "content": "hello"}],
                    "skill": "unknown-skill",
                },
                self.config(),
            )
        self.assertEqual(422, caught.exception.status_code)

    def test_auto_skill_exposes_router_without_preloading_a_skill(self):
        request = _upstream_request(
            {
                "messages": [{"role": "user", "content": "Explain this paper"}],
                "skill": "auto",
            },
            self.config(),
            include_tools=True,
        )
        tool_names = [tool["function"]["name"] for tool in request["tools"]]
        system = request["messages"][0]["content"]
        self.assertIn("activate_skill", tool_names)
        self.assertIn('<skill_routing mode="auto">', system)
        self.assertNotIn('<active_skill name="paper-reviewer">', system)

    def test_activate_skill_tool_validates_and_reports_selection(self):
        async def exercise(skill):
            return await _execute_agent_tool(
                "activate_skill",
                {"skill": skill, "reason": "The user asked for a review."},
                {},
                self.config(),
            )

        selected = asyncio.run(exercise("paper-reviewer"))
        self.assertEqual("completed", selected["state"])
        self.assertEqual("paper-reviewer", selected["activated_skill"])
        rejected = asyncio.run(exercise("made-up-skill"))
        self.assertEqual("failed", rejected["state"])
        self.assertIsNone(rejected["activated_skill"])

    def test_paper_reviewer_without_pdf_composes_downloader(self):
        config = self.config()
        request = _upstream_request(
            {
                "messages": [
                    {"role": "user", "content": "Review arXiv:1706.03762"}
                ],
                "skill": "paper-reviewer",
            },
            config,
            include_tools=True,
        )
        tool_names = [tool["function"]["name"] for tool in request["tools"]]
        self.assertIn("download_paper", tool_names)
        self.assertIn("name: paper-downloader", request["messages"][0]["content"])

    def test_visual_skill_without_pdf_can_download_before_extraction(self):
        request = _upstream_request(
            {
                "messages": [{"role": "user", "content": "Extract the main figure"}],
                "skill": "paper-visual-extractor",
            },
            self.config(),
            include_tools=True,
        )
        tool_names = [tool["function"]["name"] for tool in request["tools"]]
        self.assertIn("download_paper", tool_names)
        self.assertIn('<active_skill name="paper-visual-extractor">', request["messages"][0]["content"])

    def test_skill_references_are_loaded_by_mode(self):
        summary = _paper_reviewer_skill_text(
            [{"role": "user", "content": "Summarize this manuscript."}]
        )
        full_json = _paper_reviewer_skill_text(
            [{"role": "user", "content": "Write a full review as JSON."}]
        )
        self.assertNotIn("## Core dimensions", summary)
        self.assertNotIn("# Structured review output", summary)
        self.assertIn("## Core dimensions", full_json)
        self.assertIn("# Structured review output", full_json)

        story = _paper_reviewer_skill_text(
            [{"role": "user", "content": "Please explain it."}], "story-lecture"
        )
        self.assertIn("<selected_mode>story-lecture</selected_mode>", story)
        self.assertIn("expert professor", story)
        self.assertNotIn("## Core dimensions", story)

        visual = _paper_reviewer_skill_text(
            [{"role": "user", "content": "请提取主机制图和实验结果图表"}], "summary"
        )
        self.assertIn('composed_skill name="paper-visual-extractor"', visual)

    def test_invalid_explicit_mode_is_rejected(self):
        with self.assertRaises(HTTPException) as caught:
            _paper_reviewer_skill_text([], "invented-mode")
        self.assertEqual(422, caught.exception.status_code)

    def test_context_budget_keeps_recent_ancestor_turns_and_indexes_omissions(self):
        messages = [{"role": "system", "content": "root instructions"}]
        for index in range(8):
            messages.extend(
                [
                    {"role": "user", "content": f"ancestor question {index}"},
                    {"role": "assistant", "content": f"answer {index} " + "x" * 4000},
                ]
            )
        bounded, stats = _bounded_conversation_messages(
            messages,
            ContextConfig(
                max_chars=12_000,
                recent_turns=2,
                max_message_chars=3_000,
                outline_chars=1_000,
            ),
        )
        joined = "\n".join(message["content"] for message in bounded)
        self.assertGreater(stats["omitted_turns"], 0)
        self.assertIn("ancestor question 7", joined)
        self.assertIn("topic index, not a summary", joined)
        self.assertLessEqual(stats["estimated_chars"], stats["budget_chars"])

    def test_context_metadata_is_not_forwarded_to_model_gateway(self):
        request = _upstream_request(
            {
                "messages": [{"role": "user", "content": "hello"}],
                "context_meta": {
                    "strategy": "ancestor-path-budget-v1",
                    "total_turns": 1,
                    "included_turns": 1,
                    "omitted_turns": 0,
                    "truncated_messages": 0,
                },
            },
            self.config(),
        )
        self.assertNotIn("context_meta", request)


if __name__ == "__main__":
    unittest.main()
