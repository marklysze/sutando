#!/usr/bin/env python3
"""devapp-execute-guard — PreToolUse/PostToolUse guard for DevApp room Actions.

Sutando's task layer is at-least-once: a task whose outcome is unknown is
re-dispatched (``src/watch-tasks-stream.sh`` handler fallback + initial sweep,
``skills/worker-pool`` died-mid-work release, the codex notifier's completion
timeout) and re-run by a FRESH session with no memory of the first attempt. A
mutation that was already dispatched would therefore be sent a second time. An
instruction cannot prevent that, because the re-run never reads the first run's
transcript — so the ledger this hook keeps lives under the WORKSPACE, not the
session.

The rules are the frozen contract's, not this hook's invention
(``contracts/devapp-mcp/v1/error-catalog.json``, digest recorded in
``tests/fixtures/devapp-mcp/DIGEST``):

  * ``retry_decision``: the resend signal is ``details.dispatch_state``, never
    ``recoverable``. An execute may be resent only when ``not_dispatched``.
  * ``next_action: wait_for_explicit_wake`` (DEVAPP_SLEEPING) is ``not_dispatched``
    but is still NOT retryable — only an explicit human wake control starts a
    pod, and ``guarantees.no_automatic_wake`` is part of the contract.
  * ``ACTION_OUTCOME_UNKNOWN`` carries ``details.operation_id`` and demands
    ``operation.inspect`` on that same id — never a new id, never a resubmit.

Scope — deliberately narrow:
  * Only MCP tools (``mcp__…``) whose trailing tool segment is exactly
    ``room.action.execute``.
  * Reads (``room.action.read``), discovery and ``operation.inspect`` are never
    blocked — inspection is the contract's way out of a blocked operation.
  * Non-matching tools: no-op (exit 0), safe under a broad matcher.

Naming risk. Claude Code renders an MCP tool as ``mcp__<server>__<tool>``. The
server half is whatever the Desktop credential bridge called the connection, so
this keys on the tool half, which C0 answer 17 freezes. Two consequences: a
second MCP server exposing a tool of that name would also be guarded
(conservative, and none exists); and if the façade ever renames the tool this
silently stops matching and stops guarding — which is why
``tests/devapp-execute-guard.test.py`` pins the string rather than trusting it.

Known limit. The ledger is keyed by ``operation_id``, so an agent that invents a
NEW id for the same work walks straight past this hook. The derivation rule
(same task inputs produce the same id) and Core's request fingerprint are what
close that, not this guard — pinned by ``test_a_freshly_minted_id_is_NOT_…``.

Escape hatch: ``SUTANDO_ALLOW_DEVAPP_EXECUTE_REPLAY=1`` disables the guard.

Fail-OPEN on any error — a crashing hook must never wedge the core (same
contract as skip-ask-user-question.py and gmail-write-guard.py).
"""
import json
import os
import sys
import time

# Matched on the tool half of `mcp__<server>__<tool>`: the server half is named
# by the Desktop bridge, the tool half is frozen by the contract.
MATCHED_TOOL = "room.action.execute"

# Watched because `not_started` is the only verdict that re-permits a resubmit;
# without it the guard would wedge the contract's own recovery path.
INSPECT_TOOL = "operation.inspect"
INSPECT_STATUS_NOT_STARTED = "not_started"
# pending/running/unknown: do not resubmit, inspect again later or report.
# succeeded/failed: final, nothing to resubmit.
INSPECT_STATUSES = {"pending", "running", "succeeded", "failed", "cancelled",
                    "unknown", "not_started"}

# C0 error-catalog.json -> retry_decision.resend_execute_forbidden_when.
RESEND_FORBIDDEN = {"dispatched_unknown", "dispatched_failed", "dispatched_completed"}

# C0 error-catalog.json -> next_actions. Sleeping is not_dispatched yet must not
# be retried: only an explicit human wake control starts the pod.
NO_AUTOMATIC_WAKE = "wait_for_explicit_wake"

