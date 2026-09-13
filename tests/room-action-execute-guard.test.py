#!/usr/bin/env python3
"""Unit + call-trace tests for hooks/room-action-execute-guard.py.

Run:  python3 tests/room-action-execute-guard.test.py

Exercised the way Claude Code runs it — subprocess, one hook JSON on stdin,
decision JSON on stdout (the hook-driver idiom of tests/gmail-write-guard.test.py).
Payload shapes are the ones Claude Code 2.1.270 sent to a hook for a real MCP
server: tool names with dots folded to `_`, an in-band tool error delivered as
PostToolUseFailure `error` text, a success as PostToolUse `tool_response` blocks.

`replay()` is the trace oracle: it walks a CANDIDATE tool-call trace through the
guard and returns only the calls the guard permitted, recording each permitted
call's result first. Asserting on that trace proves "no second execute".

Error shapes are the frozen contract's: verbatim samples vendored under
tests/fixtures/ (digests in that directory's DIGEST file).
"""
import json
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HOOK = REPO / "hooks" / "room-action-execute-guard.py"
FIXTURES = REPO / "tests" / "fixtures" / "devapp-mcp"
CATALOG = json.loads((FIXTURES / "error-catalog.json").read_text())

SERVER = "mcp__ag2-space__"
EXECUTE = SERVER + "room_action_execute"
READ = SERVER + "room_action_read"
DESCRIBE = SERVER + "room_actions_describe"
SEARCH = SERVER + "room_actions_search"
INSPECT = SERVER + "operation_inspect"
WAKE = SERVER + "devapp_app_wake"

OP = "op-tasks-create-7f3a"

# Recorded verbatim from Claude Code 2.1.270 calling a stdio MCP server whose
# `room.action.execute` returned an ACTION_OUTCOME_UNKNOWN tool error.
RECORDED_FAILURE = {
    "hook_event_name": "PostToolUseFailure",
    "tool_name": "mcp__ag2-space__room_action_execute",
    "tool_input": {"room_id": "!r:x", "operation_id": "op-probe-1"},
    "error": "{\"code\": \"ACTION_OUTCOME_UNKNOWN\", \"message\": \"outcome unknown\", "
             "\"recoverable\": false, \"details\": {\"source\": \"devapp\", "
             "\"dispatch_state\": \"dispatched_unknown\", \"next_action\": "
             "\"inspect_operation\", \"operation_id\": \"op-probe-1\", "
             "\"correlation_id\": \"c1\"}}",
    "is_interrupt": False,
}


def envelope(name):
    """The `body` of a C0 fixture response — what the façade puts in the error."""
    return json.loads((FIXTURES / name).read_text())["body"]


def tool_error(name):
    """PostToolUseFailure `error`: the façade's one compact-JSON text block."""
    return json.dumps(envelope(name))


def blocks(payload):
    """PostToolUse `tool_response` for an MCP success: the content blocks."""
    return [{"type": "text", "text": json.dumps(payload)}]


def run_hook(payload, ledger):
    return subprocess.run(
        [sys.executable, str(HOOK)], input=json.dumps(payload),
        capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "SUTANDO_ROOM_ACTION_LEDGER": str(ledger)})


def pre(tool, ledger, operation_id=OP):
    return run_hook({"hook_event_name": "PreToolUse", "tool_name": tool,
                     "tool_input": {"operation_id": operation_id}}, ledger)


def post(tool, ledger, response, operation_id=OP):
    return run_hook({"hook_event_name": "PostToolUse", "tool_name": tool,
                     "tool_input": {"operation_id": operation_id},
                     "tool_response": response}, ledger)


