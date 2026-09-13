#!/usr/bin/env python3
"""Unit + call-trace tests for hooks/devapp-execute-guard.py.

Run:  python3 tests/devapp-execute-guard.test.py

Exercised the way Claude Code runs it — subprocess, one hook JSON on stdin,
decision JSON on stdout (the hook-driver idiom of tests/gmail-write-guard.test.py).

`replay()` is the trace oracle: it walks a CANDIDATE tool-call trace through the
guard and returns only the calls the guard actually permitted, recording each
permitted call's result through PostToolUse first. Asserting on that returned
trace is what proves "no second execute" mechanically rather than by prose.

Error shapes are the frozen contract's, loaded from tests/fixtures/devapp-mcp/
(verbatim C0 samples; digests in that dir's DIGEST file, contract digest
sha256:9f191b8da05c352d114febc97166d215bb2369faac05c6179195e7051e285236).
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HOOK = REPO / "hooks" / "devapp-execute-guard.py"
FIXTURES = REPO / "tests" / "fixtures" / "devapp-mcp"
CATALOG = json.loads((FIXTURES / "error-catalog.json").read_text())

SERVER = "mcp__ag2-space__"
EXECUTE = SERVER + "room.action.execute"
READ = SERVER + "room.action.read"
DESCRIBE = SERVER + "room.actions.describe"
SEARCH = SERVER + "room.actions.search"
INSPECT = SERVER + "operation.inspect"

OP = "op-tasks-create-7f3a"


def envelope(name):
    """The `body` of a C0 fixture response — what the façade puts in the error."""
    return json.loads((FIXTURES / name).read_text())["body"]


def tool_error(name):
    """C0 delivery form: in-band tool error, envelope as one compact-JSON block."""
    return {"isError": True,
            "content": [{"type": "text", "text": json.dumps(envelope(name))}]}


def run_hook(payload, ledger):
    proc = subprocess.run(
        [sys.executable, str(HOOK)], input=json.dumps(payload),
        capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "SUTANDO_DEVAPP_LEDGER": str(ledger)})
    return proc


def decision(proc):
    if not proc.stdout.strip():
        return None
    return json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"]


def reason(proc):
    return json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecisionReason"]


def replay(trace, ledger):
    """Return the sub-trace the guard permitted, feeding results back as it goes."""
    allowed = []
    for call in trace:
        pre = run_hook({"hook_event_name": "PreToolUse", "tool_name": call["tool"],
                        "tool_input": call.get("input", {})}, ledger)
        if decision(pre) == "deny":
            continue
        allowed.append(call["tool"])
        if "response" in call:
            run_hook({"hook_event_name": "PostToolUse", "tool_name": call["tool"],
                      "tool_input": call.get("input", {}),
                      "tool_response": call["response"]}, ledger)
    return allowed


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Path(self.tmp.name) / "state" / "devapp-operations.json"
        self.addCleanup(self.tmp.cleanup)


class Matching(Base):
    def test_the_matched_tool_name_is_the_one_the_contract_froze(self):
        """C0 answer 17 freezes the facade names; the hook keys on the tool half.

        If this string drifts the guard silently stops guarding, so it is pinned
        against a contract fixture rather than only against the hook's own copy.
        """
        src = HOOK.read_text()
        self.assertIn('MATCHED_TOOL = "room.action.execute"', src)
        # The fixtures address the same route the tool projects.
        self.assertEqual(
            envelope("c0-devapp-action-execute-outcome-unknown.response.json")
            ["details"]["operation_id"], OP)

    def test_non_mcp_and_unrelated_mcp_tools_are_untouched(self):
        # The other eight façade tools all pass through: only execute is gated.
        for name in ("Bash", "Read", READ, DESCRIBE, SEARCH, INSPECT,
                     SERVER + "ag2.whoami", SERVER + "approval.inspect",
                     SERVER + "room.list", SERVER + "room.inspect",
                     "mcp__other__room.action.read"):
            proc = run_hook({"hook_event_name": "PreToolUse", "tool_name": name,
                             "tool_input": {"operation_id": OP}}, self.ledger)
            self.assertIsNone(decision(proc), f"{name} must pass through")
            self.assertEqual(proc.stdout.strip(), "", f"{name} must be silent")

    def test_a_server_rename_still_matches_because_we_key_on_the_tool_half(self):
        for server in ("mcp__ag2-space__", "mcp__ag2space-dev__", "mcp__AG2__"):
            self.ledger.parent.mkdir(parents=True, exist_ok=True)
            self.ledger.write_text(json.dumps(
                {OP: {"dispatch_state": "dispatched_unknown", "ts": 9e9}}))
            proc = run_hook({"hook_event_name": "PreToolUse",
                             "tool_name": server + "room.action.execute",
                             "tool_input": {"operation_id": OP}}, self.ledger)
            self.assertEqual(decision(proc), "deny", server)

    def test_an_execute_with_no_operation_id_is_not_blocked(self):
        """Missing id is the backend's business to reject, not a reason to wedge."""
        proc = run_hook({"hook_event_name": "PreToolUse", "tool_name": EXECUTE,
                         "tool_input": {"action": "devapp.app.tasks_create"}},
                        self.ledger)
        self.assertIsNone(decision(proc))


