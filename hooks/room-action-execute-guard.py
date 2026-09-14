#!/usr/bin/env python3
"""room-action-execute-guard — refuse a duplicate `room.action.execute`.

Guards `room.action.execute` on the `ag2-space` MCP connection. PreToolUse denies
an execute whose operation_id the ledger records as already dispatched, and any
wake-named tool on that connection. PostToolUse / PostToolUseFailure record the
error's `details.dispatch_state`; an operation.inspect `operation.status` of
`not_started` is the only thing that re-permits a resubmit. The ledger lives in
the workspace, not the session, because a re-dispatched task runs in a fresh
session.

Not caught here, both covered server-side: a session that dies mid-call leaves no
record (outcomes are recorded on return), and a newly minted operation_id.
Escape hatch: SUTANDO_ALLOW_ROOM_ACTION_REPLAY=1. Fail-open on any error.
"""
import fcntl
import json
import os
import sys
import tempfile
import time

# Claude Code folds `.` to `_` in MCP tool names; tool_segment() does the same.
EXECUTE_TOOL = "room_action_execute"
INSPECT_TOOL = "operation_inspect"

INSPECT_STATUS_NOT_STARTED = "not_started"
INSPECT_STATUSES = {"pending", "running", "succeeded", "failed", "cancelled",
                    "unknown", "not_started"}
RESEND_FORBIDDEN = {"dispatched_unknown", "dispatched_failed", "dispatched_completed"}
# Arrives with not_dispatched, yet only an explicit human wake may follow it.
NO_AUTOMATIC_WAKE = "wait_for_explicit_wake"
WAKE_TOKENS = {"wake", "wakeup"}
# Another server's wake tool is none of this guard's business.
WAKE_SERVER = "ag2-space"

LEDGER_NAME = "room-action-operations.json"
# The contract's operation retention; an older entry matches no stored outcome.
RETENTION_S = 30 * 24 * 3600
MAX_ENTRIES = 2000


def _ledger_path():
    """`<workspace>/state/room-action-operations.json` via the repo's own resolver."""
    override = os.environ.get("SUTANDO_ROOM_ACTION_LEDGER")
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
    except FileNotFoundError:
        return {}
    except ValueError:
        # Fail open, but loudly: every recorded dispatch is forgotten at once.
        print(f"[room-action-execute-guard] unreadable ledger {path}; starting fresh",
              file=sys.stderr)
        return {}
    return data if isinstance(data, dict) else {}


