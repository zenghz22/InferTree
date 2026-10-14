import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fitz
from fastapi.testclient import TestClient

from backend.main import app
from backend.config import AgentConfig, ModelConfig, ModelProfile, TavilyConfig


def sample_pdf() -> bytes:
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "A Reliable Test Paper")
    payload = document.tobytes()
    document.close()
    return payload


class PaperUploadTests(unittest.TestCase):
    def test_frontend_html_is_not_cached_during_local_development(self):
        response = TestClient(app).get("/")
        self.assertEqual(200, response.status_code)
        self.assertIn("no-store", response.headers["cache-control"])

    def test_health_advertises_activity_schema(self):
        with patch("backend.main.config_status", return_value={"configured": True}):
            response = TestClient(app).get("/api/health")
        self.assertEqual(200, response.status_code)
        self.assertEqual("infertree.activity.v1", response.json()["activity_schema"])

    def test_model_catalog_endpoint_is_browser_safe(self):
        profile = ModelProfile(
            "deepseek", "DeepSeek V4", "DeepSeek", "deepseek", "secret-upstream-name"
        )
        config = AgentConfig(
            name="test",
            instructions="test",
            model=ModelConfig(
                base_url="https://secret-endpoint.test/v1",
                name=profile.name,
                api_key="secret-key",
                connect_timeout_seconds=1,
                read_timeout_seconds=1,
                use_system_proxy=False,
            ),
            tavily=TavilyConfig(enabled=False, api_key="", max_results=5),
            default_model_id=profile.id,
            models={profile.id: profile},
        )
        with patch("backend.main.load_config", return_value=config):
            response = TestClient(app).get("/api/models")
        self.assertEqual(200, response.status_code)
        self.assertEqual("deepseek", response.json()["default_model_id"])
        body = response.text
        self.assertNotIn("secret-key", body)
        self.assertNotIn("secret-endpoint", body)
        self.assertNotIn("secret-upstream-name", body)

    def test_skill_catalog_endpoint_exposes_discovery_metadata(self):
        response = TestClient(app).get("/api/skills")
        self.assertEqual(200, response.status_code)
        names = {item["name"] for item in response.json()["skills"]}
        self.assertIn("paper-reviewer", names)
        self.assertNotIn("# Paper Reviewer", response.text)

    def test_ui_config_endpoint_exposes_only_theme_palette(self):
        profile = ModelProfile("model", "Model", "Vendor", "legacy", "secret-model")
        config = AgentConfig(
            name="test",
            instructions="test",
            model=ModelConfig(
                base_url="https://secret-endpoint.test",
                name=profile.name,
                api_key="secret-key",
                connect_timeout_seconds=1,
                read_timeout_seconds=1,
                use_system_proxy=False,
            ),
            tavily=TavilyConfig(enabled=False, api_key="tavily-secret", max_results=5),
            default_model_id=profile.id,
            models={profile.id: profile},
            ui_theme={
                "background": "#E2F5FF",
                "panel": "#BAE3FB",
                "text": "#08276B",
                "border": "#F0F791",
                "accent": "#E5D2FE",
            },
        )
        with patch("backend.main.load_config", return_value=config):
            response = TestClient(app).get("/api/ui-config")
        self.assertEqual(200, response.status_code)
        self.assertEqual("#E5D2FE", response.json()["theme"]["accent"])
        self.assertNotIn("secret", response.text)

    def test_upload_is_content_addressed_and_repeatable(self):
        with tempfile.TemporaryDirectory() as temporary:
            papers_root = Path(temporary) / "papers"
            with patch("backend.main.PAPERS_ROOT", papers_root):
                client = TestClient(app)
                files = {"file": ("paper.pdf", sample_pdf(), "application/pdf")}
                first = client.post("/api/papers", files=files)
                second = client.post("/api/papers", files=files)

                self.assertEqual(200, first.status_code)
                self.assertEqual(first.json()["paper_id"], second.json()["paper_id"])
                self.assertEqual(1, len(list(papers_root.glob("*.pdf"))))
                self.assertFalse(list(papers_root.glob(".upload-*.tmp")))

    def test_uploaded_pdf_can_be_read_inline(self):
        with tempfile.TemporaryDirectory() as temporary:
            papers_root = Path(temporary) / "papers"
            papers_root.mkdir()
            paper_id = "a" * 64
            payload = sample_pdf()
            (papers_root / f"{paper_id}.pdf").write_bytes(payload)
            with patch("backend.paper_visuals.PAPERS_ROOT", papers_root):
                response = TestClient(app).get(f"/api/papers/{paper_id}/file")

        self.assertEqual(200, response.status_code)
        self.assertEqual(payload, response.content)
        self.assertEqual("application/pdf", response.headers["content-type"])
        self.assertIn("inline", response.headers["content-disposition"])

    def test_uploaded_pdf_metadata_can_hydrate_legacy_nodes(self):
        with tempfile.TemporaryDirectory() as temporary:
            papers_root = Path(temporary) / "papers"
            papers_root.mkdir()
            paper_id = "b" * 64
            (papers_root / f"{paper_id}.pdf").write_bytes(sample_pdf())
            with patch("backend.paper_visuals.PAPERS_ROOT", papers_root):
                response = TestClient(app).get(f"/api/papers/{paper_id}/metadata")

        self.assertEqual(200, response.status_code)
        self.assertEqual(paper_id, response.json()["paper_id"])
        self.assertEqual("A Reliable Test Paper", response.json()["title"])
        self.assertEqual(1, response.json()["page_count"])

    def test_oversized_upload_fails_without_leaving_temporary_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            papers_root = Path(temporary) / "papers"
            with patch("backend.main.PAPERS_ROOT", papers_root), patch(
                "backend.main.MAX_UPLOAD_BYTES", 10
            ):
                response = TestClient(app).post(
                    "/api/papers",
                    files={"file": ("paper.pdf", b"%PDF-too-large", "application/pdf")},
                )
                self.assertEqual(413, response.status_code)
                self.assertFalse(list(papers_root.iterdir()))


if __name__ == "__main__":
    unittest.main()