class DispatchStateRule(Base):
    def test_a_first_execute_is_always_allowed(self):
        proc = run_hook({"hook_event_name": "PreToolUse", "tool_name": EXECUTE,
                         "tool_input": {"operation_id": OP}}, self.ledger)
        self.assertIsNone(decision(proc))

    def test_resend_is_allowed_only_on_not_dispatched(self):
        """Mirrors error-catalog.json -> retry_decision, read from the fixture."""
        allowed = CATALOG["retry_decision"]["resend_execute_allowed_when"]
        forbidden = CATALOG["retry_decision"]["resend_execute_forbidden_when"]
        self.assertEqual(allowed, ["not_dispatched"])
        for state in allowed + forbidden:
            self.ledger.parent.mkdir(parents=True, exist_ok=True)
            self.ledger.write_text(json.dumps(
                {OP: {"dispatch_state": state, "next_action": "retry_later",
                      "ts": 9e9}}))
            proc = run_hook({"hook_event_name": "PreToolUse", "tool_name": EXECUTE,
                             "tool_input": {"operation_id": OP}}, self.ledger)
            if state in forbidden:
                self.assertEqual(decision(proc), "deny", state)
            else:
                self.assertIsNone(decision(proc), state)

    def test_recoverable_true_does_not_authorize_a_resend(self):
        """The tool error says recoverable:true; dispatch_state still forbids it.

        This is the whole point of the contract's `not_a_signal: recoverable`.
        """
        env = envelope("c0-devapp-action-execute-tool-error.response.json")
        self.assertTrue(env["recoverable"])
        self.assertEqual(env["details"]["dispatch_state"], "dispatched_failed")
        run_hook({"hook_event_name": "PostToolUse", "tool_name": EXECUTE,
                  "tool_input": {"operation_id": env["details"]["operation_id"]},
                  "tool_response": tool_error(
                      "c0-devapp-action-execute-tool-error.response.json")},
                 self.ledger)
        proc = run_hook({"hook_event_name": "PreToolUse", "tool_name": EXECUTE,
                         "tool_input": {"operation_id": env["details"]["operation_id"]}},
                        self.ledger)
        self.assertEqual(decision(proc), "deny")

    def test_the_envelope_is_read_out_of_the_in_band_tool_error_block(self):
        """The façade ships the envelope as compact JSON inside a text block."""
        run_hook({"hook_event_name": "PostToolUse", "tool_name": EXECUTE,
                  "tool_input": {"operation_id": OP},
                  "tool_response": tool_error(
                      "c0-devapp-action-execute-outcome-unknown.response.json")},
                 self.ledger)
        stored = json.loads(self.ledger.read_text())[OP]
        self.assertEqual(stored["dispatch_state"], "dispatched_unknown")
        self.assertEqual(stored["code"], "ACTION_OUTCOME_UNKNOWN")

    def test_a_success_is_recorded_as_dispatched_completed(self):
        run_hook({"hook_event_name": "PostToolUse", "tool_name": EXECUTE,
                  "tool_input": {"operation_id": OP},
                  "tool_response": {"content": [{"type": "text", "text": "{\"ok\":true}"}]}},
                 self.ledger)
        self.assertEqual(json.loads(self.ledger.read_text())[OP]["dispatch_state"],
                         "dispatched_completed")


