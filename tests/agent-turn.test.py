#!/usr/bin/env python3
"""Turn context file and the 3-outcome turn gate (src/agent_turn.py)."""
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import agent_turn as at  # noqa: E402


def _ctx(state, task="tsk_A", gen=1, room="!a:ag2.space"):
    return at.build_turn_context(task_id=task, execution_generation=gen, room_id=room,
                                 agent_mxid="@agent:ag2.space", state_dir=state)


def _journal(state, *records):
    with open(at.room_actions_path(state), "a") as fh:
        for r in records:
            fh.write(json.dumps({"ts": 1.0, **r}) + "\n")


def _intent(turn, seq, status="working", action="room.message.send"):
    return {"record_type": at.INTENT, "turn_id": turn, "seq": seq,
            "operation_id": f"op_{turn}_{seq}", "action": action,
            "requested_task_status": status}


def _outcome(turn, seq, op_status="succeeded", phase="working", terminal=False,
             requested="working", blockers=0):
    return {"record_type": at.OUTCOME, "turn_id": turn, "seq": seq,
            "operation_id": f"op_{turn}_{seq}", "operation_status": op_status,
            "requested_task_status": requested, "applied_execution_phase": phase,
            "applied_terminal": terminal, "active_blocker_count": blockers}


class TurnContext(unittest.TestCase):
    def setUp(self):
        self.state = Path(tempfile.mkdtemp())

    def test_immutable_turn_context_file(self):
        ctx = _ctx(self.state)
        path = at.write_turn_context(self.state, ctx)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o400)
        self.assertEqual(at.read_turn_context(path), ctx)
        self.assertIsInstance(ctx["issued_at"], float, "epoch seconds")
        env = at.child_env(path, base={})
        self.assertEqual(env[at.TURN_CONTEXT_ENV], str(path.resolve()))
        with self.assertRaises(FileExistsError):
            at.write_turn_context(self.state, {**ctx, "room_id": "!other:ag2.space"})
        self.assertEqual(at.read_turn_context(path)["room_id"], "!a:ag2.space")
        self.assertEqual([p.name for p in path.parent.iterdir()], [path.name], "no temp left behind")

    def test_concurrent_turns_in_different_rooms_never_overwrite(self):
        paths, errors = {}, []

        def run(i):
            try:
                c = _ctx(self.state, task=f"tsk_{i}", room=f"!r{i}:ag2.space")
                paths[i] = at.write_turn_context(self.state, c)
            except Exception as e:  # noqa: BLE001
                errors.append(e)
        threads = [threading.Thread(target=run, args=(i,)) for i in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        for i, p in paths.items():
            self.assertEqual(at.read_turn_context(p)["room_id"], f"!r{i}:ag2.space")

    def test_turn_id_shape_and_validation(self):
        tid = at.new_turn_id("tsk_A", 3, now_ms=1761500000000)
        self.assertRegex(tid, r"^tsk_A_g3_[0-9A-Z]{26}$")
        self.assertLess(at.new_turn_id("t", 1, 1), at.new_turn_id("t", 1, 2), "ulid sorts by time")
        for bad in ("../x", "a/b", ""):
            with self.assertRaises(ValueError):
                at.new_turn_id(bad, 1)
        with self.assertRaises(ValueError):
            at.new_turn_id("t", 0)

    def test_proxy_resolves_newest_open_turn_and_close_fences_only_its_own(self):
        old = at.write_turn_context(self.state, {**_ctx(self.state, gen=1)})
        new_ctx = _ctx(self.state, gen=2)
        new = at.write_turn_context(self.state, new_ctx)
        other = at.write_turn_context(self.state, _ctx(self.state, task="tsk_B"))
        self.assertEqual(at.open_turn_for_task(self.state, "tsk_A"), new)
        at.close_turn(self.state, new_ctx["turn_id"])
        self.assertTrue(at.is_closed(self.state, new_ctx["turn_id"]))
        self.assertFalse(new.exists())
        self.assertTrue(old.exists() and other.exists())
        self.assertEqual(at.open_turn_for_task(self.state, "tsk_A"), old)
        self.assertIsNone(at.open_turn_for_task(self.state, "../etc"))


class TurnGate(unittest.TestCase):
    def setUp(self):
        self.state = Path(tempfile.mkdtemp())
        self.turn = _ctx(self.state)["turn_id"]

    def _verdict(self, stdout="the answer", **kw):
        return at.classify_turn(at.read_turn_records(self.state, self.turn), stdout=stdout, **kw)

    def test_case1_text_only_turn_may_send_stdout(self):
        _journal(self.state, _intent("someone_else_g1_X", 1))
        v = self._verdict(server_operation_count=0)
        self.assertEqual((v.outcome, v.may_send_stdout, v.intent_count),
                         (at.INCOMPLETE_OR_ERROR, True, 0))
        self.assertFalse(self._verdict(stdout="  \n", server_operation_count=0).may_send_stdout)

    def test_case1_needs_core_api_to_confirm_zero_operations(self):
        self.assertFalse(self._verdict().may_send_stdout, "unknown server count")
        self.assertFalse(self._verdict(server_operation_count=1).may_send_stdout)

    def test_an_unbound_intent_since_the_turn_began_blocks_case1(self):
        unbound = {**_intent(self.turn, 1), "turn_id": None}
        _journal(self.state, {**unbound, "ts": 100.0})
        recs = at.read_turn_records(self.state, self.turn, issued_at=200.0)
        self.assertTrue(at.classify_turn(recs, stdout="x", server_operation_count=0).may_send_stdout,
                        "an unbound intent from before the turn does not count")
        _journal(self.state, {**unbound, "ts": 300.0})
        recs = at.read_turn_records(self.state, self.turn, issued_at=200.0)
        self.assertFalse(at.classify_turn(recs, stdout="x", server_operation_count=0).may_send_stdout)

    def test_turn_gate_suppresses_stdout_when_dispatch_intent_exists(self):
        _journal(self.state, _intent(self.turn, 1))
        records = at.read_turn_records(self.state, self.turn)
        self.assertEqual(len(at.unmatched_intents(records)), 1)
        v = at.classify_turn(records, stdout="the answer")
        self.assertEqual((v.outcome, v.may_send_stdout), (at.INCOMPLETE_OR_ERROR, False))
        _journal(self.state, _outcome(self.turn, 1))
        v = self._verdict()
        self.assertEqual((v.outcome, v.may_send_stdout), (at.INCOMPLETE_OR_ERROR, False))
        _journal(self.state, _intent(self.turn, 2, status="done"),
                 _outcome(self.turn, 2, op_status="failed", requested="done"))
        self.assertFalse(self._verdict().may_send_stdout)

    def test_terminal_confirmed_never_sends_stdout(self):
        _journal(self.state, _intent(self.turn, 1, status="done"),
                 _outcome(self.turn, 1, phase="responded", terminal=True, requested="done"))
        v = self._verdict()
        self.assertEqual((v.outcome, v.may_send_stdout), (at.TERMINAL_CONFIRMED, False))

    def test_turn_gate_durably_suspended_on_waiting_input_and_202(self):
        _journal(self.state, _intent(self.turn, 1, action="room.question.ask"),
                 _outcome(self.turn, 1, phase="waiting_input", blockers=1))
        v = self._verdict()
        self.assertEqual((v.outcome, v.may_send_stdout), (at.DURABLY_SUSPENDED, False))

        state = Path(tempfile.mkdtemp())
        turn = _ctx(state)["turn_id"]
        _journal(state, _intent(turn, 1, status="done"),
                 _outcome(turn, 1, op_status="accepted", requested="done"))
        v = at.classify_turn(at.read_turn_records(state, turn), stdout="x")
        self.assertEqual((v.outcome, v.may_send_stdout), (at.DURABLY_SUSPENDED, False))

    def test_a_resolved_blocker_is_not_a_suspension(self):
        _journal(self.state, _intent(self.turn, 1, action="room.question.ask"),
                 _outcome(self.turn, 1, phase="waiting_input", blockers=1),
                 _intent(self.turn, 2), _outcome(self.turn, 2, phase="working"))
        self.assertEqual(self._verdict().outcome, at.INCOMPLETE_OR_ERROR)
        _journal(self.state, _intent(self.turn, 3), _outcome(self.turn, 3, phase="working"))
        v = self._verdict(server_state={"terminal": False, "execution_phase": "waiting_input",
                                        "active_blocker_count": 1})
        self.assertEqual(v.outcome, at.DURABLY_SUSPENDED, "the task row is authoritative")

    def test_reconciled_outcome_fills_an_unmatched_intent(self):
        _journal(self.state, _intent(self.turn, 1, status="done"))
        records = at.read_turn_records(self.state, self.turn)
        rec = {"operation_id": f"op_{self.turn}_1", "operation_status": "succeeded",
               "applied_execution_phase": "responded", "applied_terminal": True}
        self.assertEqual(at.classify_turn(records, reconciled=[rec]).outcome, at.TERMINAL_CONFIRMED)
        unknown = {**rec, "operation_status": "dispatched_unknown", "applied_terminal": False}
        v = at.classify_turn(records, stdout="x", reconciled=[unknown])
        self.assertEqual((v.outcome, v.may_send_stdout), (at.INCOMPLETE_OR_ERROR, False))

    def test_unreadable_journal_or_closed_task_fails_closed(self):
        at.room_actions_path(self.state).mkdir()
        v = self._verdict()
        self.assertEqual((v.outcome, v.may_send_stdout), (at.INCOMPLETE_OR_ERROR, False))
        os.rmdir(at.room_actions_path(self.state))
        v = self._verdict(server_state={"terminal": True, "execution_phase": "cancelled"})
        self.assertFalse(v.may_send_stdout)

    def test_rotated_journal_is_read(self):
        rotated = at.room_actions_path(self.state).with_name(at.ROOM_ACTIONS_NAME + ".1")
        rotated.write_text(json.dumps(_intent(self.turn, 1)) + "\nnot json\n")
        self.assertFalse(self._verdict().may_send_stdout)

    def test_legacy_effective_task_status_outcome(self):
        _journal(self.state, _intent(self.turn, 1, status="done"),
                 {"record_type": at.OUTCOME, "turn_id": self.turn, "seq": 1,
                  "operation_id": f"op_{self.turn}_1", "operation_status": "succeeded",
                  "effective_task_status": "done"})
        self.assertEqual(self._verdict().outcome, at.TERMINAL_CONFIRMED)


if __name__ == "__main__":
    unittest.main()
