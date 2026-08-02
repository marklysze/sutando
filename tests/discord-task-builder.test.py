#!/usr/bin/env python3
"""Exact-output tests for pure Discord task-envelope construction."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from discord_task import build_task_content  # noqa: E402


class DiscordTaskBuilderTests(unittest.TestCase):
    def test_collaborator_task_bytes_preserve_wire_tier_and_rulebook(self):
        actual = build_task_content(
            task_id="task-123",
            timestamp="2026-08-02T06:00:00Z",
            media_headers="content_modalities: text,image\nmedia_form: attachment\n",
            channel_id=111,
            channel_name="dev",
            guild_name="Sutando",
            source_message_id=222,
            parent_headers="parent_message_id: 200\n",
            user_id=333,
            access_tier="team",
            is_collaborator=True,
            priority="normal",
            user_task_text="[Discord @alice] hello",
            tier_instructions={
                "team-collaborator": "COLLABORATOR RULEBOOK\n",
                "other": "OTHER RULEBOOK\n",
            },
            skill_hints="SKILL HINTS\n",
            secret_notice="SECRET NOTICE\n",
        )
        expected = (
            "id: task-123\n"
            "timestamp: 2026-08-02T06:00:00Z\n"
            "source: discord\n"
            "interaction_type: message\n"
            "content_modalities: text,image\n"
            "media_form: attachment\n"
            "channel_id: 111\n"
            "channel_name: dev\n"
            "guild_name: Sutando\n"
            "source_message_id: 222\n"
            "parent_message_id: 200\n"
            "user_id: 333\n"
            "access_tier: team\n"
            "collaborator: true\n"
            "priority: normal\n"
            "task: [Discord @alice] hello\n"
            "COLLABORATOR RULEBOOK\n"
            "SKILL HINTS\n"
            "SECRET NOTICE\n"
        )
        self.assertEqual(actual, expected)

    def test_non_collaborator_uses_own_tier_without_marker(self):
        actual = build_task_content(
            task_id="task-1",
            timestamp="t",
            media_headers="",
            channel_id="c",
            channel_name="DM",
            guild_name="DM",
            source_message_id="m",
            parent_headers="",
            user_id="u",
            access_tier="owner",
            is_collaborator=False,
            priority="normal",
            user_task_text="hello",
            tier_instructions={"owner": "OWNER\n", "other": "OTHER\n"},
            skill_hints="",
            secret_notice="",
        )
        self.assertIn("access_tier: owner\npriority: normal", actual)
        self.assertNotIn("collaborator:", actual)
        self.assertTrue(actual.endswith("task: hello\nOWNER\n"))


if __name__ == "__main__":
    unittest.main()