# There is no wake tool in the contract (C0 answer 17: no public MCP tool is
# added). A tool that looks like one is therefore off-contract, not a shortcut.
WAKE_TOKENS = {"wake", "wakeup", "unsleep", "resume_app", "start_app"}

LEDGER_NAME = "devapp-operations.json"
# C0 operation record retention is 30 days; a ledger entry older than that can
# no longer be matched against a stored outcome, so keeping it only misleads.
RETENTION_S = 30 * 24 * 3600
MAX_ENTRIES = 2000


def _ledger_path():
    """`<workspace>/state/devapp-operations.json` via the repo's own resolver."""
    override = os.environ.get("SUTANDO_DEVAPP_LEDGER")
    if override:
        return override
    root = os.environ.get("SUTANDO_REPO_ROOT") or os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, os.path.join(root, "src"))
    from workspace_default import resolve_workspace  # noqa: E402
    return os.path.join(str(resolve_workspace(migrate=False)), "state", LEDGER_NAME)


def _load(path):
    try:
        with open(path) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}


def _save(path, data):
    """Atomic replace: a reader must never see a half-written ledger."""
    cutoff = time.time() - RETENTION_S
    data = {k: v for k, v in data.items() if float(v.get("ts") or 0) >= cutoff}
    if len(data) > MAX_ENTRIES:
        keep = sorted(data.items(), key=lambda kv: float(kv[1].get("ts") or 0))
        data = dict(keep[-MAX_ENTRIES:])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)
    return data


def tool_segment(tool_name):
    """The tool half of `mcp__<server>__<tool>`, or None for a non-MCP tool."""
    if not isinstance(tool_name, str) or not tool_name.startswith("mcp__"):
        return None
    return tool_name.rsplit("__", 1)[-1]


def is_wake_attempt(tool_name):
    """True for an MCP tool whose name reads as a wake verb (none exists)."""
    seg = tool_segment(tool_name)
    if seg is None:
        return False
    lowered = seg.lower()
    tokens = set(lowered.replace(".", "_").split("_"))
    return bool(tokens & WAKE_TOKENS)


def find_envelope(node, depth=0):
    """Locate the DevApp error envelope anywhere in a tool response.

    The façade delivers it as an in-band tool error: a compact-JSON text block,
    so it may arrive parsed, or as a string inside a content list.
    """
    if depth > 8:
        return None
    if isinstance(node, str):
        stripped = node.strip()
        if stripped.startswith("{"):
            try:
                return find_envelope(json.loads(stripped), depth + 1)
            except (ValueError, TypeError):
                return None
        return None
    if isinstance(node, dict):
        details = node.get("details")
        if isinstance(details, dict) and details.get("source") == "devapp":
            return node
        for value in node.values():
            found = find_envelope(value, depth + 1)
            if found is not None:
                return found
        return None
    if isinstance(node, list):
        for value in node:
            found = find_envelope(value, depth + 1)
            if found is not None:
                return found
    return None


def find_operation(node, depth=0):
    """Locate the `operation` object in an operation.inspect result."""
    if depth > 8:
        return None
    if isinstance(node, str):
        stripped = node.strip()
        if stripped.startswith("{"):
            try:
                return find_operation(json.loads(stripped), depth + 1)
            except (ValueError, TypeError):
                return None
        return None
    if isinstance(node, dict):
        op = node.get("operation")
        if isinstance(op, dict) and op.get("status") in INSPECT_STATUSES:
            return op
        for value in node.values():
            found = find_operation(value, depth + 1)
            if found is not None:
                return found
        return None
    if isinstance(node, list):
        for value in node:
            found = find_operation(value, depth + 1)
            if found is not None:
                return found
    return None


