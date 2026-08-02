#!/usr/bin/env python3
"""Contract and wiring tests for atomic workspace JSON publication."""
from __future__ import annotations

import ast
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from atomic_state import write_json  # noqa: E402


def _function_source(path: Path, name: str) -> str:
    source = path.read_text()
    tree = ast.parse(source)
    node = next(
        item
        for item in tree.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        and item.name == name
    )
    return ast.get_source_segment(source, node) or ""


class AtomicStateTests(unittest.TestCase):
    def test_compact_json_and_parent_creation(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "state" / "routing.json"
            self.assertTrue(write_json(target, {"task-1": {"channel": "D1"}}))
            self.assertEqual(target.read_text(), '{"task-1": {"channel": "D1"}}')

    def test_real_writer_is_safe_across_same_process_threads(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "state.json"
            failures = []

            def write_many(worker: int) -> None:
                for i in range(200):
                    if not write_json(target, {"worker": worker, "i": i}):
                        failures.append((worker, i))

            threads = [
                threading.Thread(target=write_many, args=(worker,))
                for worker in range(12)
            ]
            for thread in threads:
                thread.start()
            while any(thread.is_alive() for thread in threads):
                if target.exists():
                    json.loads(target.read_text())
            for thread in threads:
                thread.join()

            self.assertEqual(failures, [])
            json.loads(target.read_text())
            self.assertEqual(list(Path(td).glob("*.tmp")), [])

    def test_replace_failure_preserves_destination_and_cleans_stage(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "state.json"
            target.write_text('{"old": true}')
            errors = []
            with patch("atomic_state.os.replace", side_effect=OSError("disk")):
                self.assertFalse(write_json(target, {"new": True}, on_error=errors.append))
            self.assertEqual(target.read_text(), '{"old": true}')
            self.assertEqual(len(errors), 1)
            self.assertEqual(list(Path(td).glob("*.tmp")), [])

    def test_bounded_bridge_state_writers_delegate(self):
        discord_path = REPO / "src" / "discord-bridge.py"
        slack_path = REPO / "src" / "slack-bridge.py"
        writers = [
            _function_source(discord_path, "_atomic_write_dm_checkpoint"),
            _function_source(discord_path, "_atomic_write_pending_replies"),
            _function_source(slack_path, "_atomic_write_pending_replies"),
        ]
        for writer in writers:
            self.assertIn("_write_atomic_json", writer)
            self.assertNotIn('with_suffix(".json.tmp")', writer)


if __name__ == "__main__":
    unittest.main()
