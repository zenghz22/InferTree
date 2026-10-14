import tempfile
import unittest
from pathlib import Path

from backend.config import (
    DEFAULT_UI_THEME,
    context_policy,
    load_config,
    model_catalog,
    resolve_model,
    ui_theme,
)


class ConfigTests(unittest.TestCase):
    def test_ui_theme_loads_valid_colors_and_falls_back_per_invalid_field(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(
                """
[model]
base_url = "https://example.test/v1"
api_key = "secret"
[ui.theme]
background = "#e2f5ff"
panel = "#BAE3FB"
text = "#08276B"
border = "not-a-color"
accent = "#E5D2FE"
[tools.tavily]
enabled = false
""".strip(),
                encoding="utf-8",
            )
            theme = ui_theme(load_config(path))
            self.assertEqual("#E2F5FF", theme["background"])
            self.assertEqual("#E5D2FE", theme["accent"])
            self.assertEqual(DEFAULT_UI_THEME["border"], theme["border"])

    def test_builtin_catalog_uses_gui_default_and_never_exposes_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(
                """
[agent]
name = "test"
[model]
base_url = "https://example.test/v1"
api_key = "top-secret"
default = "deepseek-v4-pro-0813"
[tools.tavily]
enabled = false
api_key = ""
max_results = 5
""".strip(),
                encoding="utf-8",
            )
            config = load_config(path)
            self.assertEqual("DeepSeek-V4-Pro-0813", resolve_model(config).connection.name)
            catalog = model_catalog(config)
            self.assertEqual("deepseek-v4-pro-0813", catalog["default_model_id"])
            self.assertEqual(6, len(catalog["models"]))
            self.assertNotIn("top-secret", repr(catalog))
            self.assertNotIn("base_url", repr(catalog))

    def test_profile_can_override_gateway_model_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(
                """
[model]
base_url = "https://example.test/v1"
api_key = "secret"
default = "qwen3-8-max"
[models.qwen3-8-max]
name = "qwen3.8-max"
[tools.tavily]
enabled = false
""".strip(),
                encoding="utf-8",
            )
            config = load_config(path)
            self.assertEqual("qwen3.8-max", resolve_model(config).connection.name)

    def test_rejects_non_positive_timeout(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(
                """
[agent]
name = "test"
[model]
base_url = "https://example.test/v1"
name = "model"
api_key = "secret"
connect_timeout_seconds = 0
read_timeout_seconds = 10
[tools.tavily]
enabled = false
api_key = ""
max_results = 5
""".strip(),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "greater than zero"):
                load_config(path)

    def test_context_policy_is_loaded_and_clamped_for_browser(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(
                """
[model]
base_url = "https://example.test/v1"
api_key = "secret"
[context]
max_chars = 70000
recent_turns = 5
max_message_chars = 14000
outline_chars = 5000
[tools.tavily]
enabled = false
""".strip(),
                encoding="utf-8",
            )
            policy = context_policy(load_config(path))
            self.assertEqual(70_000, policy["max_chars"])
            self.assertEqual(5, policy["recent_turns"])
            self.assertEqual(14_000, policy["max_message_chars"])


if __name__ == "__main__":
    unittest.main()