def failure(tool, ledger, error, operation_id=OP):
    return run_hook({"hook_event_name": "PostToolUseFailure", "tool_name": tool,
                     "tool_input": {"operation_id": operation_id},
                     "error": error, "is_interrupt": False}, ledger)


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
        tool_input = call.get("input", {})
        proc = run_hook({"hook_event_name": "PreToolUse", "tool_name": call["tool"],
                         "tool_input": tool_input}, ledger)
        if decision(proc) == "deny":
            continue
        allowed.append(call["tool"])
        if "error" in call:
            run_hook({"hook_event_name": "PostToolUseFailure", "tool_name": call["tool"],
                      "tool_input": tool_input, "error": call["error"]}, ledger)
        elif "response" in call:
            run_hook({"hook_event_name": "PostToolUse", "tool_name": call["tool"],
                      "tool_input": tool_input, "tool_response": call["response"]}, ledger)
    return allowed


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Path(self.tmp.name) / "state" / "room-action-operations.json"
        self.addCleanup(self.tmp.cleanup)

    def seed(self, entries):
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self.ledger.write_text(json.dumps(entries))


class Matching(Base):
    def test_the_matched_tool_name_is_the_one_claude_code_emits(self):
        """If this string drifts the guard silently stops guarding."""
        self.assertIn('EXECUTE_TOOL = "room_action_execute"', HOOK.read_text())
        self.assertEqual(RECORDED_FAILURE["tool_name"], EXECUTE)

    def test_the_recorded_cli_failure_payload_is_recorded_and_then_denies(self):
        run_hook(RECORDED_FAILURE, self.ledger)
        stored = json.loads(self.ledger.read_text())["op-probe-1"]
        self.assertEqual(stored["dispatch_state"], "dispatched_unknown")
        self.assertEqual(stored["code"], "ACTION_OUTCOME_UNKNOWN")
        self.assertEqual(decision(pre(EXECUTE, self.ledger, "op-probe-1")), "deny")

    def test_non_mcp_and_unrelated_mcp_tools_are_untouched(self):
        for name in ("Bash", "Read", READ, DESCRIBE, SEARCH, INSPECT,
                     SERVER + "ag2_whoami", SERVER + "approval_inspect",
                     SERVER + "room_list", SERVER + "room_inspect",
                     "mcp__other__room_action_read"):
            proc = pre(name, self.ledger)
            self.assertIsNone(decision(proc), f"{name} must pass through")
            self.assertEqual(proc.stdout.strip(), "", f"{name} must be silent")

    def test_any_server_name_and_the_dotted_form_still_match(self):
        for tool in ("mcp__ag2-space__room_action_execute",
                     "mcp__ag2space-dev__room_action_execute",
                     "mcp__ag2-space__room.action.execute"):
            self.seed({OP: {"dispatch_state": "dispatched_unknown", "ts": 9e9}})
            self.assertEqual(decision(pre(tool, self.ledger)), "deny", tool)

    def test_an_execute_with_no_operation_id_is_not_blocked(self):
        """Missing id is the backend's business to reject, not a reason to wedge."""
        proc = run_hook({"hook_event_name": "PreToolUse", "tool_name": EXECUTE,
                         "tool_input": {"action": "devapp.app.tasks_create"}},
                        self.ledger)
        self.assertIsNone(decision(proc))