class Sleeping(Base):
    def test_sleeping_is_not_dispatched_yet_still_not_retryable(self):
        """not_dispatched would permit a resend; wait_for_explicit_wake must not.

        A retry loop on a sleeping app is the shape that would pressure someone
        into waking it, which the contract forbids outright.
        """
        env = envelope("c0-devapp-action-read-sleeping.response.json")
        self.assertEqual(env["details"]["dispatch_state"], "not_dispatched")
        self.assertEqual(env["details"]["next_action"], "wait_for_explicit_wake")
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self.ledger.write_text(json.dumps(
            {OP: {"dispatch_state": "not_dispatched",
                  "next_action": "wait_for_explicit_wake", "ts": 9e9}}))
        proc = run_hook({"hook_event_name": "PreToolUse", "tool_name": EXECUTE,
                         "tool_input": {"operation_id": OP}}, self.ledger)
        self.assertEqual(decision(proc), "deny")
        self.assertIn("sleeping", reason(proc).lower())

    def test_any_wake_shaped_mcp_tool_is_refused(self):
        """C0 adds no public MCP tool, so a wake tool is off-contract by name."""
        for name in ("mcp__ag2-space__devapp.app.wake",
                     "mcp__ag2-space__room.app.wake_pod",
                     "mcp__ag2-space__devapp.mcp.unsleep"):
            proc = run_hook({"hook_event_name": "PreToolUse", "tool_name": name,
                             "tool_input": {}}, self.ledger)
            self.assertEqual(decision(proc), "deny", name)
            self.assertIn("wake", reason(proc).lower())


