#!/usr/bin/env python3
"""A named gateway instance's task id (`task-<instance>~<broker id>`) passes every task-id check
its tasks reach: the settled ledger, owner-agent-consult, the PR shepherd, status-ack's first-seen
record and the connect-apps pre-check. A plain id still passes, and a traversal still fails.

Run: python3 tests/lane-task-ids.test.py
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

isolate_os_environ()

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
import local_task_protocol as ltp  # noqa: E402
import task_lifecycle  # noqa: E402

LANE = "task-ag2space_dlocal~task-9c65da61713b5c635f"
BAD = ("task-a~", "task-~task-1", "task-a~task-1~x", "task-a/b~task-1", "task-..~task-1", "task-a~../x",
       "task-" + "i" * 33 + "~task-1", None, 7)


def _load(rel: str, name: str):
    path = REPO / rel
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class LaneTaskIds(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())

    def test_the_shared_predicate_accepts_a_lane_id_and_nothing_looser(self):
        self.assertTrue(ltp.valid_local_task_id(LANE))
        self.assertTrue(ltp.valid_local_task_id("task-1793057400000"))
        self.assertFalse(ltp.valid_task_id(LANE), "the plain check stays strict")
        for bad in BAD:
            with self.subTest(bad=bad):
                self.assertFalse(ltp.valid_local_task_id(bad))

    def test_the_bridges_instance_rule_and_hold_check_are_the_shared_ones(self):
        sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))
        from ag2_sparrow import gateway_instance, local_task_protocol as pkg_ltp, task_hold
        self.assertIs(gateway_instance.INSTANCE_RE, pkg_ltp.INSTANCE_ID_RE)
        self.assertTrue(task_hold.valid_tid(LANE))
        self.assertFalse(task_hold.valid_tid("task-a~../x"))

    def test_the_settled_ledger_appends_and_rereads_a_lane_id(self):
        path = self.ws / "settled.ids"
        task_lifecycle.SettledIds(path).add(LANE)
        self.assertIn(LANE, task_lifecycle.SettledIds(path).durable)
        with self.assertRaises(task_lifecycle.LedgerError):
            task_lifecycle.SettledIds(path).add("task-a~../x")

    def test_owner_agent_consult_takes_a_lane_task(self):
        consult = _load("skills/owner-agent-consult/scripts/consult_policy.py", "consult_policy_lane")
        (self.ws / "tasks").mkdir()
        (self.ws / "tasks" / f"{LANE}.txt").write_text(f"id: {LANE}\nsource: ag2space\ntask: ask\n")
        self.assertTrue(consult.task_can_post(self.ws, LANE))
        self.assertNotIn("is not a task id", consult.live_task(LANE, self.ws).get("reason", ""))

    def test_the_pr_shepherd_builds_a_contract_path_for_a_lane_task(self):
        shep = _load("skills/pr-shepherd-contract/scripts/shepherd_github.py", "shepherd_github_lane")
        with mock.patch.object(shep, "state_dir", return_value=self.ws):
            self.assertEqual(shep._contract_path(LANE), self.ws / f"{LANE}.json")
            with self.assertRaises(ValueError):
                shep._contract_path("task-a~../x")

    def test_status_ack_records_a_lane_tasks_first_seen(self):
        fs = _load("skills/status-ack/scripts/first_seen.py", "first_seen_lane")
        self.assertEqual(fs.record(self.ws, LANE, 1000.0), 1000.0)
        self.assertEqual(fs.read(self.ws, LANE), 1000.0)

    def test_the_connect_apps_precheck_sees_a_lane_task_file(self):
        pre = _load("skills/connect-apps/hooks/connect-precheck.py", "connect_precheck_lane")
        self.assertEqual([t for _, t in pre.TASK_FILE.findall(f"cat /w/tasks/{LANE}.txt")], [LANE])


if __name__ == "__main__":
    unittest.main()
