"""One-time upgrade of relay task-attempt records written before owed work carried its generation.

Kept apart from `task_attempts`, the live writer: the bridge runs `migrate` once at start, under the
attempts lock, before anything settles or sweeps the ledger. Until a record is migrated the live
code holds it untouched (`task_attempts._owes`).

The earlier bridge queued a cancel control first and named it in the record after, and named none
for a closed task it asked to stop, so a control can already be live with no record of it. Each
one still queued or claimed is found here and either adopted, when the record's own state explains
it, or retired: withdrawn while untaken, else watched as taken.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Callable

from . import task_attempts
from .task_attempts import SCHEMA, TaskAttempts

CLOSED_FENCES = ("task_already_terminal", "task_not_found", "task_cancelled")
_TARGET = re.compile(r"^task: CANCEL_INSTRUCTION: stop processing (\S+) if still in flight", re.M)
_CTL_NAME = re.compile(r"^(task-ctl-[A-Za-z0-9-]+)(\.txt|\.assigned-.+|\.claimed-.+)$")


def legacy_controls(tasks_dir: Path) -> "dict[str, dict[str, bool]] | None":
    """{task id the control stops: {control id: still queued}} for every relay control not yet
    answered; None when the queue cannot be read."""
    found: "dict[str, dict[str, bool]]" = {}
    try:
        names = sorted(os.listdir(tasks_dir))
    except FileNotFoundError:
        return found
    except OSError:
        return None
    for name in names:
        m = _CTL_NAME.match(name)
        if not m:
            continue
        try:
            text = (tasks_dir / name).read_text(encoding="utf-8")
        except FileNotFoundError:
            continue                            # taken between the listing and the read
        except (OSError, ValueError):
            return None
        target = _TARGET.search(text)
        if f"source: {task_attempts.CONTROL_SOURCE}\n" in text and target:
            found.setdefault(target.group(1), {})[m.group(1)] = m.group(2) == ".txt"
    return found


def migrate(attempts: TaskAttempts, tasks_dir: Path, *, local_tid: Callable[[str], str],
            new_ctl: Callable[[], str], failure_of: Callable[[str], "tuple[str, list | None]"],
            log: Callable[[str], None]) -> "bool | None":
    """Upgrade every record without the current schema, once. True when the ledger is current
    (nothing to do included), False when the upgrade did not commit, None when the ledger or the
    queue cannot be read. Never raises: a record not upgraded stays held, and the next start retries.

    `failure_of(wire_tid)` is the failure body a lost attempt owes and the identity of the result
    file it already wrote, if that file is still live."""
    with attempts.lock:
        data = attempts._read_json(attempts.path)
        if data is None:
            log("attempt ledger unreadable — not migrated")
            return None
        old = [t for t, r in data.items() if isinstance(r, dict) and r.get("schema") != SCHEMA]
        if not old:
            return True
        controls = legacy_controls(tasks_dir)
        if controls is None:
            log("task queue unreadable — attempt records not migrated")
            return None
        parked: "list[Path]" = []
        for wire_tid in old:
            found = controls.get(local_tid(wire_tid), {})
            rec, retire = _upgraded(data[wire_tid], found, new_ctl, lambda: failure_of(wire_tid))
            for ctl in retire:
                aside = tasks_dir / f".{ctl}.withdrawn"
                try:
                    if not found[ctl]:
                        raise FileNotFoundError(ctl)
                    os.rename(tasks_dir / f"{ctl}.txt", aside)
                    parked.append(aside)
                    log(f"task {wire_tid}: unrecorded cancel {ctl} withdrawn")
                except FileNotFoundError:
                    rec["taken_ctl"] = rec.get("taken_ctl") or ctl
                    log(f"task {wire_tid}: unrecorded cancel {ctl} was taken by the core — watched")
                except OSError as e:
                    log(f"task {wire_tid}: unrecorded cancel {ctl} could not be withdrawn ({e}) "
                        "— not migrated")
                    return False
            data[wire_tid] = rec
        if not attempts._save(data):
            log("attempt records not migrated — their save failed; held until the next start")
            return False
    for aside in parked:
        aside.unlink(missing_ok=True)           # an inert dotfile: no task watcher reads it
    log(f"migrated {len(old)} attempt record(s)")
    return True


def _upgraded(rec: dict, found: "dict[str, bool]", new_ctl: Callable[[], str],
              failure: Callable[[], "tuple[str, list | None]"]) -> "tuple[dict, list[str]]":
    """The record in the current schema, and the found controls it does not explain."""
    gen = rec.get("execution_generation")
    out = {**rec, "schema": SCHEMA}
    named = {rec.get("fence_ctl"), rec.get("stop_ctl"), rec.get("taken_ctl"),
             (rec.get("owed_ctl") or {}).get("id") if isinstance(rec.get("owed_ctl"), dict) else None}
    unexplained = [c for c in found if c not in named]
    lost = rec.get("dispatch_lost")
    if lost is True or isinstance(lost, str):
        body, file = failure()
        out["dispatch_lost"] = {"body": lost if isinstance(lost, str) else body, "gen": gen, "file": file}
    elif lost is not None and not isinstance(lost, dict):
        out["dispatch_lost"] = None
    if isinstance(rec.get("owed_close"), str):
        out["owed_close"] = {"why": rec["owed_close"], "gen": gen}
    if isinstance(rec.get("owed_ctl"), dict) and "gen" not in rec["owed_ctl"]:
        out["owed_ctl"] = {**rec["owed_ctl"], "gen": gen}
    fenced = rec.get("fenced")
    adopt = unexplained[0] if unexplained else None
    if rec.get("stop_requested") and not rec.get("stop_ctl") and not fenced:
        ctl = adopt or new_ctl()
        out["stop_ctl"] = ctl
        if not adopt:
            out["owed_ctl"] = {"id": ctl, "reason": "task_cancelled", "gen": gen}
    elif fenced == "stale_execution_generation" and not (rec.get("fence_ctl") or rec.get("owed_ctl")):
        ctl = adopt or new_ctl()
        out["fence_ctl"] = ctl
        if not adopt:
            out["owed_ctl"] = {"id": ctl, "reason": fenced, "gen": gen}
    elif fenced in CLOSED_FENCES and not (rec.get("owed_close") or rec.get("owed_ctl")):
        if adopt:
            out["fence_ctl"] = adopt            # the close already became this cancel
        else:
            out["owed_close"] = {"why": f"closed on the server ({fenced})", "gen": gen}
    else:
        adopt = None
    return out, [c for c in unexplained if c != adopt]