class DispatchStateRule(Base):
    def test_a_first_execute_is_always_allowed(self):
        self.assertIsNone(decision(pre(EXECUTE, self.ledger)))

    def test_resend_is_allowed_only_on_not_dispatched(self):
        """Mirrors error-catalog.json -> retry_decision, read from the fixture."""
        allowed = CATALOG["retry_decision"]["resend_execute_allowed_when"]
        forbidden = CATALOG["retry_decision"]["resend_execute_forbidden_when"]
        self.assertEqual(allowed, ["not_dispatched"])
        for state in allowed + forbidden:
            self.seed({OP: {"dispatch_state": state, "next_action": "retry_later",
                            "ts": 9e9}})
            if state in forbidden:
                self.assertEqual(decision(pre(EXECUTE, self.ledger)), "deny", state)
            else:
                self.assertIsNone(decision(pre(EXECUTE, self.ledger)), state)

    def test_recoverable_true_does_not_authorize_a_resend(self):
        """The contract's `not_a_signal: recoverable`: dispatch_state decides."""
        env = envelope("c0-devapp-action-execute-tool-error.response.json")
        self.assertTrue(env["recoverable"])
        self.assertEqual(env["details"]["dispatch_state"], "dispatched_failed")
        op = env["details"]["operation_id"]
        failure(EXECUTE, self.ledger,
                tool_error("c0-devapp-action-execute-tool-error.response.json"), op)
        self.assertEqual(decision(pre(EXECUTE, self.ledger, op)), "deny")

    def test_a_success_is_recorded_as_dispatched_completed(self):
        post(EXECUTE, self.ledger, blocks({"ok": True}))
        self.assertEqual(json.loads(self.ledger.read_text())[OP]["dispatch_state"],
                         "dispatched_completed")

    def test_a_failure_without_an_envelope_is_recorded_as_unknown(self):
        """A client-side timeout or dropped connection may still have run."""
        failure(EXECUTE, self.ledger, "MCP error -32001: Request timed out")
        self.assertEqual(json.loads(self.ledger.read_text())[OP]["dispatch_state"],
                         "dispatched_unknown")
        proc = pre(EXECUTE, self.ledger)
        self.assertEqual(decision(proc), "deny")
        self.assertIn("operation.inspect", reason(proc))


class Sleeping(Base):
    def test_sleeping_is_not_dispatched_yet_still_not_retryable(self):
        """not_dispatched would permit a resend; wait_for_explicit_wake must not."""
        env = envelope("c0-devapp-action-read-sleeping.response.json")
        self.assertEqual(env["details"]["dispatch_state"], "not_dispatched")
        self.assertEqual(env["details"]["next_action"], "wait_for_explicit_wake")
        failure(EXECUTE, self.ledger,
                tool_error("c0-devapp-action-read-sleeping.response.json"))
        proc = pre(EXECUTE, self.ledger)
        self.assertEqual(decision(proc), "deny")
        self.assertIn("sleeping", reason(proc).lower())

    def test_any_wake_shaped_mcp_tool_is_refused(self):
        """The contract adds no wake tool, so one is off-contract by name."""
        for name in (WAKE, SERVER + "room_app_wake_pod",
                     SERVER + "devapp.app.wakeup"):
            proc = run_hook({"hook_event_name": "PreToolUse", "tool_name": name,
                             "tool_input": {}}, self.ledger)
            self.assertEqual(decision(proc), "deny", name)
            self.assertIn("wake", reason(proc).lower())


class Traces(Base):
    """Call traces: assert what the guard actually let through."""

    def test_sleeping_produces_no_wake_call_and_no_retry_loop(self):
        sleeping = tool_error("c0-devapp-action-read-sleeping.response.json")
        # A misbehaving candidate: three executes, then a wake attempt.
        trace = [
            {"tool": SEARCH},
            {"tool": DESCRIBE},
            {"tool": EXECUTE, "input": {"operation_id": OP}, "error": sleeping},
            {"tool": EXECUTE, "input": {"operation_id": OP}, "error": sleeping},
            {"tool": EXECUTE, "input": {"operation_id": OP}, "error": sleeping},
            {"tool": WAKE},
        ]
        allowed = replay(trace, self.ledger)
        self.assertEqual(allowed, [SEARCH, DESCRIBE, EXECUTE])

    def test_unknown_mutation_yields_no_second_execute_and_inspect_is_reachable(self):
        unknown = tool_error("c0-devapp-action-execute-outcome-unknown.response.json")
        trace = [
            {"tool": EXECUTE, "input": {"operation_id": OP}, "error": unknown},
            {"tool": EXECUTE, "input": {"operation_id": OP}},
            {"tool": INSPECT, "input": {"operation_id": OP}},
        ]
        allowed = replay(trace, self.ledger)
        self.assertEqual(allowed, [EXECUTE, INSPECT])

    def test_a_freshly_minted_id_is_NOT_stopped_by_this_hook(self):
        """The guard's limit: keyed on operation_id, so a new id walks past it."""
        unknown = tool_error("c0-devapp-action-execute-outcome-unknown.response.json")
        allowed = replay([
            {"tool": EXECUTE, "input": {"operation_id": OP}, "error": unknown},
            {"tool": EXECUTE, "input": {"operation_id": "op-freshly-minted"}},
        ], self.ledger)
        self.assertEqual(allowed, [EXECUTE, EXECUTE])

    def test_the_inspected_id_is_the_original_one(self):
        env = envelope("c0-devapp-action-execute-outcome-unknown.response.json")
        self.assertEqual(env["details"]["next_action"], "inspect_operation")
        self.assertEqual(env["details"]["operation_id"], OP)

    def test_a_freshly_minted_id_for_the_same_work_is_still_caught_by_core(self):
        """The guard stops the same id; Core's fingerprint stops a new one."""
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
        # not_dispatched, so the guard does not block; the ordering is the agent's.
        self.assertEqual(env["details"]["dispatch_state"], "not_dispatched")
        trace = [
            {"tool": EXECUTE, "input": {"operation_id": OP}, "error": conflict},
            {"tool": DESCRIBE},
            {"tool": EXECUTE, "input": {"operation_id": "op-after-describe"}},
        ]
        self.assertEqual(replay(trace, self.ledger), [EXECUTE, DESCRIBE, EXECUTE])


