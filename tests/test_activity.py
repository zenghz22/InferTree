import unittest

from backend.activity import ActivitySpan, SCHEMA


class ActivityTests(unittest.TestCase):
    def test_span_events_keep_identity_and_add_duration(self):
        span = ActivitySpan(
            kind="tool",
            action="web.search",
            title="联网搜索",
            parent_id="act_parent",
            detail={"query": "paper"},
        )
        started = span.event("started", "正在搜索")
        completed = span.event("completed", "搜索完成", {"result_count": 3})

        self.assertEqual(SCHEMA, started["schema"])
        self.assertEqual(started["id"], completed["id"])
        self.assertEqual("act_parent", completed["parent_id"])
        self.assertEqual("paper", completed["detail"]["query"])
        self.assertEqual(3, completed["detail"]["result_count"])
        self.assertNotIn("duration_ms", started)
        self.assertGreaterEqual(completed["duration_ms"], 0)

    def test_rejects_unknown_state(self):
        with self.assertRaises(ValueError):
            ActivitySpan(kind="run", action="agent.run", title="run").event("thinking")


if __name__ == "__main__":
    unittest.main()