def _update(path, operation_id, make_entry):
    """Locked read-modify-write: concurrent sessions share one workspace ledger."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    with open(path + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = _load(path)
        prior = data.get(operation_id)
        entry = make_entry(prior if isinstance(prior, dict) else {})
        entry["ts"] = time.time()
        data[operation_id] = entry
        cutoff = time.time() - RETENTION_S
        data = {k: v for k, v in data.items()
                if isinstance(v, dict) and float(v.get("ts") or 0) >= cutoff}
        if len(data) > MAX_ENTRIES:
            keep = sorted(data.items(), key=lambda kv: float(kv[1].get("ts") or 0))
            data = dict(keep[-MAX_ENTRIES:])
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".room-action-operations.")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(data, fh)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def tool_segment(tool_name):
    """The tool half of `mcp__<server>__<tool>`, or None for a non-MCP tool."""
    if not isinstance(tool_name, str) or not tool_name.startswith("mcp__"):
        return None
    return tool_name.rsplit("__", 1)[-1].replace(".", "_")


def is_wake_attempt(tool_name):
    """True for a wake-named tool on the room-action connection (the contract has none)."""
    if not isinstance(tool_name, str) or not tool_name.startswith(f"mcp__{WAKE_SERVER}__"):
        return False
    return bool(set(tool_segment(tool_name).lower().split("_")) & WAKE_TOKENS)


def _find(node, pick, depth=0):
    """First non-None `pick(dict)` anywhere in a tool result, JSON text included."""
    if depth > 8:
        return None
    if isinstance(node, str):
        start, end = node.find("{"), node.rfind("}")
        if start < 0 or end < start:
            return None
        try:
            node = json.loads(node[start:end + 1])
        except ValueError:
            return None
        return _find(node, pick, depth + 1)
    if isinstance(node, dict):
        hit = pick(node)
        if hit is not None:
            return hit
        children = node.values()
    elif isinstance(node, list):
        children = node
    else:
        return None
    for child in children:
        hit = _find(child, pick, depth + 1)
        if hit is not None:
            return hit
    return None


def _envelope(d):
    details = d.get("details")
    return d if isinstance(details, dict) and "dispatch_state" in details else None


def _operation(d):
    op = d.get("operation")
    return op if isinstance(op, dict) and op.get("status") in INSPECT_STATUSES else None


def decide(operation_id, ledger):
    """(deny_reason | None) for an execute carrying `operation_id`."""
    prior = ledger.get(operation_id)
    if not isinstance(prior, dict):
        return None
    status = prior.get("inspect_status")
    if status == INSPECT_STATUS_NOT_STARTED:
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
            f"(dispatch_state={state!r}). The contract allows an execute to be "
            "re-sent only when dispatch_state is 'not_dispatched'; re-sending "
            "this one could apply the mutation twice." + tail
        )
    if prior.get("next_action") == NO_AUTOMATIC_WAKE:
        return (
            f"Blocked: operation_id {operation_id!r} last returned next_action "
            f"{NO_AUTOMATIC_WAKE!r}: the app is sleeping. A sleeping app is never "
            "woken by a retry, a health probe or error recovery — only by an "
            "explicit human wake control. Stop this app task and report that "
            "the app is sleeping."
        )
    return None


def deny(reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason + " [room-action-execute-guard]",
    }}))


def main():
    if os.environ.get("SUTANDO_ALLOW_ROOM_ACTION_REPLAY", "").strip() == "1":
        return 0
    data = json.loads(sys.stdin.read())
    tool_name = str(data.get("tool_name") or "")
    event = data.get("hook_event_name") or "PreToolUse"

    if event == "PreToolUse" and is_wake_attempt(tool_name):
        deny("Blocked: the room-action contract exposes no wake tool, and "
             "nothing here may wake an app. Only an explicit human wake "
             "control starts a sleeping app.")
        return 0

    segment = tool_segment(tool_name)
    if segment not in (EXECUTE_TOOL, INSPECT_TOOL):
        return 0

    tool_input = data.get("tool_input") or {}
    operation_id = tool_input.get("operation_id") if isinstance(tool_input, dict) else None
    if not isinstance(operation_id, str) or not operation_id:
        return 0

    path = _ledger_path()

    if segment == INSPECT_TOOL:
        # Never blocked, only recorded; the id comes from the request.
        if event != "PostToolUse":
            return 0
        op = _find(data.get("tool_response"), _operation)
        if op is None:
            return 0

        def merge(prior):
            entry = dict(prior)
            entry["inspect_status"] = op.get("status")
            entry.setdefault("dispatch_state", op.get("dispatch_state"))
            return entry

        _update(path, operation_id, merge)
        return 0

    if event == "PreToolUse":
        reason = decide(operation_id, _load(path))
        if reason:
            deny(reason)
        return 0

    if event == "PostToolUseFailure":
        # An error without an envelope (timeout, dropped connection) may still have run.
        envelope, fallback = _find(data.get("error"), _envelope), "dispatched_unknown"
    elif event == "PostToolUse":
        envelope, fallback = _find(data.get("tool_response"), _envelope), "dispatched_completed"
    else:
        return 0
    if envelope is None:
        entry = {"dispatch_state": fallback}
    else:
        details = envelope.get("details") or {}
        entry = {
            "dispatch_state": details.get("dispatch_state"),
            "next_action": details.get("next_action"),
            "code": envelope.get("code"),
        }
    _update(path, operation_id, lambda _prior: entry)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:  # fail-open: never wedge the core on a hook error
        print(f"[room-action-execute-guard] non-fatal error, allowing: {e}", file=sys.stderr)
        sys.exit(0)