class Traces(Base):
    """Call traces: assert what the guard actually let through."""

    def test_sleeping_produces_no_wake_call_and_no_retry_loop(self):
        sleeping = tool_error("c0-devapp-action-read-sleeping.response.json")
        # A candidate trace in which the agent misbehaves: it retries the
        # execute three times and then tries to wake the pod.
        trace = [
            {"tool": SEARCH},
            {"tool": DESCRIBE},
            {"tool": EXECUTE, "input": {"operation_id": OP}, "response": sleeping},
            {"tool": EXECUTE, "input": {"operation_id": OP}, "response": sleeping},
            {"tool": EXECUTE, "input": {"operation_id": OP}, "response": sleeping},
            {"tool": "mcp__ag2-space__devapp.app.wake"},
        ]
        allowed = replay(trace, self.ledger)
        self.assertEqual(allowed, [SEARCH, DESCRIBE, EXECUTE])
        self.assertEqual(allowed.count(EXECUTE), 1, "no retry loop")
        self.assertNotIn("mcp__ag2-space__devapp.app.wake", allowed, "no wake call")

    def test_unknown_mutation_yields_no_second_execute_and_inspect_is_reachable(self):
        unknown = tool_error("c0-devapp-action-execute-outcome-unknown.response.json")
        trace = [
            {"tool": EXECUTE, "input": {"operation_id": OP}, "response": unknown},
            # The wrong move: resubmit under the same id.
            {"tool": EXECUTE, "input": {"operation_id": OP}},
            # The right move, and it must stay available.
            {"tool": INSPECT, "input": {"operation_id": OP}},
        ]
        allowed = replay(trace, self.ledger)
        self.assertEqual(allowed.count(EXECUTE), 1, "no second execute")
        self.assertIn(INSPECT, allowed, "operation.inspect stays reachable")
        self.assertEqual(allowed[-1], INSPECT)

    def test_a_freshly_minted_id_is_NOT_stopped_by_this_hook(self):
        """The guard's limit, pinned so nobody mistakes it for full coverage.

        It keys on operation_id, so an agent that invents a new id for the same
        work walks straight past it. Only the derivation rule (same inputs →
        same id) and Core's fingerprint close that hole.
        """
        unknown = tool_error("c0-devapp-action-execute-outcome-unknown.response.json")
        allowed = replay([
            {"tool": EXECUTE, "input": {"operation_id": OP}, "response": unknown},
            {"tool": EXECUTE, "input": {"operation_id": "op-freshly-minted"}},
        ], self.ledger)
        self.assertEqual(allowed, [EXECUTE, EXECUTE])

    def test_the_inspected_id_is_the_original_one(self):
        env = envelope("c0-devapp-action-execute-outcome-unknown.response.json")
        self.assertEqual(env["details"]["next_action"], "inspect_operation")
        # The id to inspect is carried on the failure itself, not re-derived.
        self.assertEqual(env["details"]["operation_id"], OP)

    def test_a_freshly_minted_id_for_the_same_work_is_still_caught_by_core(self):
        """The guard stops the same id; Core's fingerprint stops a new one.

        Both halves are needed, and the contract says so — record that here so a
        future change cannot quietly drop one and call the other sufficient.
        """
        binding = CATALOG["operation_binding"]
        self.assertEqual(binding["core_fingerprint"],
                         ["actor", "room_id", "action", "arguments_digest",
                          "action_revision"])
        self.assertIn("under a different operation_id", binding["duplicate_rule"])

    def test_conflict_requires_describe_before_a_new_execute(self):
        conflict = tool_error("c0-devapp-action-read-stale-revision.response.json")
        env = envelope("c0-devapp-action-read-stale-revision.response.json")
        self.assertEqual(env["code"], "CONFLICT")
        self.assertEqual(env["details"]["next_action"], "describe_again")
        # CONFLICT is not_dispatched, so the guard does NOT block the retry —
        # the ordering is the agent's to get right, and the trace shows it.
        self.assertEqual(env["details"]["dispatch_state"], "not_dispatched")
        trace = [
            {"tool": EXECUTE, "input": {"operation_id": OP}, "response": conflict},
            {"tool": DESCRIBE},
            {"tool": EXECUTE, "input": {"operation_id": "op-after-describe"}},
        ]
        allowed = replay(trace, self.ledger)
        self.assertEqual(allowed, [EXECUTE, DESCRIBE, EXECUTE])
        self.assertLess(allowed.index(DESCRIBE), len(allowed) - 1,
                        "describe precedes the new execute")


# C0.1 ships a fixture per status except these two, derived below from the real
# `pending` record with only `status` swapped.
STATUS_WITHOUT_FIXTURE = {"running", "cancelled"}

INSPECT_FIXTURE = {
    "not_started": "c0-devapp-operation-inspect-not-started.response.json",
    "pending": "c0-devapp-operation-inspect-pending.response.json",
    "succeeded": "c0-devapp-operation-inspect-succeeded.response.json",
    "failed": "c0-devapp-operation-inspect-failed.response.json",
    "unknown": "c0-devapp-operation-inspect-unknown.response.json",
}


def inspect_body(status):
    """The C0.1 `{operation: {...}, correlation_id}` body for one status."""
    name = INSPECT_FIXTURE["pending" if status in STATUS_WITHOUT_FIXTURE else status]
    doc = json.loads((FIXTURES / name).read_text())["body"]
    if status in STATUS_WITHOUT_FIXTURE:
        doc["operation"]["status"] = status
    return doc


def inspect_result(status):
    """That body delivered as the façade delivers it: one compact-JSON block."""
    return {"content": [{"type": "text", "text": json.dumps(inspect_body(status))}]}


