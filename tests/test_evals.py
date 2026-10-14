import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.config import AgentConfig, ModelConfig, TavilyConfig
from backend.evals import grade_eval_sample, list_eval_suites
from backend.main import app


def test_config():
    return AgentConfig(
        name="Eval Test Agent",
        instructions="test",
        model=ModelConfig(
            base_url="https://example.test/v1",
            name="test-model",
            api_key="secret-not-for-results",
            connect_timeout_seconds=1,
            read_timeout_seconds=1,
            use_system_proxy=False,
        ),
        tavily=TavilyConfig(enabled=False, api_key="", max_results=5),
    )


class EvalTests(unittest.TestCase):
    def test_builtin_suites_include_fixture_and_agent_modes(self):
        suites = list_eval_suites()["suites"]
        modes = {suite["id"]: suite["mode"] for suite in suites}
        self.assertEqual("fixture", modes["grader-smoke"])
        self.assertEqual("agent", modes["agent-core"])

    def test_weighted_graders_score_visible_output_and_tool_trace(self):
        case = {
            "pass_threshold": 0.75,
            "graders": [
                {"type": "contains", "value": "42", "weight": 2},
                {"type": "tool_called", "value": "web.search", "weight": 1},
                {"type": "not_contains", "value": "secret", "weight": 1},
            ],
        }
        sample = {
            "output": "The answer is 42.",
            "error": None,
            "activities": [
                {"action": "web.search", "state": "completed"}
            ],
            "artifacts": [],
            "latency_ms": 10,
        }
        result = grade_eval_sample(case, sample)
        self.assertEqual(1.0, result["score"])
        self.assertTrue(result["passed"])

    def test_fixture_run_completes_without_model_and_persists_results(self):
        with tempfile.TemporaryDirectory() as temporary, patch(
            "backend.evals.RUNS_ROOT", Path(temporary)
        ), patch("backend.main.load_config", return_value=test_config()):
            client = TestClient(app)
            started = client.post(
                "/api/evals/runs",
                json={"suite_id": "grader-smoke", "repeat": 1},
            )
            self.assertEqual(200, started.status_code)
            run_id = started.json()["id"]
            result = None
            for _ in range(40):
                response = client.get(f"/api/evals/runs/{run_id}")
                result = response.json()
                if result["status"] not in {"queued", "running"}:
                    break
                time.sleep(0.025)

            self.assertIsNotNone(result)
            self.assertEqual("completed", result["status"])
            self.assertEqual(3, result["completed_samples"])
            self.assertEqual(1.0, result["summary"]["overall"]["pass_rate"])
            self.assertNotIn("secret-not-for-results", response.text)
            self.assertTrue((Path(temporary) / f"{run_id}.json").exists())

    def test_eval_run_rejects_unsafe_suite_identifier(self):
        response = TestClient(app).get("/api/evals/suites/../secret")
        self.assertIn(response.status_code, {404, 422})


if __name__ == "__main__":
    unittest.main()
