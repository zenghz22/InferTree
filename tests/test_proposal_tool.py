"""P4：agent 修改提案工具。

门槛：模型输出与迟到响应都不能绕过用户确认修改正文；失败不丢人工草稿。
"""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.agent import _execute_agent_tool, _upstream_request
from backend.config import AgentConfig, ContextConfig, ModelConfig, TavilyConfig
from backend.main import app


PAPER_A = "a" * 64
WORKSPACE = "default"


def tree_state():
    return {
        "tree": {
            "rootId": "root",
            "selectedId": "root",
            "nodes": {
                "root": {
                    "id": "root",
                    "parentId": None,
                    "query": None,
                    "content": "system",
                    "title": "Start",
                    "children": [],
                }
            },
        }
    }


def agent_config():
    return AgentConfig(
        name="Test Agent",
        instructions="Agent instructions",
        model=ModelConfig(
            base_url="https://example.test/v1",
            name="server-model",
            api_key="secret",
            connect_timeout_seconds=0.2,
            read_timeout_seconds=0.2,
            use_system_proxy=False,
        ),
        tavily=TavilyConfig(enabled=False, api_key="", max_results=5),
        context=ContextConfig(),
    )


class ProposalToolTests(unittest.TestCase):
    def _seed(self, client, markdown="Original body."):
        response = client.put(
            f"/api/workspaces/{WORKSPACE}",
            json={"baseRevision": 0, "state": tree_state()},
        )
        revision = response.json()["revision"]
        response = client.post(
            f"/api/workspaces/{WORKSPACE}/operations",
            json={
                "operationId": "op-doc",
                "baseRevision": revision,
                "op": "document.create",
                "title": "Notes",
            },
        )
        document_id = response.json()["result"]["documentId"]
        revision = response.json()["committed_revision"]
        response = client.post(
            f"/api/workspaces/{WORKSPACE}/operations",
            json={
                "operationId": "op-blk",
                "baseRevision": revision,
                "op": "block.add",
                "documentId": document_id,
                "block": {"type": "markdown", "title": "A note", "markdown": markdown},
            },
        )
        block_id = response.json()["result"]["blockId"]
        revision = response.json()["committed_revision"]
        return document_id, block_id, revision

    def _block(self, client, document_id, block_id):
        state = client.get(f"/api/workspaces/{WORKSPACE}").json()["state"]
        return state["documents"][document_id]["blocks"][block_id], state

    def _scope(self, document_id, block_id, mode="propose_edit"):
        return {
            "workspaceId": WORKSPACE,
            "documentId": document_id,
            "blockId": block_id,
            "mode": mode,
        }

    def _run_tool(self, payload, arguments, baseline=None):
        return asyncio.run(
            _execute_agent_tool(
                "propose_document_patch",
                arguments,
                payload,
                agent_config(),
                document_baseline=baseline,
            )
        )

    # ---- 工具只在正确的作用域下存在 ---------------------------------------

    def test_tool_is_offered_only_for_propose_edit_with_block(self):
        with tempfile.TemporaryDirectory() as temporary, patch(
            "backend.workspace.WORKSPACE_ROOT", Path(temporary)
        ):
            client = TestClient(app)
            document_id, block_id, _ = self._seed(client)

            offered = _upstream_request(
                {
                    "messages": [{"role": "user", "content": "改一下"}],
                    "document_scope": self._scope(document_id, block_id),
                },
                agent_config(),
                include_tools=True,
            )
            names = [tool["function"]["name"] for tool in offered.get("tools", [])]
            self.assertIn("propose_document_patch", names)

            followup = _upstream_request(
                {
                    "messages": [{"role": "user", "content": "这是什么"}],
                    "document_scope": self._scope(document_id, block_id, "followup"),
                },
                agent_config(),
                include_tools=True,
            )
            names = [
                tool["function"]["name"] for tool in followup.get("tools", [])
            ]
            self.assertNotIn("propose_document_patch", names)

    def test_editing_instructions_appear_in_context(self):
        with tempfile.TemporaryDirectory() as temporary, patch(
            "backend.workspace.WORKSPACE_ROOT", Path(temporary)
        ):
            client = TestClient(app)
            document_id, block_id, _ = self._seed(client)
            request = _upstream_request(
                {
                    "messages": [{"role": "user", "content": "改一下"}],
                    "document_scope": self._scope(document_id, block_id),
                },
                agent_config(),
            )
            system = request["messages"][0]["content"]
            self.assertIn("<editing_instructions>", system)
            self.assertIn("never claim the document was updated", system)

    # ---- 提案不修改正文 ---------------------------------------------------

    def test_tool_creates_pending_proposal_without_touching_body(self):
        with tempfile.TemporaryDirectory() as temporary, patch(
            "backend.workspace.WORKSPACE_ROOT", Path(temporary)
        ):
            client = TestClient(app)
            document_id, block_id, _ = self._seed(client)
            block, _ = self._block(client, document_id, block_id)
            baseline = {
                "block_revision": block["revision"],
                "block_content_hash": block["contentHash"],
            }

            outcome = self._run_tool(
                {
                    "document_scope": self._scope(document_id, block_id),
                },
                {
                    "replacement_markdown": "New body.",
                    "explanation": "Tightened the wording.",
                },
                baseline,
            )

            self.assertEqual("completed", outcome["state"])
            self.assertIn("提出修改", outcome["message"])
            payload = json.loads(outcome["content"])
            self.assertFalse(payload["document_changed"])
            self.assertEqual("pending", payload["status"])

            block_after, state = self._block(client, document_id, block_id)
            self.assertEqual("Original body.", block_after["markdown"])
            self.assertEqual(block["revision"], block_after["revision"])
            self.assertEqual("human", block_after["updatedBy"])
            proposals = state["proposals"]
            self.assertEqual(1, len(proposals))
            proposal = next(iter(proposals.values()))
            self.assertEqual("pending", proposal["status"])
            self.assertEqual(block["contentHash"], proposal["baseContentHash"])
            self.assertEqual("New body.", proposal["replacementMarkdown"])

    def test_repeated_identical_tool_call_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary, patch(
            "backend.workspace.WORKSPACE_ROOT", Path(temporary)
        ):
            client = TestClient(app)
            document_id, block_id, _ = self._seed(client)
            block, _ = self._block(client, document_id, block_id)
            baseline = {
                "block_revision": block["revision"],
                "block_content_hash": block["contentHash"],
            }
            payload = {"document_scope": self._scope(document_id, block_id)}
            arguments = {
                "replacement_markdown": "New body.",
                "explanation": "Same change.",
            }

            first = self._run_tool(payload, arguments, baseline)
            second = self._run_tool(payload, arguments, baseline)

            self.assertEqual("completed", first["state"])
            self.assertEqual("completed", second["state"])
            self.assertEqual(
                json.loads(first["content"])["proposal_id"],
                json.loads(second["content"])["proposal_id"],
            )
            self.assertTrue(second["detail"]["duplicate"])
            _, state = self._block(client, document_id, block_id)
            self.assertEqual(1, len(state["proposals"]))

    # ---- 迟到响应 ---------------------------------------------------------

    def test_late_response_cannot_propose_over_a_newer_edit(self):
        with tempfile.TemporaryDirectory() as temporary, patch(
            "backend.workspace.WORKSPACE_ROOT", Path(temporary)
        ):
            client = TestClient(app)
            document_id, block_id, revision = self._seed(client)
            block, _ = self._block(client, document_id, block_id)
            baseline = {
                "block_revision": block["revision"],
                "block_content_hash": block["contentHash"],
            }

            # 用户在模型作答期间改了正文
            response = client.post(
                f"/api/workspaces/{WORKSPACE}/operations",
                json={
                    "operationId": "op-human",
                    "baseRevision": revision,
                    "op": "block.update",
                    "documentId": document_id,
                    "blockId": block_id,
                    "baseBlockRevision": block["revision"],
                    "baseContentHash": block["contentHash"],
                    "markdown": "User typed this first.",
                },
            )
            self.assertEqual(200, response.status_code)

            outcome = self._run_tool(
                {"document_scope": self._scope(document_id, block_id)},
                {
                    "replacement_markdown": "Model body.",
                    "explanation": "Based on the old body.",
                },
                baseline,
            )

            self.assertEqual("failed", outcome["state"])
            block_after, state = self._block(client, document_id, block_id)
            self.assertEqual("User typed this first.", block_after["markdown"])
            self.assertEqual({}, state["proposals"])

    def test_tool_rejects_followup_scope(self):
        with tempfile.TemporaryDirectory() as temporary, patch(
            "backend.workspace.WORKSPACE_ROOT", Path(temporary)
        ):
            client = TestClient(app)
            document_id, block_id, _ = self._seed(client)
            outcome = self._run_tool(
                {"document_scope": self._scope(document_id, block_id, "followup")},
                {"replacement_markdown": "x", "explanation": "y"},
            )
            self.assertEqual("failed", outcome["state"])

    def test_tool_rejects_empty_replacement(self):
        with tempfile.TemporaryDirectory() as temporary, patch(
            "backend.workspace.WORKSPACE_ROOT", Path(temporary)
        ):
            client = TestClient(app)
            document_id, block_id, _ = self._seed(client)
            outcome = self._run_tool(
                {"document_scope": self._scope(document_id, block_id)},
                {"replacement_markdown": "   ", "explanation": "y"},
            )
            self.assertEqual("failed", outcome["state"])
            _, state = self._block(client, document_id, block_id)
            self.assertEqual({}, state["proposals"])


