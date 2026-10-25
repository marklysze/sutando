#!/usr/bin/env python3
"""The bridge's task-lifecycle hooks, in process (REMOTE_TASK_LIFECYCLE_V2 + REMOTE_TASK_TURN_CONTEXT).

The real bridge module, imported hermetically with both flags on, against a fake `_req`:
the ack paths, the per-task beat riding the client heartbeat, a cancel reaching the core,
the ledger reconcile, the startup turn sweep, the result gate, and the control-task sweep.

Run: python3 tests/gateway-agent-activity-hooks.test.py
"""
from __future__ import annotations

import io
import json
import os
import sys
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers.hermetic_gateway import isolate_then_import  # noqa: E402

gw, _READS = isolate_then_import(env={"REMOTE_TASK_LIFECYCLE_V2": "1", "REMOTE_TASK_TURN_CONTEXT": "1"})
agent_turn = gw.task_attempts.agent_turn


def _http(code, body):
    return urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(json.dumps(body).encode()))


class FakeGateway:
    def __init__(self, **answers):
        self.calls, self.answers = [], answers

    def __call__(self, method, path, payload=None, timeout=None, **_k):
        self.calls.append((method, path, payload))
        ans = self.answers.get(path.split("?")[0])
        if isinstance(ans, BaseException):
            raise ans
        return ans(payload) if callable(ans) else (ans if ans is not None else {})

    def posted(self, suffix):
        return [p for _m, path, p in self.calls if path.endswith(suffix)]


def _task(tid, **extra):
    return {"id": tid, "task": "reply", "source": "ag2space", "user_id": "@o:x", "access_tier": "owner",
            **extra}


def _ack(tid, gen=1):
    return {"ok": True, "durable": True, "task_id": tid, "execution_generation": gen, "attempt": gen,
            "lease_expires_at": 1761354125.0}


