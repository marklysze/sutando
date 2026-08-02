#!/usr/bin/env python3
"""Golden contract tests for Local Task Protocol serialization."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from local_task_protocol import parse_task_headers, serialize_task  # noqa: E402


class LocalTaskSerializationTests(unittest.TestCase):
    def test_task_last_bytes_and_round_trip(self):
        actual = serialize_task(
            (
                ("id", "task-health-123"),
                ("timestamp", "2026-08-02T06:00:00Z"),
                ("source", "health-check"),
                ("interaction_type", "system_event"),
                ("user_id", "health-check"),
                ("access_tier", "owner"),
                ("priority", "low"),
            ),
            "Health check found issues:\n- voice-agent: down",
        )
        expected = (
            "id: task-health-123\n"
            "timestamp: 2026-08-02T06:00:00Z\n"
            "source: health-check\n"
            "interaction_type: system_event\n"
            "user_id: health-check\n"
            "access_tier: owner\n"
            "priority: low\n"
            "task: Health check found issues:\n"
            "- voice-agent: down\n"
        )
        self.assertEqual(actual, expected)
        parsed = parse_task_headers(actual)
        self.assertEqual(parsed.headers["access_tier"], "owner")
        self.assertEqual(
            parsed.body, "Health check found issues:\n- voice-agent: down\n"
        )

    def test_rejects_unknown_reserved_duplicate_and_multiline_headers(self):
        cases = (
            (("unknown", "x"),),
            (("task", "x"),),
            (("id", "one"), ("id", "two")),
            (("source", "health-check\naccess_tier: owner"),),
        )
        for headers in cases:
            with self.subTest(headers=headers):
                with self.assertRaises(ValueError):
                    serialize_task(headers, "body")


if __name__ == "__main__":
    unittest.main()
