"""The rig fault launcher's rule (tests/rig/lifecycle-fault-launcher.py): it fires on exactly the
claimed effect it names, passes every other call through unchanged, and reports a second match.

Run:
    python3 tests/rig-lifecycle-fault-launcher.test.py
"""
import importlib.util
import json
import tempfile
import types
import unittest
from pathlib import Path

PATH = Path(__file__).resolve().parent / "rig" / "lifecycle-fault-launcher.py"
spec = importlib.util.spec_from_file_location("lifecycle_fault_launcher", PATH)
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)

ACK = {"kind": "ack", "execution_generation": None, "lease_id": "L1", "run": "task-a", "wake_episode": None}
RESULT = {"kind": "result", "execution_generation": 2, "lease_id": "L2", "run": "task-a", "wake_episode": None}


class Bridge(types.SimpleNamespace):
    """The two names the launcher uses: `_req` and the attempt ledger's reader."""


class Launcher(unittest.TestCase):
    def setUp(self):
        self.report = Path(tempfile.mkdtemp()) / "report.jsonl"
        self.sent = []
        record = {"claims": {"ack-1": ACK, "result-1": RESULT}}
        self.mod = Bridge(_req=lambda m, p, payload=None, *a, **k: self.sent.append((m, p, k.get("operation_id"))) or {"ok": True},
                          _ATTEMPTS=types.SimpleNamespace(entry=lambda tid: record if tid == "task-a" else None))

    def events(self):
        return [json.loads(line)["event"] for line in self.report.read_text().splitlines()]

    def test_it_fires_once_on_the_named_claim_and_passes_the_rest_through(self):
        rule = launcher.Rule("result", "task-a", "L2", 2, None)
        sentinel = Path(tempfile.mkdtemp()) / "held"
        launcher.install(self.mod, rule, "stall", self.report, sentinel)
        real_sleep = launcher.time.sleep
        launcher.time.sleep = lambda s: sentinel.unlink(missing_ok=True)     # the operator releases it
        self.addCleanup(setattr, launcher.time, "sleep", real_sleep)
        self.mod._req("POST", "/v1/tasks/task-a/ack", {"id": "task-a"}, operation_id="ack-1")    # another kind
        self.mod._req("POST", "/v1/results", {"id": "task-b", "execution_generation": 2})          # another task
        self.mod._req("POST", "/v1/results", {"id": "task-a", "execution_generation": 1})          # no such claim
        self.assertEqual(rule.matches, [])
        self.assertEqual(len(self.sent), 3, "unmatched calls go through unchanged")
        self.mod._req("POST", "/v1/results", {"id": "task-a", "execution_generation": 2})
        self.assertEqual([m["op"] for m in rule.matches], ["result-1"])
        self.mod._req("POST", "/v1/results", {"id": "task-a", "execution_generation": 2})
        self.assertEqual(self.events(), ["matched", "released", "FAIL: matched more than once"])
        self.assertEqual(len(self.sent), 5)

    def test_an_ack_rule_matches_by_operation_id_and_the_claim_behind_it(self):
        self.assertIsNotNone(launcher.Rule("ack", "task-a", "L1", "none", "task-a").match(
            "POST", "/v1/tasks/task-a/ack", {}, "ack-1", {"claims": {"ack-1": ACK}}))
        for rule in (launcher.Rule("ack", "task-a", "L2", None, None), launcher.Rule("ack", "task-a", None, 1, None),
                     launcher.Rule("ack", "task-a", None, None, "task-a~~2")):
            self.assertIsNone(rule.match("POST", "/v1/tasks/task-a/ack", {}, "ack-1", {"claims": {"ack-1": ACK}}))
        self.assertIsNone(launcher.Rule("ack", "task-a", "L1", None, None).match(
            "POST", "/v1/tasks/task-a/ack", {}, "ack-unknown", {"claims": {"ack-1": ACK}}),
            "an operation the ledger never claimed is not the rule's")


if __name__ == "__main__":
    unittest.main()
