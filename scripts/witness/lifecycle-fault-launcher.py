#!/usr/bin/env python3
"""Rig-only fault launcher for the lifecycle-v2 witness: runs the production bridge with one fault.

It loads the production loader (`src/remote-gateway-bridge.py`) exactly as `python3 src/...` does,
prints the loaded module identity, then wraps only the bridge's `_req`. Every call passes through
unchanged except the ONE that matches the rule, identified by its claimed effect: the request path
kind and task, plus the claim the ledger holds for the request's operation id (its generation,
lease and run). The rule fires once; a second match, or none by exit, is reported as a failed run.

  crash  call the real broker, record its answer (the proof it accepted), then os._exit(9)
         before the bridge sees the answer, so nothing is committed.
  stall  hold the matching call until the sentinel file is removed, then pass it through.

Usage (all on the rig, at the exact head under test):
  python3 scripts/witness/lifecycle-fault-launcher.py --fault crash --kind ack --task <broker id> \\
      --lease <lease id> [--generation N|none] [--run <local run id>] --report <file.jsonl> \\
      [--sentinel <file>] [-- <bridge args>]

Restart the bridge normally afterwards (`python3 src/remote-gateway-bridge.py`), never under
this launcher. Not part of the product; nothing imports it.
"""
from __future__ import annotations

import argparse
import atexit
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LOADER = REPO / "src" / "remote-gateway-bridge.py"
KINDS = {"ack": "/ack", "cancel-ack": "/cancel-ack", "result": "/v1/results"}


class Rule:
    """The one call a fault fires on, and the record of every call that matched it."""

    def __init__(self, kind: str, task: str, lease: "str | None", generation, run: "str | None"):
        self.kind, self.task, self.lease, self.generation, self.run = kind, task, lease, generation, run
        self.matches: "list[dict]" = []
        self.lock = threading.Lock()

    def claim_of(self, record, op: "str | None", payload) -> "dict | None":
        """The claim this call was made under: by its operation id, or for a result (sent by the outbox,
        which carries no id) the record's one open result claim for this payload's generation."""
        claims = (record or {}).get("claims") or {}
        if op:
            c = claims.get(op)
            return {**c, "op": op} if isinstance(c, dict) else None
        found = [{**c, "op": k} for k, c in claims.items() if isinstance(c, dict) and c.get("kind") == "result"
                 and c.get("execution_generation") == (payload or {}).get("execution_generation")]
        return found[0] if len(found) == 1 else None

    def match(self, method: str, path: str, payload, op, record) -> "dict | None":
        if method != "POST" or not path.split("?")[0].endswith(KINDS[self.kind]):
            return None
        target = (payload or {}).get("id") if self.kind == "result" else path.split("/v1/tasks/")[-1].split("/")[0]
        if target != self.task:
            return None
        claim = self.claim_of(record, op, payload)
        if not claim or claim.get("kind") != self.kind:
            return None
        # An ack's claim names the serve it answers; any other effect is the record's current lease.
        lease = claim.get("serve") if self.kind == "ack" else (record or {}).get("lease_id")
        identity = {"task": target, "op": claim["op"], "kind": claim["kind"], "run": claim.get("run"),
                    "execution_generation": claim.get("execution_generation"),
                    "wake_episode": claim.get("wake_episode"), "lease_id": lease, "epoch": claim.get("epoch")}
        want = {"lease_id": self.lease, "execution_generation": self.generation, "run": self.run}
        if any(v is not None and v != "*" and identity[k] != (None if v == "none" else v) for k, v in want.items()):
            return None
        return identity


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _report(path: Path, line: dict) -> None:
    with open(path, "a") as fh:
        fh.write(json.dumps({"at": time.time(), **line}, sort_keys=True, default=str) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def load_bridge():
    """The production loader, loaded as `python3 src/remote-gateway-bridge.py` loads it, but stopped
    before its script tail so `_req` can be wrapped first."""
    spec = importlib.util.spec_from_file_location("sutando_remote_gateway_bridge", LOADER)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def install(mod, rule: Rule, fault: str, report: Path, sentinel: "Path | None") -> None:
    real = mod._req

    def wrapped(method, path, payload=None, *a, **k):
        op = k.get("operation_id")
        record = mod._ATTEMPTS.entry(rule.task) if mod._ATTEMPTS is not None else None
        claim = rule.match(method, path, payload, op, record if isinstance(record, dict) else None)
        if claim is None:
            return real(method, path, payload, *a, **k)
        with rule.lock:
            rule.matches.append(claim)
            first = len(rule.matches) == 1
        if not first:
            _report(report, {"event": "FAIL: matched more than once", "claim": claim, "path": path})
            return real(method, path, payload, *a, **k)
        _report(report, {"event": "matched", "fault": fault, "path": path, "claim": claim})
        if fault == "stall":
            sentinel.write_text(claim["op"])
            while sentinel.exists():
                time.sleep(0.1)
            answer = real(method, path, payload, *a, **k)
            _report(report, {"event": "released", "answer": answer})
            return answer
        answer = real(method, path, payload, *a, **k)    # raises on a refusal: nothing accepted, no crash
        _report(report, {"event": "accepted by the broker; killing the bridge before its commit",
                         "answer": answer, "op": claim["op"]})
        os._exit(9)
    mod._req = wrapped


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fault", choices=("crash", "stall"), required=True)
    ap.add_argument("--kind", choices=tuple(KINDS), required=True)
    ap.add_argument("--task", required=True, help="the broker task id")
    ap.add_argument("--lease", help="the lease the claim was made under ('*' for any)")
    ap.add_argument("--generation", help="the generation the claim was made under, or 'none'")
    ap.add_argument("--run", help="the local run id the claim was made under")
    ap.add_argument("--report", required=True, type=Path)
    ap.add_argument("--sentinel", type=Path, help="stall: held while this file exists")
    args, rest = ap.parse_known_args(argv)
    if args.fault == "stall" and not args.sentinel:
        ap.error("--sentinel is required with --fault stall")
    generation = int(args.generation) if args.generation and args.generation.isdigit() else args.generation
    rule = Rule(args.kind, args.task, args.lease, generation, args.run)
    sys.argv = [str(LOADER), *[a for a in rest if a != "--"]]
    mod = load_bridge()
    head = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    identity = {"event": "loaded", "head": head, "loader": str(LOADER), "loader_sha256": _sha256(LOADER),
                "module_file": str(Path(mod._IMPL).resolve()), "module_sha256": _sha256(Path(mod._IMPL)),
                "runtime_identity": mod.RUNTIME_IDENTITY,
                "rule": {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()} | {"generation": generation}}
    print(json.dumps(identity, sort_keys=True, default=str), flush=True)
    _report(args.report, identity)
    install(mod, rule, args.fault, args.report, args.sentinel)

    def zero_matches():
        if not rule.matches:
            _report(args.report, {"event": "FAIL: the rule never matched"})
    atexit.register(zero_matches)
    mod.__name__ = "__main__"
    mod.VOICE_ROOM_VERIFIER.start()
    mod.main()


if __name__ == "__main__":
    main()