class InspectVerdict(Base):
    """operation.inspect is the way OUT of a block, so the guard reads it."""

    def test_the_record_shape_is_the_one_c0_1_froze(self):
        """Pins the wrapper and the inner id name against the real fixtures."""
        doc = inspect_body("unknown")
        self.assertEqual(sorted(doc), ["correlation_id", "operation"])
        op = doc["operation"]
        self.assertEqual(op["operation_id"], OP, "inner id echoes the request's")
        self.assertEqual(op["status"], "unknown")
        self.assertEqual(op["dispatch_state"], "dispatched_unknown")
        self.assertIsNone(op["result"], "unknown never synthesizes a result")
        for key in ("action", "audit_id", "binding", "completed_at", "created_at",
                    "error", "retention_expires_at", "room_id", "started_at",
                    "updated_at"):
            self.assertIn(key, op)

    def test_not_started_really_did_not_dispatch(self):
        op = inspect_body("not_started")["operation"]
        self.assertEqual(op["dispatch_state"], "not_dispatched")
        self.assertIsNone(op["started_at"], "nothing started, so a resubmit is safe")

    def test_inspecting_someone_elses_operation_is_an_indistinguishable_404(self):
        """Denial must not let inspection probe rooms, so both denials match."""
        a = json.loads((FIXTURES / ("c0-devapp-operation-inspect-denied-nonmember"
                                    ".response.json")).read_text())
        b = json.loads((FIXTURES / ("c0-devapp-operation-inspect-denied-other-actor"
                                    ".response.json")).read_text())
        self.assertEqual(a, b, "the two denials are byte-identical")
        self.assertEqual(a["status"], 404)
        self.assertEqual(a["body"]["code"], "NOT_FOUND")
        self.assertNotIn("room_id", a["body"]["details"], "no room disclosure")

    def test_inspect_itself_is_never_blocked(self):
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self.ledger.write_text(json.dumps(
            {OP: {"dispatch_state": "dispatched_unknown", "ts": 9e9}}))
        proc = run_hook({"hook_event_name": "PreToolUse", "tool_name": INSPECT,
                         "tool_input": {"operation_id": OP}}, self.ledger)
        self.assertIsNone(decision(proc))

    def test_not_started_is_the_only_status_that_re_permits_a_resubmit(self):
        outcomes = {"not_started": None, "pending": "deny", "running": "deny",
                    "unknown": "deny", "succeeded": "deny", "failed": "deny",
                    "cancelled": "deny"}
        for status, expected in outcomes.items():
            self.ledger.parent.mkdir(parents=True, exist_ok=True)
            self.ledger.write_text(json.dumps(
                {OP: {"dispatch_state": "dispatched_unknown", "ts": 9e9}}))
            run_hook({"hook_event_name": "PostToolUse", "tool_name": INSPECT,
                      "tool_input": {"operation_id": OP},
                      "tool_response": inspect_result(status)}, self.ledger)
            proc = run_hook({"hook_event_name": "PreToolUse", "tool_name": EXECUTE,
                             "tool_input": {"operation_id": OP}}, self.ledger)
            self.assertEqual(decision(proc), expected, status)

    def test_pending_is_refused_as_not_proof_it_did_not_run(self):
        run_hook({"hook_event_name": "PostToolUse", "tool_name": INSPECT,
                  "tool_input": {"operation_id": OP},
                  "tool_response": inspect_result("pending")}, self.ledger)
        proc = run_hook({"hook_event_name": "PreToolUse", "tool_name": EXECUTE,
                         "tool_input": {"operation_id": OP}}, self.ledger)
        self.assertEqual(decision(proc), "deny")
        self.assertIn("not proof", reason(proc))

    def test_the_id_comes_from_the_request_not_the_operation_object(self):
        """The inner id field name is not yet re-exported; don't depend on it."""
        payload = inspect_result("not_started")
        payload["content"][0]["text"] = json.dumps(
            {"operation": {"status": "not_started"}, "correlation_id": "c"})
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self.ledger.write_text(json.dumps(
            {OP: {"dispatch_state": "dispatched_unknown", "ts": 9e9}}))
        run_hook({"hook_event_name": "PostToolUse", "tool_name": INSPECT,
                  "tool_input": {"operation_id": OP},
                  "tool_response": payload}, self.ledger)
        proc = run_hook({"hook_event_name": "PreToolUse", "tool_name": EXECUTE,
                         "tool_input": {"operation_id": OP}}, self.ledger)
        self.assertIsNone(decision(proc))

    def test_the_full_recovery_trace(self):
        unknown = tool_error("c0-devapp-action-execute-outcome-unknown.response.json")
        trace = [
            {"tool": EXECUTE, "input": {"operation_id": OP}, "response": unknown},
            {"tool": EXECUTE, "input": {"operation_id": OP}},          # refused
            {"tool": INSPECT, "input": {"operation_id": OP},
             "response": inspect_result("not_started")},
            {"tool": EXECUTE, "input": {"operation_id": OP}},          # now allowed
        ]
        allowed = replay(trace, self.ledger)
        self.assertEqual(allowed, [EXECUTE, INSPECT, EXECUTE])
        self.assertLess(allowed.index(INSPECT), len(allowed) - 1,
                        "inspect precedes the permitted resubmit")