class Hooks(unittest.TestCase):
    def setUp(self):
        self.logs = []
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(gw, "_log", self.logs.append).start()

    def use(self, fake):
        mock.patch.object(gw, "_req", fake).start()
        return fake

    def controls(self, tid):
        return [p for p in gw.TASKS_DIR.glob("task-ctl-*.txt") if f"stop processing {tid} " in p.read_text()]

    def test_the_ack_carries_the_lease_holder_and_records_the_generation(self):
        self.assertIsNotNone(gw._write_task(_task("task-h1")))
        fake = self.use(FakeGateway(**{"/v1/tasks/task-h1/ack": _ack("task-h1", 4)}))
        self.assertTrue(gw._post_task_ack("task-h1", durable=True))
        body = fake.posted("/ack")[0]
        self.assertEqual((body["protocol_version"], body["worker_id"]), (2, gw._ATTEMPTS.worker_id()))
        rec = gw._ATTEMPTS.get("task-h1")
        self.assertEqual(rec["execution_generation"], 4)
        self.assertEqual(agent_turn.open_turn_for_task(gw._STATE, "task-h1").stem, rec["turn_id"])

    def test_a_cancelled_ack_stops_the_core_and_is_acknowledged(self):
        cancel = _http(409, {"error_code": "task_cancelled", "cancel_requested_generation": 2,
                             "cancel_requested_attempt": 2})
        fake = self.use(FakeGateway(**{"/v1/tasks/task-h2/ack": cancel}))
        self.assertFalse(gw._post_task_ack("task-h2", durable=True))
        self.assertEqual(len(self.controls("task-h2")), 1)
        self.assertEqual(fake.posted("/cancel-ack")[0]["cancel_requested_generation"], 2)

    def test_the_ack_first_path_records_the_generation_or_refuses_a_cancel(self):
        self.use(FakeGateway(**{"/v1/tasks/task-h3/ack": _ack("task-h3", 2)}))
        self.assertEqual(gw._post_ack_first("task-h3", None), "accepted")
        self.assertEqual(gw._ATTEMPTS.get("task-h3")["execution_generation"], 2)
        fake = self.use(FakeGateway(**{"/v1/tasks/task-h3b/ack": _http(409, {"error_code": "task_cancelled"})}))
        self.assertEqual(gw._post_ack_first("task-h3b", None), "stale")
        self.assertEqual(self.controls("task-h3b"), [], "a staged task never reached the core")
        self.assertEqual(len(fake.posted("/cancel-ack")), 1)

    def test_the_client_heartbeat_carries_a_per_task_beat_that_can_fence(self):
        gw._ATTEMPTS.record_ack("task-h4", _ack("task-h4", 1))
        fake = self.use(FakeGateway(**{"/v1/tasks/heartbeat": {"ok": True, "task_errors": [
            {"task_id": "task-h4", "error_code": "stale_execution_generation"},
            {"task_id": "task-elsewhere", "error_code": "task_cancelled"}]}}))
        gw._post_heartbeat({"task-h4"}, force=True)
        beat = fake.posted("/v1/tasks/heartbeat")[0]
        self.assertEqual(beat["tasks"][0]["task_id"], "task-h4")
        self.assertEqual(len(self.controls("task-h4")), 1)
        self.assertEqual(self.controls("task-elsewhere"), [], "only this node's attempts are fenced")

    def test_two_cancels_in_one_millisecond_both_reach_the_core(self):
        with mock.patch.object(gw.time, "time", return_value=1761500000.0):
            gw._queue_cancel_instruction("task-h5a", "task_cancelled")
            gw._queue_cancel_instruction("task-h5b", "task_cancelled")
        self.assertEqual((len(self.controls("task-h5a")), len(self.controls("task-h5b"))), (1, 1))

    def test_a_result_written_before_the_ack_waits_for_the_lease(self):
        self.assertTrue(gw._take_served_task(_task("task-race"), set(), []))
        fake = self.use(FakeGateway(**{"/v1/tasks/task-race/ack": _ack("task-race", 3),
                                       "/v1/results": {"ok": True}}))
        self.assertFalse(gw._deliver_result_payload("task-race", "task-race", "fast answer"))
        self.assertEqual(fake.posted("/v1/results"), [], "nothing goes before the ack names the lease")
        self.assertTrue(gw._post_task_ack("task-race", durable=True))
        turn = agent_turn.open_turn_for_task(gw._STATE, "task-race").stem
        self.assertTrue(gw._deliver_result_payload("task-race", "task-race", "fast answer"))
        sent = fake.posted("/v1/results")[0]
        self.assertEqual((sent["execution_generation"], sent["status"]), (3, "completed"))
        self.assertTrue(agent_turn.is_closed(gw._STATE, turn))

    def test_room_actions_before_the_ack_still_gate_the_result(self):
        self.assertTrue(gw._take_served_task(_task("task-early"), set(), []))
        with open(agent_turn.room_actions_path(gw._STATE), "a") as fh:
            fh.write(json.dumps({"record_type": "dispatch_intent", "turn_id": None, "task_id": "task-early",
                                 "ts": time.time(), "action": "room.message.send",
                                 "body_sha256": agent_turn.body_sha256("the answer")}) + "\n")
        fake = self.use(FakeGateway(**{"/v1/tasks/task-early/ack": _ack("task-early", 1),
                                       "/v1/results": {"ok": True}}))
        self.assertTrue(gw._post_task_ack("task-early", durable=True))
        self.assertTrue(gw._deliver_result_payload("task-early", "task-early", "the answer"))
        self.assertEqual(fake.posted("/v1/results")[0].get("no_send"), True, "the early send is not repeated")

    def early(self, tid, kind):
        """A room action the proxy journalled unbound, naming the task, before any ack."""
        rec = {"turn_id": None, "task_id": tid, "ts": time.time(), "seq": 1, "operation_id": f"op_{tid}"}
        if kind == "send":
            rec.update(record_type="dispatch_intent", action="room.message.send",
                       body_sha256=agent_turn.body_sha256("answer"))
        elif kind == "terminal":
            rec.update(record_type="dispatch_outcome", operation_status="succeeded",
                       applied_execution_phase="responded", applied_terminal=True)
        else:
            rec.update(record_type="dispatch_outcome", operation_status="succeeded",
                       applied_execution_phase="waiting_input", applied_terminal=False,
                       active_blocker_count=1)
        with open(agent_turn.room_actions_path(gw._STATE), "a") as fh:
            fh.write(json.dumps(rec) + "\n")

    def test_on_a_broker_that_names_leases_an_unanswered_ack_never_releases_the_result(self):
        gw._ATTEMPTS.record_ack("task-v2seen", _ack("task-v2seen", 1))
        self.assertTrue(gw._ATTEMPTS.broker_names_leases())
        self.assertTrue(gw._take_served_task(_task("task-cap"), set(), []))
        self.early("task-cap", "send")
        fake = self.use(FakeGateway(**{"/v1/results": {"ok": True}}))
        later = time.time() + gw.task_attempts.ACK_HOLD_MAX_S + 1
        with mock.patch.object(gw._ATTEMPTS, "now", lambda: later):
            self.assertFalse(gw._deliver_result_payload("task-cap", "task-cap", "answer"))
            self.assertFalse(gw._deliver_result_payload("task-cap", "task-cap", "answer"))
        self.assertEqual(fake.posted("/v1/results"), [], "never posted ungated")
        self.assertEqual(sum("waits for the ack or a fence" in m for m in self.logs), 1, "said once")
        gw._ATTEMPTS.fence("task-cap", {"error_code": "task_already_terminal"})
        self.assertTrue(gw._deliver_result_payload("task-cap", "task-cap", "answer"), "a fence ends the wait")
        self.assertEqual(fake.posted("/v1/results"), [])

    def test_a_held_result_whose_task_the_server_closed_is_settled_by_the_reconcile(self):
        gw._ATTEMPTS.record_ack("task-v2seen2", _ack("task-v2seen2", 1))
        tid = "task-held-closed"
        self.assertTrue(gw._take_served_task(_task(tid), set(), []))
        later = time.time() + 10_000
        fake = self.use(FakeGateway(**{"/v1/tasks/active": {"tasks": [], "next_cursor": None},
                                       "/v1/results": {"ok": True}}))
        mock.patch.object(gw._ATTEMPTS, "now", lambda: later).start()
        self.assertFalse(gw._deliver_result_payload(tid, tid, "answer"), "held: its ack never answered")
        mock.patch.object(gw, "_last_ledger_reconcile", 0.0).start()
        gw._reconcile_ledger({tid})
        self.assertEqual(gw._ATTEMPTS.get(tid)["fenced"], "task_already_terminal")
        self.assertEqual(len(self.controls(tid)), 1, "the core is stopped")
        self.assertTrue(gw._deliver_result_payload(tid, tid, "answer"), "settled")
        self.assertEqual(fake.posted("/v1/results"), [], "never sent ungated")

    def test_on_a_broker_never_seen_to_name_leases_the_fallback_is_still_gated(self):
        cases = {"none": {}, "send": {"status": "completed", "no_send": True},
                 "terminal": None, "blocker": {"status": "waiting_input", "no_send": True}}
        with mock.patch.object(gw._ATTEMPTS, "broker_names_leases", lambda: False):
            for kind, want in cases.items():
                with self.subTest(kind=kind):
                    tid = f"task-legacy-{kind}"
                    self.assertTrue(gw._take_served_task(_task(tid), set(), []))
                    if kind != "none":
                        self.early(tid, kind)
                    fake = self.use(FakeGateway(**{"/v1/results": {"ok": True}}))
                    self.assertFalse(gw._deliver_result_payload(tid, tid, "answer"), "waits first")
                    later = time.time() + gw.task_attempts.ACK_HOLD_MAX_S + 1
                    with mock.patch.object(gw._ATTEMPTS, "now", lambda: later):
                        self.assertTrue(gw._deliver_result_payload(tid, tid, "answer"))
                    posted = fake.posted("/v1/results")
                    if want is None:
                        self.assertEqual(posted, [], "closed through MCP: settled, not posted")
                        continue
                    sent = posted[0]
                    self.assertNotIn("execution_generation", sent)
                    self.assertEqual({k: sent[k] for k in want}, want)
                    if kind == "none":
                        self.assertNotIn("status", sent, "no journal record: today's post")

    def test_a_redelivery_before_the_ack_keeps_the_first_publication_boundary(self):
        for kind, want in (("send", {"status": "completed", "no_send": True}), ("terminal", None),
                           ("blocker", {"status": "waiting_input", "no_send": True})):
            with self.subTest(kind=kind):
                tid = f"task-redeliver-{kind}"
                self.assertTrue(gw._take_served_task(_task(tid), set(), []))
                first = gw._ATTEMPTS.get(tid)["published_at"]
                self.early(tid, kind)
                with mock.patch.object(gw._ATTEMPTS, "now", lambda: first + 5):
                    self.assertEqual(gw._write_task(_task(tid)), (tid, True), "served again before the ack")
                self.assertEqual(gw._ATTEMPTS.get(tid)["published_at"], first, "the boundary does not move")
                fake = self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _ack(tid, 1), "/v1/results": {"ok": True}}))
                with mock.patch.object(agent_turn.time, "time", lambda: first + 10):
                    self.assertTrue(gw._post_task_ack(tid, durable=True))
                self.assertTrue(gw._deliver_result_payload(tid, tid, "answer"))
                posted = fake.posted("/v1/results")
                if want is None:
                    self.assertEqual(posted, [])
                else:
                    self.assertEqual({k: posted[0][k] for k in want}, want)

    def test_a_late_ack_for_a_finished_task_opens_no_turn(self):
        gw._ATTEMPTS.remember_task("task-late", _task("task-late"))
        self.use(FakeGateway(**{"/v1/tasks/task-late/ack": _ack("task-late", 1)}))
        self.assertTrue(gw._post_task_ack("task-late", durable=True))
        self.assertIsNone(agent_turn.open_turn_for_task(gw._STATE, "task-late"))
        self.assertEqual(gw._ATTEMPTS.get("task-late")["execution_generation"], 1)

    def test_every_fencing_ack_answer_fences_retires_and_stops_the_core(self):
        cases = [(409, "stale_execution_generation"), (404, "stale_execution_generation"),
                 (409, "task_not_found"), (409, "task_already_terminal")]
        for status, code in cases:
            with self.subTest(status=status, code=code):
                tid = f"task-fence-{status}-{code}"
                inflight, pending = set(), []
                self.assertTrue(gw._take_served_task(_task(tid), inflight, pending))
                self.assertTrue(gw._record_pending_acks(pending))
                fake = self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _http(status, {
                    "error": "not leased to you", "reason": "stale lease generation", "error_code": code}),
                    "/v1/results": {"ok": True}}))
                self.assertFalse(gw._post_task_ack(tid, durable=True))
                self.assertEqual(gw._ATTEMPTS.get(tid)["fenced"], code)
                self.assertNotIn(tid, gw._load_pending_acks(), "its pending ack is retired")
                self.assertEqual(len(self.controls(tid)), 1, "the core is stopped")
                self.assertTrue(gw._deliver_result_payload(tid, tid, "late"))
                self.assertEqual(fake.posted("/v1/results"), [], "its result is settled, never posted")

    def test_a_stale_older_lease_with_a_newer_serve_here_is_left_to_the_newer_ack(self):
        tid = "task-reserved"
        self.assertTrue(gw._take_served_task(_task(tid, lease_id="L2"), set(), []))
        self.assertTrue(gw._record_pending_acks([(tid, True, "L2")]))
        self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _http(404, {"error_code": "stale_execution_generation"})}))
        self.assertFalse(gw._post_task_ack(tid, durable=True, lease_id="L1"))
        self.assertIsNone(gw._ATTEMPTS.get(tid).get("fenced"))
        self.assertEqual(self.controls(tid), [])
        self.assertIn(tid, gw._load_pending_acks(), "the newer serve's ack is still due")

    def test_a_fenced_ack_first_task_is_refused_without_reaching_the_core(self):
        tid = "task-af-stale"
        gw._ATTEMPTS.remember_task(tid, _task(tid))
        self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _http(404, {"error_code": "stale_execution_generation"})}))
        self.assertEqual(gw._post_ack_first(tid, None), "stale")
        self.assertEqual(gw._ATTEMPTS.get(tid)["fenced"], "stale_execution_generation")
        self.assertEqual(self.controls(tid), [])

    def test_a_wake_is_never_run_without_support_even_as_the_first_sight_of_its_id(self):
        wake = {"blocker_episode_id": "ep1", "resolved_blockers": [
            {"outcome": "answered", "answer": {"kind": "text", "text": "blue"}}]}
        self.assertIsNone(gw._write_task(_task("task-fresh-wake", wake=wake)))
        self.assertFalse((gw.TASKS_DIR / "task-fresh-wake.txt").exists())
        self.assertIsNone(gw._write_task(_task("task-fresh-wake", ack_first=True, wake=wake), hold=True))

    def test_a_legacy_not_leased_refusal_releases_the_result_without_fencing(self):
        tid = "task-legacy"
        self.assertTrue(gw._take_served_task(_task(tid), set(), []))
        refusal = _http(404, {"error": "not leased to you"})
        self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": refusal}))
        self.assertFalse(gw._post_task_ack(tid, durable=True))
        rec = gw._ATTEMPTS.get(tid)
        self.assertEqual((rec.get("fenced"), rec.get("awaiting_ack")), (None, False))
        self.assertIsNone(gw._ATTEMPTS.result_hold(tid), "nothing left to wait for")
        self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _http(404, {"error": "not leased to you"})}))
        self.assertEqual(gw._post_ack_first(tid, None), "gone")

    def test_a_journal_line_still_being_written_defers_the_result(self):
        self.assertTrue(gw._take_served_task(_task("task-defer"), set(), []))
        fake = self.use(FakeGateway(**{"/v1/tasks/task-defer/ack": _ack("task-defer", 1),
                                       "/v1/results": {"ok": True}}))
        self.assertTrue(gw._post_task_ack("task-defer", durable=True))
        with open(agent_turn.room_actions_path(gw._STATE), "a") as fh:
            fh.write('{"record_type": "dispatch_intent", "turn_id": nul')
        self.addCleanup(lambda: agent_turn.room_actions_path(gw._STATE).write_text(""))
        self.assertFalse(gw._deliver_result_payload("task-defer", "task-defer", "x"))
        self.assertEqual(fake.posted("/v1/results"), [])
        self.assertTrue(any("still being written" in m for m in self.logs))

    def test_a_cancel_instruction_that_cannot_be_written_is_reported(self):
        with mock.patch.object(gw, "_durable_write", return_value=False):
            gw._queue_cancel_instruction("task-h5", "task_cancelled")
        self.assertTrue(any("cancel instruction not written" in m for m in self.logs))

    def test_the_ledger_reconcile_closes_what_the_server_closed(self):
        gw._ATTEMPTS.record_ack("task-h6", _ack("task-h6", 1))
        gw._ATTEMPTS._update("task-h6", acked_at=0)
        self.use(FakeGateway(**{"/v1/tasks/active": {"tasks": [{"task_id": "task-srv"}], "next_cursor": None}}))
        mock.patch.object(gw, "_last_ledger_reconcile", 0.0).start()
        gw._reconcile_ledger({"task-h6"})
        self.assertEqual(len(self.controls("task-h6")), 1)
        listed = json.loads(gw.OUTSTANDING_TASKS_FILE.read_text())
        self.assertEqual([t["task_id"] for t in listed["tasks"]], ["task-srv"])
        self.assertTrue(any("not held here" in m for m in self.logs))
        before = len(self.controls("task-h6"))
        gw._reconcile_ledger({"task-h6"})
        self.assertEqual(len(self.controls("task-h6")), before, "the cadence holds the next pass")
        mock.patch.object(gw, "_last_ledger_reconcile", 0.0).start()
        self.use(FakeGateway(**{"/v1/tasks/active": _http(503, {"error_code": "task_api_unavailable"})}))
        gw._reconcile_ledger({"task-h6"})

    def test_turns_whose_task_left_flight_are_closed_at_start(self):
        turn = gw._ATTEMPTS.record_ack("task-h7", _ack("task-h7", 1))["turn_id"]
        gw._close_stale_turns(set())
        self.assertTrue(agent_turn.is_closed(gw._STATE, turn))
        self.assertTrue(any(m.startswith("closed ") and "turn file" in m for m in self.logs))

    def test_a_wake_for_a_finished_task_is_left_unacked(self):
        task = {"id": "task-h8", "task": "hi", "source": "ag2space", "user_id": "@o:x"}
        self.assertIsNotNone(gw._write_task(dict(task)))
        (gw.TASKS_DIR / "archive").mkdir(exist_ok=True)
        (gw.TASKS_DIR / "task-h8.txt").rename(gw.TASKS_DIR / "archive" / "task-h8.txt")
        self.assertIsNone(gw._write_task({**task, "wake": {"blocker_episode_id": "e1"}}))
        self.assertTrue(any("wake for task-h8" in m for m in self.logs))

    def test_the_result_gate_settles_a_closed_turn_and_posts_the_rest_with_lease_fields(self):
        turn = gw._ATTEMPTS.record_ack("task-h9", _ack("task-h9", 3))["turn_id"]
        with open(agent_turn.room_actions_path(gw._STATE), "a") as fh:
            fh.write(json.dumps({"record_type": "dispatch_outcome", "turn_id": turn, "seq": 1,
                                 "operation_id": f"op_{turn}_1", "operation_status": "succeeded",
                                 "applied_execution_phase": "responded", "applied_terminal": True}) + "\n")
        fake = self.use(FakeGateway(**{"/v1/results": {"ok": True}}))
        self.assertTrue(gw._deliver_result_payload("task-h9", "task-h9", "final"))
        self.assertEqual(fake.posted("/v1/results"), [], "the room action already answered")
        self.assertTrue(agent_turn.is_closed(gw._STATE, turn))

        turn = gw._ATTEMPTS.record_ack("task-h10", _ack("task-h10", 2))["turn_id"]
        self.assertTrue(gw._deliver_result_payload("task-h10", "task-h10", "the answer"))
        sent = fake.posted("/v1/results")[0]
        self.assertEqual((sent["status"], sent["execution_generation"], sent["worker_id"]),
                         ("completed", 2, gw._ATTEMPTS.worker_id()))
        self.assertTrue(agent_turn.is_closed(gw._STATE, turn))
        gw._ATTEMPTS.record_ack("task-h10", _ack("task-h10", 2))
        self.assertTrue(gw._deliver_result_payload("task-h10", "task-h10", "the answer"),
                        "the delivered item answers a repeat")
        self.assertEqual(gw._ATTEMPTS.get("task-h10"), {}, "and the attempt is finished")

    def test_a_control_tasks_result_is_retired_locally_never_posted(self):
        tid = "task-ctl-1"
        (gw.TASKS_DIR / f"{tid}.txt").write_text(gw.task_attempts.cancel_task_text(
            "task-x", tid, "2026-10-26T00:00:00Z", "test"))
        result = gw.RESULTS_DIR / f"{tid}.txt"
        result.write_text("[no-send]")
        old = time.time() - gw.ORPHAN_GRACE_S - 60
        os.utime(result, (old, old))
        fake = self.use(FakeGateway())
        mock.patch.object(gw, "_last_orphan_sweep", 0.0).start()
        gw._reconcile_orphan_results(set())
        self.assertFalse(result.exists())
        self.assertEqual(fake.posted("/v1/results"), [])


if __name__ == "__main__":
    unittest.main()