INSPECT_STATUSES = ("not_started", "pending", "running", "succeeded", "failed",
                    "cancelled", "unknown")


def inspect_body(status):
    """The `{operation: {...}, correlation_id}` body of that status's C0 fixture."""
    name = f"c0-devapp-operation-inspect-{status.replace('_', '-')}.response.json"
    return json.loads((FIXTURES / name).read_text())["body"]


class InspectVerdict(Base):
    """operation.inspect is the way OUT of a block, so the guard reads it."""

    def test_the_record_shape_is_the_one_c0_1_froze(self):
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

    def test_every_status_has_a_real_fixture_echoing_its_status(self):
        for status in INSPECT_STATUSES:
            op = inspect_body(status)["operation"]
            self.assertEqual(op["status"], status)
            self.assertEqual(op["operation_id"], OP)

    def test_not_started_really_did_not_dispatch(self):
        op = inspect_body("not_started")["operation"]
        self.assertEqual(op["dispatch_state"], "not_dispatched")
        self.assertIsNone(op["started_at"], "nothing started, so a resubmit is safe")

    def test_pending_is_recorded_but_unsent_and_running_is_in_flight(self):
        """Pending sent nothing yet, but may start any moment, so it stays refused."""
        self.assertEqual(inspect_body("pending")["operation"]["dispatch_state"],
                         "not_dispatched")
        self.assertEqual(inspect_body("running")["operation"]["dispatch_state"],
                         "dispatched_unknown")
        self.assertEqual(inspect_body("cancelled")["operation"]["dispatch_state"],
                         "not_dispatched")

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
        self.seed({OP: {"dispatch_state": "dispatched_unknown", "ts": 9e9}})
        self.assertIsNone(decision(pre(INSPECT, self.ledger)))

    def test_not_started_is_the_only_status_that_re_permits_a_resubmit(self):
        outcomes = {"not_started": None, "pending": "deny", "running": "deny",
                    "unknown": "deny", "succeeded": "deny", "failed": "deny",
                    "cancelled": "deny"}
        for status, expected in outcomes.items():
            self.seed({OP: {"dispatch_state": "dispatched_unknown", "ts": 9e9}})
            post(INSPECT, self.ledger, blocks(inspect_body(status)))
            self.assertEqual(decision(pre(EXECUTE, self.ledger)), expected, status)

    def test_pending_is_refused_as_not_proof_it_did_not_run(self):
        post(INSPECT, self.ledger, blocks(inspect_body("pending")))
        proc = pre(EXECUTE, self.ledger)
        self.assertEqual(decision(proc), "deny")
        self.assertIn("not proof", reason(proc))

    def test_the_id_comes_from_the_request_not_the_operation_object(self):
        self.seed({OP: {"dispatch_state": "dispatched_unknown", "ts": 9e9}})
        post(INSPECT, self.ledger,
             blocks({"operation": {"status": "not_started"}, "correlation_id": "c"}))
        self.assertIsNone(decision(pre(EXECUTE, self.ledger)))

    def test_the_full_recovery_trace(self):
        unknown = tool_error("c0-devapp-action-execute-outcome-unknown.response.json")
        trace = [
            {"tool": EXECUTE, "input": {"operation_id": OP}, "error": unknown},
            {"tool": EXECUTE, "input": {"operation_id": OP}},          # refused
            {"tool": INSPECT, "input": {"operation_id": OP},
             "response": blocks(inspect_body("not_started"))},
            {"tool": EXECUTE, "input": {"operation_id": OP}},          # now allowed
        ]
        self.assertEqual(replay(trace, self.ledger), [EXECUTE, INSPECT, EXECUTE])