class FailOpen(Base):
    def test_a_broken_ledger_allows_rather_than_wedges(self):
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self.ledger.write_text("{ not json")
        proc = run_hook({"hook_event_name": "PreToolUse", "tool_name": EXECUTE,
                         "tool_input": {"operation_id": OP}}, self.ledger)
        self.assertEqual(proc.returncode, 0)
        self.assertIsNone(decision(proc))

    def test_garbage_stdin_allows_rather_than_wedges(self):
        proc = subprocess.run([sys.executable, str(HOOK)], input="not json",
                              capture_output=True, text=True,
                              env={"PATH": "/usr/bin:/bin",
                                   "SUTANDO_DEVAPP_LEDGER": str(self.ledger)})
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")

    def test_the_escape_hatch_disables_the_guard(self):
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self.ledger.write_text(json.dumps(
            {OP: {"dispatch_state": "dispatched_unknown", "ts": 9e9}}))
        proc = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps({"hook_event_name": "PreToolUse", "tool_name": EXECUTE,
                              "tool_input": {"operation_id": OP}}),
            capture_output=True, text=True,
            env={"PATH": "/usr/bin:/bin", "SUTANDO_DEVAPP_LEDGER": str(self.ledger),
                 "SUTANDO_ALLOW_DEVAPP_EXECUTE_REPLAY": "1"})
        self.assertIsNone(decision(proc))


class LedgerDurability(Base):
    def test_the_ledger_outlives_the_process_that_wrote_it(self):
        """The duplicate arrives in a FRESH session, so session state is useless.

        Each run_hook call is its own process; the deny below therefore proves
        the record survived, which is the only reason this guard works at all.
        """
        run_hook({"hook_event_name": "PostToolUse", "tool_name": EXECUTE,
                  "tool_input": {"operation_id": OP},
                  "tool_response": tool_error(
                      "c0-devapp-action-execute-outcome-unknown.response.json")},
                 self.ledger)
        self.assertTrue(self.ledger.is_file())
        proc = run_hook({"hook_event_name": "PreToolUse", "tool_name": EXECUTE,
                         "tool_input": {"operation_id": OP}}, self.ledger)
        self.assertEqual(decision(proc), "deny")

    def test_expired_entries_stop_blocking(self):
        """C0 retention is 30 days; a record older than that matches nothing."""
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self.ledger.write_text(json.dumps(
            {OP: {"dispatch_state": "dispatched_unknown", "ts": 1.0}}))
        # A write prunes; then the stale entry no longer denies.
        run_hook({"hook_event_name": "PostToolUse", "tool_name": EXECUTE,
                  "tool_input": {"operation_id": "op-other"},
                  "tool_response": {"content": []}}, self.ledger)
        self.assertNotIn(OP, json.loads(self.ledger.read_text()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
