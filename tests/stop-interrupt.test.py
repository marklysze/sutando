#!/usr/bin/env python3
"""stop_interrupt: a Stop interrupts the core's turn only when the pane is mid-turn on that task.

Run: python3 tests/stop-interrupt.test.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
import stop_interrupt as si  # noqa: E402

TARGET = "task-ag2space_dlocal~task-0e542ed29e181e5510"
BUSY = "✻ Running… (12s · esc to interrupt)\n> "
IDLE = "> \n  ? for shortcuts"


class FakeTmux:
    def __init__(self, pane_text=BUSY):
        self.pane_text, self.calls = pane_text, []

    def __call__(self, argv):
        self.calls.append(argv)
        if "list-windows" in argv:
            return "0\n"
        if "capture-pane" in argv:
            return self.pane_text
        return ""

    def escapes(self):
        return [c for c in self.calls if c[-1] == "Escape"]


class StopInterrupt(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.ws = Path(self.td.name)
        self.tasks = self.ws / "tasks"
        self.tasks.mkdir()
        (self.ws / "state").mkdir()
        (self.tasks / f"{TARGET}.txt").write_text(f"id: {TARGET}\nchannel_id: !room:x\ntask: sleep 200\n")
        self.now = time.time()
        self.logs = []
        env = {k: v for k, v in os.environ.items() if not k.startswith("SUTANDO_TMUX")}
        mock.patch.dict(os.environ, env, clear=True).start()
        self.addCleanup(mock.patch.stopall)

    def tearDown(self):
        self.td.cleanup()

    def row(self, task_id, age=5.0, **extra):
        rec = {"ts": self.now - age, "line": "Bash", "kind": "working", "task": {"id": task_id}, **extra}
        with open(self.ws / "state" / "agent-activity.jsonl", "a") as fh:
            fh.write(json.dumps(rec) + "\n")

    def run_it(self, tmux):
        return si.interrupt(TARGET, workspace=self.ws, tasks_dir=self.tasks, run=tmux,
                            log=self.logs.append, now=self.now)

    def owner_rows(self):
        rows = [json.loads(l) for l in (self.ws / "state" / "agent-activity.jsonl").read_text().splitlines()]
        return [r for r in rows if r.get("audience") == "owner"]

    def test_mid_turn_on_the_cancelled_task_sends_one_escape(self):
        self.row(TARGET)
        tmux = FakeTmux()
        self.assertTrue(self.run_it(tmux))
        esc = tmux.escapes()
        self.assertEqual(len(esc), 1)
        self.assertIn("tmux-pane-keys.sh", esc[0][1])
        self.assertEqual(esc[0][esc[0].index("-t") + 1], "=sutando-core:0")
        self.assertIn("interrupted the running turn", self.owner_rows()[-1]["line"])
        self.assertEqual(self.owner_rows()[-1]["room"], "!room:x")

    def live_rows(self, with_hook_rows):
        """The rig's rows for a Stop at +31 s on a `sleep 90` task: only the bus's TASK_STATUS rows
        existed live, because the hook could not bind an id carrying `~`."""
        base = {"room": "!r:x", "task": {"id": TARGET}}
        self.row(TARGET, age=42, **{**base, "kind": "notice", "line": "queued", "audience": "room",
                                    "projection": "TASK_STATUS"})
        self.row(TARGET, age=42, **{**base, "kind": "processing", "line": "picked up", "audience": "room",
                                    "projection": "TASK_STATUS"})
        if with_hook_rows:
            self.row(TARGET, age=40, **{**base, "kind": "processing", "line": "Your agent is working on a task"})
            self.row(TARGET, age=35, **{**base, "kind": "working", "line": "Sleep 90 seconds"})

    def test_the_live_stop_interrupts_once_the_hook_writes_rows_for_the_task(self):
        self.live_rows(with_hook_rows=True)
        tmux = FakeTmux()
        self.assertTrue(self.run_it(tmux))
        self.assertEqual(len(tmux.escapes()), 1)

    def test_delivery_rows_alone_never_name_the_sessions_task(self):
        self.live_rows(with_hook_rows=False)
        tmux = FakeTmux()
        self.assertFalse(self.run_it(tmux))
        self.assertEqual(tmux.escapes(), [])
        self.assertIn("the session is on no task", self.logs[-1])

    def test_the_session_on_another_task_gets_no_key(self):
        self.row("task-other")
        tmux = FakeTmux()
        self.assertFalse(self.run_it(tmux))
        self.assertEqual(tmux.escapes(), [])
        self.assertEqual([c for c in tmux.calls if "capture-pane" in c], [], "the pane is not even read")
        self.assertIn("the session is on task-other", self.owner_rows()[-1]["line"])

    def test_an_idle_pane_gets_no_key(self):
        self.row(TARGET)
        tmux = FakeTmux(IDLE)
        self.assertFalse(self.run_it(tmux))
        self.assertEqual(tmux.escapes(), [])
        self.assertIn("no turn is running", self.owner_rows()[-1]["line"])

    def test_no_fresh_session_row_gets_no_key(self):
        self.row(TARGET, age=si.FRESH_S + 1)
        tmux = FakeTmux()
        self.assertFalse(self.run_it(tmux))
        self.assertEqual(tmux.escapes(), [])

    def test_delivery_rows_and_its_own_rows_never_count_as_the_session_turn(self):
        self.row(TARGET)
        self.row("task-other", projection="TASK_STATUS", audience="room")
        self.row("task-other", audience="owner")
        self.assertEqual(si.current_task(self.ws, self.now), TARGET)

    def test_a_duplicate_cancel_sends_a_single_escape(self):
        self.row(TARGET)
        tmux = FakeTmux()
        self.assertTrue(self.run_it(tmux))
        self.assertFalse(self.run_it(tmux))
        self.assertEqual(len(tmux.escapes()), 1)
        self.assertIn("already interrupted once", self.owner_rows()[-1]["line"])

    def test_a_task_a_pool_worker_holds_is_not_interrupted_from_the_core_pane(self):
        self.row(TARGET)
        (self.ws / "deliveries" / "w1").mkdir(parents=True)
        (self.ws / "deliveries" / "w1" / f"{TARGET}.claimed").write_text("")
        tmux = FakeTmux()
        self.assertFalse(self.run_it(tmux))
        self.assertEqual(tmux.escapes(), [])

    def test_the_worker_holding_it_interrupts_its_own_pane(self):
        self.row(TARGET)
        inbox = self.ws / "deliveries" / "w1"
        inbox.mkdir(parents=True)
        (inbox / f"{TARGET}.claimed").write_text("")
        os.environ["SUTANDO_TMUX_PANE"] = "%7"
        tmux = FakeTmux()
        self.assertTrue(si.interrupt(TARGET, workspace=self.ws, tasks_dir=inbox, run=tmux,
                                     log=self.logs.append, now=self.now))
        self.assertEqual(tmux.escapes()[0][tmux.escapes()[0].index("-t") + 1], "%7")

    def test_a_failed_send_is_logged_and_reports_false(self):
        self.row(TARGET)
        tmux = FakeTmux()
        real = tmux.__call__
        tmux_fail = lambda argv: None if argv[-1] == "Escape" else real(argv)  # noqa: E731
        self.assertFalse(si.interrupt(TARGET, workspace=self.ws, tasks_dir=self.tasks, run=tmux_fail,
                                      log=self.logs.append, now=self.now))
        self.assertIn("could not be sent", self.logs[-1])

    def test_an_unreadable_or_rowless_log_names_no_current_task(self):
        self.assertIsNone(si.current_task(self.ws, self.now), "no log")
        log = self.ws / "state" / "agent-activity.jsonl"
        log.write_text('{"ts": 1, "kind": "working", "task": {"id": "x"\n{"ts": 2, "kind": "notice"}\n')
        self.assertIsNone(si.current_task(self.ws, self.now), "malformed and taskless rows are skipped")

    def test_an_unreadable_pane_or_pool_state_sends_nothing(self):
        self.row(TARGET)
        tmux = FakeTmux(None)
        self.assertFalse(self.run_it(tmux))
        self.assertIn("could not be read", self.logs[-1])
        with mock.patch.object(si, "worker_holds", side_effect=OSError("unlistable")):
            (self.ws / "deliveries").mkdir()
            self.assertTrue(si._held_elsewhere(self.ws, self.tasks, TARGET))

    def test_a_row_that_cannot_be_written_never_blocks_the_interrupt(self):
        self.row(TARGET)
        tmux = FakeTmux()
        with mock.patch.object(si, "append_row", side_effect=OSError("disk full")):
            self.assertTrue(self.run_it(tmux))
        self.assertEqual(len(tmux.escapes()), 1)

    def test_the_cli_takes_the_canonical_workspace_and_never_fails(self):
        seen = []
        with mock.patch.object(si, "interrupt", lambda t, **k: seen.append((t, k["workspace"]))), \
                mock.patch.object(si, "resolve_workspace", return_value=self.ws):
            self.assertEqual(si.main([TARGET, "--tasks-dir", str(self.ws / "deliveries" / "w1")]), 0)
            self.assertEqual(si.main([TARGET, "--tasks-dir", str(self.tasks), "--workspace", "/w"]), 0)
        self.assertEqual(seen, [(TARGET, self.ws), (TARGET, Path("/w"))])
        with mock.patch.object(si, "interrupt", side_effect=RuntimeError("boom")):
            self.assertEqual(si.main([TARGET, "--tasks-dir", str(self.tasks)]), 0, "advisory: never fails")

    def test_run_returns_stdout_only_on_success(self):
        self.assertEqual(si._run(["echo", "hi"]), "hi\n")
        self.assertIsNone(si._run(["false"]))
        self.assertIsNone(si._run(["/nonexistent/tmux"]))

    def test_the_watcher_emit_hands_the_cancelled_task_to_stop_interrupt(self):
        ctl = self.tasks / "task-ctl-1.txt"
        ctl.write_text(f"id: task-ctl-1\ntask: CANCEL_INSTRUCTION: stop processing {TARGET}\n")
        stub = self.ws / "py"
        record = self.ws / "calls"
        stub.write_text(f'#!/bin/bash\necho "$*" >> "{record}"\n')
        stub.chmod(0o755)
        subprocess.run(["bash", "-c", f'source "{REPO}/src/task-emit.sh"; activity_cancel_target "{ctl.name}"; wait'],
                       env={**os.environ, "SUTANDO_PY_BIN": str(stub), "TASKS_DIR": str(self.tasks)}, check=True)
        for _ in range(50):
            if record.exists() and "stop_interrupt.py" in record.read_text():
                break
            time.sleep(0.05)
        calls = record.read_text().splitlines()
        self.assertIn(f"stop_interrupt.py {TARGET} --tasks-dir {self.tasks}",
                      [c.split("/src/", 1)[-1] for c in calls])


if __name__ == "__main__":
    unittest.main()