def decide(operation_id, ledger):
    """(deny_reason | None) for an execute carrying `operation_id`."""
    prior = ledger.get(operation_id)
    if not isinstance(prior, dict):
        return None
    status = prior.get("inspect_status")
    if status == INSPECT_STATUS_NOT_STARTED:
        # Inspection proved nothing was started: the contract permits exactly
        # this resubmit, under the same id.
        return None
    if status in ("pending", "running", "unknown"):
        return (
            f"Blocked: operation_id {operation_id!r} was inspected and is "
            f"{status!r}. That is not proof it did not run, so it must not be "
            "resubmitted — inspect it again later or report it."
        )
    if status in ("succeeded", "failed", "cancelled"):
        return (
            f"Blocked: operation_id {operation_id!r} already reached a final "
            f"state ({status!r}). Report that outcome; do not resubmit it."
        )
    state = prior.get("dispatch_state")
    if state in RESEND_FORBIDDEN:
        tail = ""
        if state == "dispatched_unknown":
            tail = (" Call operation.inspect with this same operation_id "
                    "(never a new one) and report what it says.")
        return (
            f"Blocked: operation_id {operation_id!r} was already dispatched "
            f"(dispatch_state={state!r}). The DevApp contract allows an execute "
            "to be re-sent only when dispatch_state is 'not_dispatched'; "
            "re-sending this one could apply the mutation twice." + tail
        )
    if prior.get("next_action") == NO_AUTOMATIC_WAKE:
        return (
            f"Blocked: operation_id {operation_id!r} last returned "
            "DEVAPP_SLEEPING. A sleeping app is never woken by a retry, a "
            "health probe or error recovery — only by an explicit human wake "
            "control. Stop this app task and report that the app is sleeping."
        )
    return None


def deny(reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason + " [devapp-execute-guard]",
    }}))


def main():
    if os.environ.get("SUTANDO_ALLOW_DEVAPP_EXECUTE_REPLAY", "").strip() == "1":
        return 0
    data = json.loads(sys.stdin.read())
    tool_name = str(data.get("tool_name") or "")
    event = data.get("hook_event_name") or "PreToolUse"

    if event == "PreToolUse" and is_wake_attempt(tool_name):
        deny("Blocked: the DevApp contract exposes no wake tool, and nothing in "
             "this integration may wake a pod. Only an explicit human wake "
             "control starts a sleeping app.")
        return 0

    segment = tool_segment(tool_name)
    if segment not in (MATCHED_TOOL, INSPECT_TOOL):
        return 0

    tool_input = data.get("tool_input") or {}
    operation_id = tool_input.get("operation_id") if isinstance(tool_input, dict) else None
    if not isinstance(operation_id, str) or not operation_id:
        return 0

    path = _ledger_path()
    ledger = _load(path)

    if segment == INSPECT_TOOL:
        # Never blocked, only recorded. The id comes from the REQUEST, so this
        # does not depend on the id field name inside the operation object.
        if event == "PreToolUse":
            return 0
        op = find_operation(data.get("tool_response"))
        if op is None:
            return 0
        entry = dict(ledger.get(operation_id) or {})
        entry["inspect_status"] = op.get("status")
        entry.setdefault("dispatch_state", op.get("dispatch_state"))
        entry["ts"] = time.time()
        ledger[operation_id] = entry
        _save(path, ledger)
        return 0

    if event == "PreToolUse":
        reason = decide(operation_id, ledger)
        if reason:
            deny(reason)
        return 0

    # PostToolUse / PostToolUseFailure: record what the façade reported so the
    # NEXT run — which may be a fresh session after a crash — can refuse.
    envelope = find_envelope(data.get("tool_response"))
    if envelope is None:
        entry = {"dispatch_state": "dispatched_completed", "next_action": "none"}
    else:
        details = envelope.get("details") or {}
        entry = {
            "dispatch_state": details.get("dispatch_state"),
            "next_action": details.get("next_action"),
            "code": envelope.get("code"),
        }
    entry["ts"] = time.time()
    ledger[operation_id] = entry
    _save(path, ledger)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:  # fail-open: never wedge the core on a hook error
        print(f"[devapp-execute-guard] non-fatal error, allowing: {e}", file=sys.stderr)
        sys.exit(0)
