import json
import unittest
from unittest.mock import patch

import launcher


class FakeResponse:
    def __init__(self, payload, status=200):
        self.status = status
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class LauncherTests(unittest.TestCase):
    def test_health_check_requires_infertree_identity(self):
        with patch("launcher.urllib.request.urlopen", return_value=FakeResponse({"service": "other"})):
            self.assertFalse(launcher.server_is_ready())
        with patch(
            "launcher.urllib.request.urlopen",
            return_value=FakeResponse({"service": "infertree-agent"}),
        ):
            self.assertTrue(launcher.server_is_ready())


if __name__ == "__main__":
    unittest.main()
