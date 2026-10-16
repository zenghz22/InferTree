import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.main import app


PAPER_A = "a" * 64
PAPER_B = "b" * 64
PAPER_C = "c" * 64


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
                },
            },
        },
        "active_paper": None,
    }


def put_tree(client, base_revision, state=None):
    return client.put(
        "/api/workspaces/default",
        json={"baseRevision": base_revision, "state": state or tree_state()},
    )


def op(client, operation_id, base_revision, name, **params):
    return client.post(
        "/api/workspaces/default/operations",
        json={
            "operationId": operation_id,
            "baseRevision": base_revision,
            "op": name,
            **params,
        },
    )


def result_of(response):
    return response.json()["result"]


class WorkspaceV2Tests(unittest.TestCase):
    def _setup(self, client):
        response = put_tree(client, 0)
        self.assertEqual(200, response.status_code)
        return response.json()["revision"]

    def _create_doc(self, client, rev, operation_id="op-doc", title="Notes"):
        response = op(client, operation_id, rev, "document.create", title=title)
        self.assertEqual(200, response.status_code)
        return result_of(response)["documentId"], response.json()["committed_revision"]

    def _create_markdown_block(self, client, rev, document_id, operation_id="op-blk", **extra):
        block = {
            "type": "markdown",
            "title": "A note",
            "markdown": "Some **markdown** content.",
        }
        block.update(extra)
        response = op(client, operation_id, rev, "block.add", documentId=document_id, block=block)
        self.assertEqual(200, response.status_code)
        return result_of(response)["blockId"], result_of(response)["blockRevision"], response.json()["committed_revision"]

    def test_v1_to_v2_migration_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary, patch(
            "backend.workspace.WORKSPACE_ROOT", Path(temporary)
        ):
            legacy = Path(temporary) / "default.json"
            legacy.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "workspace_id": "default",
                        "revision": 7,
                        "updated_at": "2026-08-29T00:00:00+00:00",
                        "state": tree_state(),
                    }
                ),
                encoding="utf-8",
            )
            client = TestClient(app)
            first = client.get("/api/workspaces/default")
            second = client.get("/api/workspaces/default")

            self.assertEqual(200, first.status_code)
            self.assertEqual(2, first.json()["schema_version"])
            state = first.json()["state"]
            self.assertEqual({}, state["documents"])
            self.assertEqual({}, state["evidence"])
            self.assertEqual({}, state["proposals"])
            self.assertEqual({}, state["dedup"])
            self.assertEqual("root", state["tree"]["rootId"])
            # Idempotent: same migrated state across loads.
            self.assertEqual(first.json()["state"], second.json()["state"])
            # On-disk file is not rewritten until a save happens.
            disk = json.loads((Path(temporary) / "default" / "workspace.json").read_text(encoding="utf-8"))
            self.assertEqual(1, disk["schema_version"])

    def test_full_round_trip_preserves_document_and_stable_hash(self):
        with tempfile.TemporaryDirectory() as temporary, patch(
            "backend.workspace.WORKSPACE_ROOT", Path(temporary)
        ):
            client = TestClient(app)
            rev = self._setup(client)
            ev = op(client, "op-ev", rev, "evidence.upsert", paperId=PAPER_A, sourceType="paper", locatorStatus="paper_only")
            self.assertEqual(200, ev.status_code)
            rev = ev.json()["committed_revision"]
            document_id, rev = self._create_doc(client, rev)
            block_id, _, rev = self._create_markdown_block(
                client, rev, document_id,
                evidenceIds=[result_of(ev)["evidenceId"]],
                sourceRefs=[{"nodeId": "root", "contentHash": "x" * 64}],
            )

            loaded = client.get("/api/workspaces/default").json()
            document = loaded["state"]["documents"][document_id]
            block = document["blocks"][block_id]
            self.assertEqual("A note", block["title"])
            self.assertEqual([result_of(ev)["evidenceId"]], block["evidenceIds"])
            self.assertEqual([{"nodeId": "root", "contentHash": "x" * 64}], block["sourceRefs"])
            self.assertRegex(block["contentHash"], r"^[0-9a-f]{64}$")

            # Hash is stable across a second load.
            reloaded = client.get("/api/workspaces/default").json()
            self.assertEqual(block["contentHash"], reloaded["state"]["documents"][document_id]["blocks"][block_id]["contentHash"])

    def test_tree_put_does_not_lose_documents(self):
        with tempfile.TemporaryDirectory() as temporary, patch(
            "backend.workspace.WORKSPACE_ROOT", Path(temporary)
        ):
            client = TestClient(app)
            rev = self._setup(client)
            document_id, rev = self._create_doc(client, rev)
            _, _, rev = self._create_markdown_block(client, rev, document_id)

            changed = tree_state()
            changed["tree"]["nodes"]["root"]["children"] = ["child"]
            changed["tree"]["nodes"]["child"] = {
                "id": "child", "parentId": "root", "query": "q", "content": "a",
                "title": "Child", "children": [],
            }
            changed["tree"]["selectedId"] = "child"
            response = put_tree(client, rev, changed)
            self.assertEqual(200, response.status_code)

            loaded = client.get("/api/workspaces/default").json()
            self.assertEqual("child", loaded["state"]["tree"]["selectedId"])
            self.assertIn(document_id, loaded["state"]["documents"])
            self.assertEqual(1, len(loaded["state"]["documents"][document_id]["blocks"]))

    def test_stale_revision_is_rejected_for_tree_and_operation(self):
        with tempfile.TemporaryDirectory() as temporary, patch(
            "backend.workspace.WORKSPACE_ROOT", Path(temporary)
        ):
            client = TestClient(app)
            rev = self._setup(client)
            _, rev = self._create_doc(client, rev)

            stale_tree = put_tree(client, 1, tree_state())
            self.assertEqual(409, stale_tree.status_code)
            self.assertEqual(rev, stale_tree.json()["detail"]["current_revision"])

            stale_op = op(client, "op-x", 1, "document.create", title="Stale")
            self.assertEqual(409, stale_op.status_code)
            self.assertEqual(rev, stale_op.json()["detail"]["current_revision"])

    def test_duplicate_operation_is_not_reexecuted(self):
        with tempfile.TemporaryDirectory() as temporary, patch(
            "backend.workspace.WORKSPACE_ROOT", Path(temporary)
        ):
            client = TestClient(app)
            rev = self._setup(client)
            first = op(client, "op-doc", rev, "document.create", title="Notes")
            self.assertEqual(200, first.status_code)
            committed = first.json()["committed_revision"]

            # Retry with a stale baseRevision must be recognized as duplicate, not conflict.
            retry = op(client, "op-doc", rev, "document.create", title="Notes")
            self.assertEqual(200, retry.status_code)
            self.assertTrue(retry.json()["duplicate"])
            self.assertEqual(committed, retry.json()["committed_revision"])

            loaded = client.get("/api/workspaces/default").json()
            self.assertEqual(1, len(loaded["state"]["documents"]))

    def test_same_operation_id_with_different_content_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary, patch(
            "backend.workspace.WORKSPACE_ROOT", Path(temporary)
        ):
            client = TestClient(app)
            rev = self._setup(client)
            first = op(client, "op-doc", rev, "document.create", title="Notes")
            self.assertEqual(200, first.status_code)
            rev = first.json()["committed_revision"]

            conflict = op(client, "op-doc", rev, "document.create", title="Other")
            self.assertEqual(409, conflict.status_code)

    def test_failed_operation_preserves_previous_file(self):
        with tempfile.TemporaryDirectory() as temporary, patch(
            "backend.workspace.WORKSPACE_ROOT", Path(temporary)
        ):
            client = TestClient(app)
            rev = self._setup(client)
            document_id, rev = self._create_doc(client, rev)

            bad = op(client, "op-bad", rev, "block.add", documentId=document_id,
                     block={"type": "unknown", "title": "x", "markdown": "y"})
            self.assertEqual(422, bad.status_code)

            loaded = client.get("/api/workspaces/default").json()
            self.assertIn(document_id, loaded["state"]["documents"])
            self.assertEqual(0, len(loaded["state"]["documents"][document_id]["blocks"]))

    def test_unknown_schema_version_is_rejected_without_downgrade(self):
        with tempfile.TemporaryDirectory() as temporary, patch(
            "backend.workspace.WORKSPACE_ROOT", Path(temporary)
        ):
            directory = Path(temporary) / "default"
            directory.mkdir(parents=True)
            workspace_path = directory / "workspace.json"
            workspace_path.write_text(
                json.dumps(
                    {
                        "schema_version": 99,
                        "workspace_id": "default",
                        "revision": 1,
                        "state": tree_state(),
                    }
                ),
                encoding="utf-8",
            )
            response = TestClient(app).get("/api/workspaces/default")
            self.assertEqual(413, response.status_code)
            disk = json.loads(workspace_path.read_text(encoding="utf-8"))
            self.assertEqual(99, disk["schema_version"])

    def test_document_only_paper_enters_index(self):
        with tempfile.TemporaryDirectory() as temporary, patch(
            "backend.workspace.WORKSPACE_ROOT", Path(temporary)
        ):
            client = TestClient(app)
            rev = self._setup(client)
            ev = op(client, "op-ev", rev, "evidence.upsert", paperId=PAPER_C, sourceType="paper", locatorStatus="paper_only")
            self.assertEqual(200, ev.status_code)
            rev = ev.json()["committed_revision"]
            document_id, rev = self._create_doc(client, rev)
            _, _, _ = self._create_markdown_block(client, rev, document_id, evidenceIds=[result_of(ev)["evidenceId"]])

            papers = client.get("/api/workspaces/default/papers").json()
            self.assertEqual(1, papers["paper_count"])
            entry = papers["papers"][0]
            self.assertEqual(PAPER_C, entry["paperId"])
            self.assertEqual([], entry["nodeIds"])
            self.assertEqual([document_id], entry["documentIds"])

    def test_proposal_accept_conflicts_when_block_changed(self):
        with tempfile.TemporaryDirectory() as temporary, patch(
            "backend.workspace.WORKSPACE_ROOT", Path(temporary)
        ):
            client = TestClient(app)
            rev = self._setup(client)
            document_id, rev = self._create_doc(client, rev)
            block_id, block_rev, rev = self._create_markdown_block(client, rev, document_id)

            proposal = op(client, "op-prop", rev, "proposal.create",
                          documentId=document_id, blockId=block_id,
                          replacementMarkdown="Proposed text.", explanation="fix")
            self.assertEqual(200, proposal.status_code)
            proposal_id = result_of(proposal)["proposalId"]
            rev = proposal.json()["committed_revision"]

            loaded = client.get("/api/workspaces/default").json()
            block = loaded["state"]["documents"][document_id]["blocks"][block_id]

            update = op(client, "op-upd", rev, "block.update",
                        documentId=document_id, blockId=block_id,
                        baseBlockRevision=block_rev, baseContentHash=block["contentHash"],
                        markdown="User edited it.")
            self.assertEqual(200, update.status_code)
            rev = update.json()["committed_revision"]

            accept = op(client, "op-acc", rev, "proposal.accept", proposalId=proposal_id)
            self.assertEqual(409, accept.status_code)
            self.assertEqual("conflicted", accept.json()["detail"]["proposal_status"])

            loaded = client.get("/api/workspaces/default").json()
            self.assertEqual("conflicted", loaded["state"]["proposals"][proposal_id]["status"])
            self.assertEqual("User edited it.", loaded["state"]["documents"][document_id]["blocks"][block_id]["markdown"])

    def test_proposal_reject_does_not_require_baseline_match(self):
        with tempfile.TemporaryDirectory() as temporary, patch(
            "backend.workspace.WORKSPACE_ROOT", Path(temporary)
        ):
            client = TestClient(app)
            rev = self._setup(client)
            document_id, rev = self._create_doc(client, rev)
            block_id, block_rev, rev = self._create_markdown_block(client, rev, document_id)

            proposal = op(client, "op-prop", rev, "proposal.create",
                          documentId=document_id, blockId=block_id,
                          replacementMarkdown="Proposed text.", explanation="fix")
            proposal_id = result_of(proposal)["proposalId"]
            rev = proposal.json()["committed_revision"]

            loaded = client.get("/api/workspaces/default").json()
            block = loaded["state"]["documents"][document_id]["blocks"][block_id]
            update = op(client, "op-upd", rev, "block.update",
                        documentId=document_id, blockId=block_id,
                        baseBlockRevision=block_rev, baseContentHash=block["contentHash"],
                        markdown="User edited it.")
            rev = update.json()["committed_revision"]

            reject = op(client, "op-rej", rev, "proposal.reject", proposalId=proposal_id)
            self.assertEqual(200, reject.status_code)
            self.assertEqual("rejected", result_of(reject)["status"])

    def test_proposal_accept_applies_replacement(self):
        with tempfile.TemporaryDirectory() as temporary, patch(
            "backend.workspace.WORKSPACE_ROOT", Path(temporary)
        ):
            client = TestClient(app)
            rev = self._setup(client)
            document_id, rev = self._create_doc(client, rev)
            block_id, _, rev = self._create_markdown_block(client, rev, document_id)

            proposal = op(client, "op-prop", rev, "proposal.create",
                          documentId=document_id, blockId=block_id,
                          replacementMarkdown="Agent rewrote this.", explanation="improve")
            proposal_id = result_of(proposal)["proposalId"]
            rev = proposal.json()["committed_revision"]

            accept = op(client, "op-acc", rev, "proposal.accept", proposalId=proposal_id)
            self.assertEqual(200, accept.status_code)
            self.assertEqual("accepted", result_of(accept)["status"])

            loaded = client.get("/api/workspaces/default").json()
            block = loaded["state"]["documents"][document_id]["blocks"][block_id]
            self.assertEqual("Agent rewrote this.", block["markdown"])
            self.assertEqual("agent", block["updatedBy"])
            self.assertEqual("accepted", loaded["state"]["proposals"][proposal_id]["status"])

    def test_block_update_cannot_change_asset_ref(self):
        with tempfile.TemporaryDirectory() as temporary, patch(
            "backend.workspace.WORKSPACE_ROOT", Path(temporary)
        ):
            client = TestClient(app)
            rev = self._setup(client)
            document_id, rev = self._create_doc(client, rev)
            figure = op(client, "op-fig", rev, "block.add", documentId=document_id, block={
                "type": "figure", "title": "Fig", "markdown": "caption",
                "assetRef": {"paperId": PAPER_A, "visualId": "p1-figure-1"},
            })
            self.assertEqual(200, figure.status_code)
            block_id = result_of(figure)["blockId"]
            rev = figure.json()["committed_revision"]

            loaded = client.get("/api/workspaces/default").json()
            block = loaded["state"]["documents"][document_id]["blocks"][block_id]
            bad = op(client, "op-bad", rev, "block.update",
                     documentId=document_id, blockId=block_id,
                     baseBlockRevision=block["revision"], baseContentHash=block["contentHash"],
                     assetRef={"paperId": PAPER_A, "visualId": "p2-figure-9"})
            self.assertEqual(422, bad.status_code)

    def test_block_move_reorders(self):
        with tempfile.TemporaryDirectory() as temporary, patch(
            "backend.workspace.WORKSPACE_ROOT", Path(temporary)
        ):
            client = TestClient(app)
            rev = self._setup(client)
            document_id, rev = self._create_doc(client, rev)
            a_id, _, rev = self._create_markdown_block(client, rev, document_id, operation_id="op-a")
            b_id, _, rev = self._create_markdown_block(client, rev, document_id, operation_id="op-b")

            move = op(client, "op-move", rev, "block.move", documentId=document_id, blockId=b_id, index=0)
            self.assertEqual(200, move.status_code)

            loaded = client.get("/api/workspaces/default").json()
            self.assertEqual([b_id, a_id], loaded["state"]["documents"][document_id]["blockOrder"])

    def test_block_remove_cleans_pending_proposals(self):
        with tempfile.TemporaryDirectory() as temporary, patch(
            "backend.workspace.WORKSPACE_ROOT", Path(temporary)
        ):
            client = TestClient(app)
            rev = self._setup(client)
            document_id, rev = self._create_doc(client, rev)
            block_id, _, rev = self._create_markdown_block(client, rev, document_id)

            proposal = op(client, "op-prop", rev, "proposal.create",
                          documentId=document_id, blockId=block_id,
                          replacementMarkdown="Proposed.", explanation="fix")
            proposal_id = result_of(proposal)["proposalId"]
            rev = proposal.json()["committed_revision"]

            remove = op(client, "op-rm", rev, "block.remove", documentId=document_id, blockId=block_id)
            self.assertEqual(200, remove.status_code)

            loaded = client.get("/api/workspaces/default").json()
            self.assertNotIn(block_id, loaded["state"]["documents"][document_id]["blocks"])
            self.assertEqual("rejected", loaded["state"]["proposals"][proposal_id]["status"])

    def test_tree_put_with_document_fields_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary, patch(
            "backend.workspace.WORKSPACE_ROOT", Path(temporary)
        ):
            client = TestClient(app)
            self._setup(client)
            state = tree_state()
            state["documents"] = {}
            response = put_tree(client, 1, state)
            self.assertEqual(422, response.status_code)

    def test_operation_without_tree_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary, patch(
            "backend.workspace.WORKSPACE_ROOT", Path(temporary)
        ):
            client = TestClient(app)
            response = op(client, "op-doc", 0, "document.create", title="Notes")
            self.assertEqual(422, response.status_code)


if __name__ == "__main__":
    unittest.main()
