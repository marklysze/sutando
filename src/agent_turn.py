"""Per-invocation turn context and the 3-outcome turn gate for relay tasks.

A turn is one attempt at one task: `turn_id = <task_id>_g<execution_generation>_<ulid>`.
Its context lives in an immutable `<state>/turns/<turn_id>.json` (mode 0400) that the
MCP proxy reads to stamp `operation_id = op_<turn_id>_<seq>` on every mutating call;
the proxy journals each call to `<state>/room-actions.jsonl` as a `dispatch_intent`
before the HTTP request and a `dispatch_outcome` after it.

At turn exit `classify_turn` reads those records and decides:
  terminal_confirmed   — a succeeded operation applied a terminal task state;
  durably_suspended    — the latest applied state is an open HITL blocker, or a
                         terminal-intent operation is still running (202);
  incomplete_or_error  — anything else. The plain-text answer may be sent as the
                         reply ONLY when the journal is readable, holds no intent for
                         the turn nor any unbound intent since it began, and core-api
                         confirms zero operations for the turn.

Stdlib only, so the ag2-sparrow package can vendor it byte-identical.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

SCHEMA_VERSION = 1
TURN_CONTEXT_ENV = "AG2_TURN_CONTEXT_FILE"
TURNS_DIRNAME = "turns"
ROOM_ACTIONS_NAME = "room-actions.jsonl"

TERMINAL_CONFIRMED = "terminal_confirmed"
DURABLY_SUSPENDED = "durably_suspended"
INCOMPLETE_OR_ERROR = "incomplete_or_error"

INTENT = "dispatch_intent"
OUTCOME = "dispatch_outcome"

TERMINAL_PHASES = frozenset({"responded", "suppressed", "consolidated", "completed",
                             "no_action_needed", "failed", "cancelled", "dead"})
TERMINAL_STATUSES = frozenset({"done", "no_action", "consolidated", "failed", "cancelled"})
TERMINAL_INTENT_STATUSES = frozenset({"done", "no_action"})
ASYNC_STATUSES = frozenset({"accepted", "running"})

_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")
_TURN_RE = re.compile(r"[A-Za-z0-9._-]{1,160}")
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _ulid(now_ms: int | None = None) -> str:
    ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    n = (ms << 80) | secrets.randbits(80)
    return "".join(_CROCKFORD[(n >> (5 * i)) & 31] for i in reversed(range(26)))


def new_turn_id(task_id: str, execution_generation: int, now_ms: int | None = None) -> str:
    if not _ID_RE.fullmatch(str(task_id)):
        raise ValueError(f"invalid task_id: {task_id!r}")
    gen = int(execution_generation)
    if gen < 1:
        raise ValueError(f"execution_generation must be >= 1, got {gen}")
    return f"{task_id}_g{gen}_{_ulid(now_ms)}"


def turns_dir(state_dir: Path) -> Path:
    return Path(state_dir) / TURNS_DIRNAME


def room_actions_path(state_dir: Path) -> Path:
    return Path(state_dir) / ROOM_ACTIONS_NAME


def _turn_path(state_dir: Path, turn_id: str, suffix: str) -> Path:
    if not _TURN_RE.fullmatch(str(turn_id)):
        raise ValueError(f"invalid turn_id: {turn_id!r}")
    return turns_dir(state_dir) / f"{turn_id}{suffix}"


def build_turn_context(*, task_id: str, execution_generation: int, room_id: str | None = None,
                       thread_root_id: str | None = None, source_event_id: str | None = None,
                       agent_mxid: str | None = None, worker_id: str | None = None,
                       source_kind: str | None = None, state_dir: Path,
                       now_ms: int | None = None) -> dict:
    ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    return {
        "schema_version": SCHEMA_VERSION,
        "turn_id": new_turn_id(task_id, execution_generation, ms),
        "task_id": str(task_id),
        "execution_generation": int(execution_generation),
        "worker_id": worker_id,
        "room_id": room_id,
        "thread_root_id": thread_root_id,
        "source_event_id": source_event_id,
        "agent_mxid": agent_mxid,
        "source_kind": source_kind,
        "created_at_ms": ms,
        "issued_at": ms / 1000,
        "room_actions_file": str(room_actions_path(state_dir).resolve()),
    }


def write_turn_context(state_dir: Path, context: dict) -> Path:
    """Publish the context read-only; refuses to replace an existing turn file."""
    path = _turn_path(state_dir, context["turn_id"], ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.stem}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(context, fh, sort_keys=True)
            fh.flush()
            os.fchmod(fh.fileno(), 0o400)
            os.fsync(fh.fileno())
        # link() fails on an existing name where rename() would silently replace it
        os.link(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
    _fsync_dir(path.parent)
    return path


def _fsync_dir(d: Path) -> None:
    try:
        fd = os.open(d, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def child_env(context_path: Path, base: dict | None = None) -> dict:
    env = dict(os.environ if base is None else base)
    env[TURN_CONTEXT_ENV] = str(Path(context_path).resolve())
    return env


def read_turn_context(path: Path) -> dict | None:
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("turn_id") else None


def is_closed(state_dir: Path, turn_id: str) -> bool:
    return _turn_path(state_dir, turn_id, ".closed").exists()


def open_turn_for_task(state_dir: Path, task_id: str) -> Path | None:
    """The newest unclosed turn file for a task — how a long-lived proxy finds its turn."""
    if not _ID_RE.fullmatch(str(task_id)):
        return None
    best: tuple[int, str, Path] | None = None
    for p in turns_dir(state_dir).glob(f"{task_id}_g*_*.json"):
        m = re.fullmatch(re.escape(task_id) + r"_g(\d+)_([0-9A-Z]{26})", p.stem)
        if not m or is_closed(state_dir, p.stem):
            continue
        key = (int(m.group(1)), m.group(2), p)
        if best is None or key[:2] > best[:2]:
            best = key
    return best[2] if best else None


def close_turn(state_dir: Path, turn_id: str) -> None:
    """Fence the turn: later calls carrying it are refused, and only its own file goes."""
    closed = _turn_path(state_dir, turn_id, ".closed")
    closed.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(closed, os.O_WRONLY | os.O_CREAT, 0o400)
    os.close(fd)
    try:
        _turn_path(state_dir, turn_id, ".json").unlink()
    except FileNotFoundError:
        pass


@dataclass(frozen=True)
class TurnRecords:
    intents: list
    outcomes: list
    readable: bool
    unbound_intents: tuple = ()


def read_turn_records(state_dir: Path, turn_id: str, issued_at: float | None = None) -> TurnRecords:
    """Both record types for one turn across the rotated and live journal, plus any
    intent the proxy could not bind to a turn since `issued_at` (None: all of them)."""
    path = room_actions_path(state_dir)
    intents, outcomes, unbound, readable = [], [], [], True
    for p in (path.with_name(path.name + ".1"), path):
        try:
            text = p.read_text()
        except FileNotFoundError:
            continue
        except OSError:
            readable = False
            continue
        for line in text.splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            if rec.get("turn_id") is None and rec.get("record_type") == INTENT:
                ts = rec.get("ts")
                if issued_at is None or not isinstance(ts, (int, float)) or ts >= issued_at:
                    unbound.append(rec)
                continue
            if rec.get("turn_id") != turn_id:
                continue
            if rec.get("record_type") == INTENT:
                intents.append(rec)
            elif rec.get("record_type") == OUTCOME:
                outcomes.append(rec)
    return TurnRecords(intents, outcomes, readable, tuple(unbound))


def unmatched_intents(records: TurnRecords) -> list:
    """Intents whose server outcome is unknown and must be reconciled with core-api."""
    known = {o.get("operation_id") for o in records.outcomes
             if o.get("operation_status") not in (None, "dispatched_unknown")}
    return [i for i in records.intents if i.get("operation_id") not in known]


@dataclass(frozen=True)
class TurnVerdict:
    outcome: str
    may_send_stdout: bool
    reason: str
    intent_count: int


def _applied_terminal(o: dict) -> bool:
    if "applied_terminal" in o:
        return o.get("applied_terminal") is True
    if o.get("applied_execution_phase") is not None:
        return o.get("applied_execution_phase") in TERMINAL_PHASES
    return o.get("effective_task_status") in TERMINAL_STATUSES


def _seq(rec: dict) -> int:
    try:
        return int(rec.get("seq") or 0)
    except (TypeError, ValueError):
        return 0


def classify_turn(records: TurnRecords, *, stdout: str = "", reconciled: Iterable[dict] = (),
                  server_state: dict | None = None,
                  server_operation_count: int | None = None) -> TurnVerdict:
    """`reconciled`: core-api outcomes for unmatched intents. `server_state`: the task row.
    `server_operation_count`: core-api operations for (task_id, turn_id); None is unknown."""
    by_op: dict = {}
    for o in sorted(records.outcomes, key=_seq):
        by_op[o.get("operation_id")] = o
    intent_by_op = {i.get("operation_id"): i for i in records.intents}
    for r in reconciled:
        op = r.get("operation_id")
        merged = {**intent_by_op.get(op, {}), **by_op.get(op, {}), **r}
        merged["record_type"] = OUTCOME
        by_op[op] = merged
    outcomes = sorted(by_op.values(), key=_seq)
    n = len(records.intents)

    if any(o.get("operation_status") == "succeeded" and _applied_terminal(o) for o in outcomes):
        return TurnVerdict(TERMINAL_CONFIRMED, False, "terminal operation succeeded", n)

    latest = outcomes[-1] if outcomes else {}
    if server_state is not None:
        terminal, phase = server_state.get("terminal") is True, server_state.get("execution_phase")
        blockers = server_state.get("active_blocker_count")
    else:
        terminal, phase = _applied_terminal(latest), latest.get("applied_execution_phase")
        blockers = latest.get("active_blocker_count")
    if not terminal and phase == "waiting_input" and int(blockers or 0) > 0:
        return TurnVerdict(DURABLY_SUSPENDED, False, "open HITL blocker", n)

    terminal_intents = [o for o in outcomes
                        if o.get("requested_task_status") in TERMINAL_INTENT_STATUSES]
    if terminal_intents and terminal_intents[-1].get("operation_status") in ASYNC_STATUSES:
        return TurnVerdict(DURABLY_SUSPENDED, False, "terminal operation still running", n)

    if terminal and server_state is not None:
        return TurnVerdict(INCOMPLETE_OR_ERROR, False, "task already closed on server", n)
    if not records.readable:
        return TurnVerdict(INCOMPLETE_OR_ERROR, False, "journal unreadable", n)
    if n:
        return TurnVerdict(INCOMPLETE_OR_ERROR, False, "room actions attempted without a terminal", n)
    if records.unbound_intents:
        return TurnVerdict(INCOMPLETE_OR_ERROR, False, "unbound room actions during the turn", n)
    if server_operation_count != 0:
        return TurnVerdict(INCOMPLETE_OR_ERROR, False, "server operations not confirmed absent", n)
    if not stdout.strip():
        return TurnVerdict(INCOMPLETE_OR_ERROR, False, "no output", n)
    return TurnVerdict(INCOMPLETE_OR_ERROR, True, "text-only turn", n)