class FailOpen(Base):
    def test_a_broken_ledger_allows_rather_than_wedges(self):
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self.ledger.write_text("{ not json")
        proc = pre(EXECUTE, self.ledger)
        self.assertEqual(proc.returncode, 0)
        self.assertIsNone(decision(proc))

    def test_a_broken_ledger_is_rebuilt_by_the_next_write(self):
        """Otherwise one torn file would disable the guard for good."""
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self.ledger.write_text("{ not json")
        failure(EXECUTE, self.ledger,
                tool_error("c0-devapp-action-execute-outcome-unknown.response.json"))
        self.assertEqual(decision(pre(EXECUTE, self.ledger)), "deny")

    def test_garbage_stdin_allows_rather_than_wedges(self):
        proc = subprocess.run([sys.executable, str(HOOK)], input="not json",
                              capture_output=True, text=True,
                              env={"PATH": "/usr/bin:/bin",
                                   "SUTANDO_ROOM_ACTION_LEDGER": str(self.ledger)})
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")

    def test_the_escape_hatch_disables_the_guard(self):
        self.seed({OP: {"dispatch_state": "dispatched_unknown", "ts": 9e9}})
        proc = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps({"hook_event_name": "PreToolUse", "tool_name": EXECUTE,
                              "tool_input": {"operation_id": OP}}),
            capture_output=True, text=True,
            env={"PATH": "/usr/bin:/bin", "SUTANDO_ROOM_ACTION_LEDGER": str(self.ledger),
                 "SUTANDO_ALLOW_ROOM_ACTION_REPLAY": "1"})
        self.assertIsNone(decision(proc))


class LedgerDurability(Base):
    def test_the_ledger_outlives_the_process_that_wrote_it(self):
        """The duplicate arrives in a FRESH session, so session state is useless."""
        failure(EXECUTE, self.ledger,
                tool_error("c0-devapp-action-execute-outcome-unknown.response.json"))
        self.assertTrue(self.ledger.is_file())
        self.assertEqual(decision(pre(EXECUTE, self.ledger)), "deny")

    def test_expired_entries_stop_blocking(self):
        """C0 retention is 30 days; a record older than that matches nothing."""
        self.seed({OP: {"dispatch_state": "dispatched_unknown", "ts": 1.0}})
        post(EXECUTE, self.ledger, blocks({"ok": True}), "op-other")
        self.assertNotIn(OP, json.loads(self.ledger.read_text()))

    def test_concurrent_writers_lose_no_entry(self):
        """Parallel sessions share one workspace ledger; each record must survive."""
        ids = [f"op-{i}" for i in range(16)]
        unknown = tool_error("c0-devapp-action-execute-outcome-unknown.response.json")
        with ThreadPoolExecutor(max_workers=len(ids)) as pool:
            list(pool.map(lambda op: failure(EXECUTE, self.ledger, unknown, op), ids))
        self.assertEqual(sorted(json.loads(self.ledger.read_text())), sorted(ids))
        leftovers = [p.name for p in self.ledger.parent.iterdir()
                     if p.name.startswith(".room-action-operations.")]
        self.assertEqual(leftovers, [], "no temp file left behind")


if __name__ == "__main__":
    unittest.main(verbosity=2)
