#!/usr/bin/env python3
"""health-check reports a standing pane-key fence (an uncertain send) on the core's socket."""
import importlib.util
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("health_check", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)


class PaneKeyFence(unittest.TestCase):
    def test_no_lock_and_an_active_send_are_ok(self):
        with tempfile.TemporaryDirectory() as d:
            sock = str(Path(d) / "s")
            self.assertEqual(hc.check_pane_key_fence(sock)["status"], "ok")
            (Path(sock + ".pane-keys-lock")).mkdir()
            (Path(sock + ".pane-keys-lock") / "pid").write_text("1\n")
            self.assertEqual(hc.check_pane_key_fence(sock)["status"], "ok")

    def test_an_uncertain_send_fails_and_names_the_recovery(self):
        with tempfile.TemporaryDirectory() as d:
            sock = str(Path(d) / "s")
            lock = Path(sock + ".pane-keys-lock")
            lock.mkdir()
            (lock / "uncertain").touch()
            out = hc.check_pane_key_fence(sock)
            self.assertEqual((out["name"], out["status"]), ("pane-key-fence", "fail"))
            self.assertIn(str(lock), out["detail"])
            self.assertIn("docs/codex-core.md", out["detail"])

    def test_the_check_is_registered(self):
        self.assertIn("checks.append(check_pane_key_fence())", (REPO / "src" / "health-check.py").read_text())


if __name__ == "__main__":
    unittest.main()
