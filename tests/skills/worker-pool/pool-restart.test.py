#!/usr/bin/env python3
"""`pool_remedy --restart <id>`: the owner's restart of one worker, outside the death ladder.

One JSON line `{worker_id, result, detail}`; result is restarted | already-running | paused |
suspended | failed; exit 0 except failed (1) and an invalid id (2). A restart or an
already-running worker clears that worker's ladder evidence only, so an escalated worker
starts clean; any other result leaves the ladder alone.

Run: python3 tests/skills/worker-pool/pool-restart.test.py
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / "skills" / "worker-pool" / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


rem = _load("pool_remedy")
ps = rem.ps
WID, OTHER = "a" * 32, "b" * 32


class Restart(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.ws = Path(self.td.name) / "ws"
        (self.ws / "state").mkdir(parents=True)
        self.escalated = ps.WorkerEvidence(consecutive=351, escalated=True, recover_issued_at=5.0)
        self.other = ps.WorkerEvidence(consecutive=2)
        rem.sup.save_state(self.ws, ps.SupervisionState(last_sample_at=1.0, workers={
            WID: self.escalated, OTHER: self.other}))
        self.ensure = mock.patch.object(rem, "ensure_supervisor", return_value={"outcome": rem.SUPERVISED})
        self.watch = mock.patch.object(rem, "ensure_input_watch", return_value={"outcome": rem.WATCHING})
        self.sup_mock, self.watch_mock = self.ensure.start(), self.watch.start()

    def tearDown(self):
        mock.patch.stopall()
        self.td.cleanup()

    def run_main(self, recovered):
        out = io.StringIO()
        with mock.patch.object(rem, "recover", return_value=recovered), redirect_stdout(out):
            rc = rem.main(["--workspace", str(self.ws), "--repo", str(REPO), "--restart", WID])
        lines = out.getvalue().strip().splitlines()
        self.assertEqual(len(lines), 1)
        return rc, json.loads(lines[0])

    def ladder(self):
        return rem.sup.load_state(self.ws).workers

    def test_an_escalated_worker_restarts_now_and_its_ladder_starts_clean(self):
        rc, out = self.run_main({"worker_id": WID, "outcome": rem.RECOVERED, "session_id": "s1"})
        self.assertEqual(rc, 0)
        self.assertEqual((out["worker_id"], out["result"]), (WID, "restarted"))
        self.assertEqual(out["detail"], {"session_id": "s1", "supervisor": rem.SUPERVISED,
                                         "input_watch": rem.WATCHING})
        self.assertNotIn(WID, self.ladder())
        self.assertEqual(self.ladder()[OTHER], self.other)
        self.sup_mock.assert_called_once(), self.watch_mock.assert_called_once()

    def test_a_running_worker_is_left_alone_and_exits_zero(self):
        rc, out = self.run_main({"worker_id": WID, "outcome": rem.ALREADY_RUNNING})
        self.assertEqual((rc, out["result"]), (0, "already-running"))
        self.assertNotIn(WID, self.ladder())

    def test_a_paused_worker_is_not_restarted_and_keeps_its_ladder(self):
        rc, out = self.run_main({"worker_id": WID, "outcome": rem.PAUSED})
        self.assertEqual((rc, out["result"]), (0, "paused"))
        self.assertEqual(self.ladder()[WID], self.escalated)
        self.sup_mock.assert_not_called()

    def test_a_suspended_pool_restarts_nothing(self):
        rem.suspend(self.ws, "app-quit", now=1)
        with mock.patch.object(rem, "recover") as rec:
            rc, out = self.run_main({"worker_id": WID, "outcome": rem.RECOVERED})
        rec.assert_not_called()
        self.assertEqual((rc, out["result"]), (0, "suspended"))
        self.assertEqual(self.ladder()[WID], self.escalated)

    def test_every_other_outcome_is_failed_exits_one_and_names_why(self):
        for outcome in (rem.FAILED, rem.NO_SESSION, rem.INDETERMINATE):
            with self.subTest(outcome=outcome):
                rc, out = self.run_main({"worker_id": WID, "outcome": outcome, "why": "x"})
                self.assertEqual((rc, out["result"]), (1, "failed"))
                self.assertEqual(out["detail"], {"why": "x", "outcome": outcome})
                self.assertEqual(self.ladder()[WID], self.escalated)

    def test_an_invalid_id_is_failed_with_exit_two(self):
        out = io.StringIO()
        with mock.patch.object(rem, "recover") as rec, redirect_stdout(out):
            rc = rem.main(["--workspace", str(self.ws), "--repo", str(REPO), "--restart", "../escape"])
        rec.assert_not_called()
        self.assertEqual(rc, 2)
        got = json.loads(out.getvalue())
        self.assertEqual((got["worker_id"], got["result"]), ("../escape", "failed"))
        self.assertIn("worker id must match", got["detail"]["why"])

    def test_restart_is_one_mode_among_the_others(self):
        with self.assertRaises(SystemExit), mock.patch("sys.stderr", new_callable=io.StringIO):
            rem.main(["--workspace", str(self.ws), "--repo", str(REPO), "--restart", WID, "--sweep"])


if __name__ == "__main__":
    sys.exit(unittest.main())
