import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.main import app


def workspace_state():
    return {
        "tree": {
            "rootId": "root",
            "selectedId": "child",
            "nodes": {
                "root": {
                    "id": "root",
                    "parentId": None,
                    "query": None,
                    "content": "system",
                    "title": "Start",
                    "children": ["child"],
                },
                "child": {
                    "id": "child",
                    "parentId": "root",
                    "query": "question",
                    "content": "answer",
                    "title": "Question",
                    "children": [],
                    "paperId": "a" * 64,
                    "paper": {
                        "paperId": "a" * 64,
                        "filename": "paper.pdf",
                        "title": "Paper",
                        "pageCount": 2,
                    },
                },
            },
        },
        "active_paper": None,
    }


class WorkspaceTests(unittest.TestCase):
    def test_missing_workspace_is_an_empty_resource(self):
        with tempfile.TemporaryDirectory() as temporary, patch(
            "backend.workspace.WORKSPACE_ROOT", Path(temporary)
        ):
            response = TestClient(app).get("/api/workspaces/default")
        self.assertEqual(200, response.status_code)
        self.assertFalse(response.json()["exists"])
        self.assertIsNone(response.json()["state"])

    def test_workspace_round_trip_is_atomic_and_revisioned(self):
        with tempfile.TemporaryDirectory() as temporary, patch(
            "backend.workspace.WORKSPACE_ROOT", Path(temporary)
        ):
            client = TestClient(app)
            first = client.put("/api/workspaces/default", json={"baseRevision": 0, "state": workspace_state()})
            second = client.put("/api/workspaces/default", json={"baseRevision": 1, "state": workspace_state()})
            loaded = client.get("/api/workspaces/default")
            papers = client.get("/api/workspaces/default/papers")
            workspace_dir = Path(temporary) / "default"
            files = sorted(item.name for item in workspace_dir.iterdir())

        self.assertEqual(200, first.status_code)
        self.assertEqual(2, second.json()["revision"])
        self.assertEqual("question", loaded.json()["state"]["tree"]["nodes"]["child"]["query"])
        self.assertEqual("Paper", loaded.json()["state"]["tree"]["nodes"]["child"]["paper"]["title"])
        self.assertEqual(["artifacts", "papers.json", "workspace.json"], files)
        self.assertEqual(1, papers.json()["paper_count"])
        self.assertEqual(["child"], papers.json()["papers"][0]["nodeIds"])
        self.assertEqual("Paper", papers.json()["papers"][0]["title"])

    def test_legacy_flat_workspace_is_migrated_on_load(self):
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
                        "state": workspace_state(),
                    }
                ),
                encoding="utf-8",
            )
            response = TestClient(app).get("/api/workspaces/default")
            workspace_dir = Path(temporary) / "default"

            self.assertEqual(200, response.status_code)
            self.assertEqual(7, response.json()["revision"])
            self.assertFalse(legacy.exists())
            self.assertTrue((workspace_dir / "workspace.json").exists())
            self.assertTrue((workspace_dir / "papers.json").exists())
            self.assertTrue((workspace_dir / "artifacts").is_dir())

    def test_workspace_rejects_parent_child_mismatch(self):
        state = workspace_state()
        state["tree"]["nodes"]["child"]["parentId"] = None
        with tempfile.TemporaryDirectory() as temporary, patch(
            "backend.workspace.WORKSPACE_ROOT", Path(temporary)
        ):
            response = TestClient(app).put(
                "/api/workspaces/default", json={"baseRevision": 0, "state": state}
            )
        self.assertEqual(422, response.status_code)

    def test_workspace_id_is_restricted_to_safe_characters(self):
        response = TestClient(app).get("/api/workspaces/INVALID.NAME")
        self.assertEqual(422, response.status_code)

    def test_workspace_rejects_inconsistent_node_paper_metadata(self):
        state = workspace_state()
        state["tree"]["nodes"]["child"]["paper"]["paperId"] = "b" * 64
        with tempfile.TemporaryDirectory() as temporary, patch(
            "backend.workspace.WORKSPACE_ROOT", Path(temporary)
        ):
            response = TestClient(app).put(
                "/api/workspaces/default", json={"baseRevision": 0, "state": state}
            )
        self.assertEqual(422, response.status_code)


if __name__ == "__main__":
    unittest.main()