class ProposalBaselineTests(unittest.TestCase):
    def test_create_rejects_stale_expected_hash(self):
        with tempfile.TemporaryDirectory() as temporary, patch(
            "backend.workspace.WORKSPACE_ROOT", Path(temporary)
        ):
            client = TestClient(app)
            response = client.put(
                f"/api/workspaces/{WORKSPACE}",
                json={"baseRevision": 0, "state": tree_state()},
            )
            revision = response.json()["revision"]
            response = client.post(
                f"/api/workspaces/{WORKSPACE}/operations",
                json={
                    "operationId": "op-doc",
                    "baseRevision": revision,
                    "op": "document.create",
                    "title": "Notes",
                },
            )
            document_id = response.json()["result"]["documentId"]
            revision = response.json()["committed_revision"]
            response = client.post(
                f"/api/workspaces/{WORKSPACE}/operations",
                json={
                    "operationId": "op-blk",
                    "baseRevision": revision,
                    "op": "block.add",
                    "documentId": document_id,
                    "block": {"type": "markdown", "title": "A", "markdown": "b"},
                },
            )
            block_id = response.json()["result"]["blockId"]
            revision = response.json()["committed_revision"]

            response = client.post(
                f"/api/workspaces/{WORKSPACE}/operations",
                json={
                    "operationId": "op-prop",
                    "baseRevision": revision,
                    "op": "proposal.create",
                    "documentId": document_id,
                    "blockId": block_id,
                    "replacementMarkdown": "x",
                    "explanation": "y",
                    "expectedBaseContentHash": "0" * 64,
                },
            )
            self.assertEqual(409, response.status_code)
            state = client.get(f"/api/workspaces/{WORKSPACE}").json()["state"]
            self.assertEqual({}, state["proposals"])


if __name__ == "__main__":
    unittest.main()
