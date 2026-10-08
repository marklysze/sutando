#!/usr/bin/env python3
"""src/tmux-pane-keys.sh is the one way keys reach an agent pane: it leaves copy mode before
typing and bounds each send. Pins that behaviour against a real tmux, the timeout, and that no
other caller in src/, skills/ or scripts/ runs send-keys itself."""
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HELPER = REPO / "src" / "tmux-pane-keys.sh"
sys.path.insert(0, str(REPO / "src"))
import tmux_pane_keys  # noqa: E402


def run(*args, timeout=30):
    return subprocess.run(["bash", str(HELPER), *args], capture_output=True, text=True, timeout=timeout)


@unittest.skipUnless(shutil.which("tmux"), "tmux not installed")
class RealTmux(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="pk", dir="/tmp")
        self.sock = os.path.join(self.dir, "s")
        self.view = os.path.join(self.dir, "v")
        self.tmux("new-session", "-d", "-s", "core", "-x", "120", "-y", "30", "cat")
        self.tmux("set-option", "-g", "mouse", "on")
        self.tmux("set-window-option", "-g", "mode-keys", "emacs")
        # A command-prompt only blocks when a client is attached to answer it.
        subprocess.run(["tmux", "-S", self.view, "new-session", "-d", "-s", "v", "-x", "120", "-y", "30",
                        f"env -u TMUX tmux -S {self.sock} attach -t core"], check=True)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not self.tmux("list-clients").stdout.strip():
            time.sleep(0.1)

    def tearDown(self):
        for sock in (self.view, self.sock):
            subprocess.run(["tmux", "-S", sock, "kill-server"], capture_output=True)
        shutil.rmtree(self.dir, ignore_errors=True)

    def tmux(self, *args):
        return subprocess.run(["tmux", "-S", self.sock, *args], capture_output=True, text=True, timeout=10)

    def in_mode(self):
        return self.tmux("display-message", "-p", "-t", "=core:0", "#{pane_in_mode}").stdout.strip()

    def test_a_pane_in_copy_mode_is_released_and_the_text_lands(self):
        self.tmux("copy-mode", "-t", "=core:0")
        self.assertEqual(self.in_mode(), "1")
        start = time.monotonic()
        r = run("-S", self.sock, "-t", "=core:0", "--", "-l", "--", "Sutando task ready")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertLess(time.monotonic() - start, 3)
        self.assertEqual(self.in_mode(), "0")
        self.assertEqual(run("-S", self.sock, "-t", "=core:0", "--", "C-m").returncode, 0)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and "Sutando task ready" not in self.tmux("capture-pane", "-p", "-t", "=core:0").stdout:
            time.sleep(0.1)
        self.assertIn("Sutando task ready", self.tmux("capture-pane", "-p", "-t", "=core:0").stdout)

    def test_without_the_helper_the_same_send_blocks(self):
        # The control: plain send-keys into copy mode waits on "(jump to forward)".
        self.tmux("copy-mode", "-t", "=core:0")
        p = subprocess.Popen(["tmux", "-S", self.sock, "send-keys", "-t", "=core:0", "-l", "--", "Sutando"])
        try:
            with self.assertRaises(subprocess.TimeoutExpired):
                p.wait(timeout=2)
        finally:
            p.kill()
            p.wait()


@unittest.skipUnless(shutil.which("tmux"), "tmux not installed")
@unittest.skipUnless(shutil.which("tmux"), "tmux not installed")
class StoppedServer(unittest.TestCase):
    """A request queued with a stopped server is not withdrawn by killing its client; once the
    helper has reported 124, resuming the server must not apply the keys late."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="pk", dir="/tmp")
        self.sock = os.path.join(self.dir, "s")
        self.bytes = os.path.join(self.dir, "bytes")
        subprocess.run(["tmux", "-S", self.sock, "new-session", "-d", "-s", "t", "-x", "120", "-y", "20",
                        f"stty raw -echo; cat > {self.bytes}"], check=True)
        self.pid = int(subprocess.run(["tmux", "-S", self.sock, "display-message", "-p", "#{pid}"],
                                      capture_output=True, text=True, check=True).stdout)
        time.sleep(0.3)

    def tearDown(self):
        os.kill(self.pid, signal.SIGCONT)
        subprocess.run(["tmux", "-S", self.sock, "kill-server"], capture_output=True)
        shutil.rmtree(self.dir, ignore_errors=True)

    def landed_after_resume(self):
        os.kill(self.pid, signal.SIGCONT)
        time.sleep(1.5)
        return Path(self.bytes).read_text()

    def test_stopped_before_the_call_returns_124_and_nothing_lands_after_resume(self):
        os.kill(self.pid, signal.SIGSTOP)
        start = time.monotonic()
        r = run("-S", self.sock, "-t", "=t:0", "--timeout", "1", "--", "-l", "--", "LATE-MARKER", timeout=12)
        self.assertEqual(r.returncode, 124, r.stderr)
        self.assertLess(time.monotonic() - start, 5)
        self.assertEqual(self.landed_after_resume(), "")

    def test_stopped_just_before_the_send_returns_124_and_nothing_lands_after_resume(self):
        shim = Path(self.dir) / "tmux"
        shim.write_text(f'#!/bin/bash\ncase "$*" in *if-shell*|*send-keys*) kill -STOP {self.pid};; esac\nexec tmux "$@"\n')
        shim.chmod(0o755)
        r = run("--tmux", str(shim), "-S", self.sock, "-t", "=t:0", "--timeout", "1", "--", "-l", "--", "RACE-MARKER", timeout=12)
        self.assertEqual(r.returncode, 124, r.stderr)
        self.assertEqual(self.landed_after_resume(), "")

    def test_a_live_server_receives_the_keys_exactly(self):
        text = "it's \"x\"; a\\;b $HOME #{pane_id}"
        self.assertEqual(run("-S", self.sock, "-t", "=t:0", "--", "-l", "--", text).returncode, 0)
        self.assertEqual(run("-S", self.sock, "-t", "=t:0", "--", "-l", "--", "end\\;").returncode, 0)
        self.assertEqual(run("-S", self.sock, "-t", "=t:0", "--", "-N", "2", "BSpace").returncode, 0)
        self.assertEqual(run("-S", self.sock, "-t", "=t:0", "--", "C-m").returncode, 0)
        time.sleep(0.5)
        self.assertEqual(Path(self.bytes).read_bytes(), text.encode() + b"end;\x7f\x7f\r")

    def test_stopped_after_claim_refuses_retries_even_after_server_resumes(self):
        bin_dir = Path(self.dir) / "bin"
        bin_dir.mkdir()
        claimed = Path(self.dir) / "claimed"
        mv = bin_dir / "mv"
        mv.write_text(f'''#!/bin/sh
"{shutil.which('mv')}" "$@" || exit $?
touch "{claimed}"
kill -STOP {self.pid}
''')
        mv.chmod(0o755)
        subprocess.run(["tmux", "-S", self.sock, "set-environment", "-g", "PATH",
                        str(bin_dir) + ":" + os.environ["PATH"]], check=True)
        r = run("-S", self.sock, "-t", "=t:0", "--timeout", "1", "--", "-l", "--", "FIRST", timeout=8)
        self.assertTrue(claimed.exists())
        self.assertEqual(r.returncode, 125, r.stderr)
        before = self.landed_after_resume()
        retry = run("-S", self.sock, "-t", "=t:0", "--", "-l", "--", "UNSAFE-RETRY")
        self.assertEqual(retry.returncode, 125, retry.stderr)
        time.sleep(0.3)
        self.assertEqual(Path(self.bytes).read_text(), before)
        subprocess.run(["tmux", "-S", self.sock, "set-environment", "-g", "PATH", os.environ["PATH"]], check=True)
        Path(self.sock + ".pane-keys-lock").rmdir()
        recovered = run("-S", self.sock, "-t", "=t:0", "--", "-l", "--", "RECOVERED")
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        time.sleep(0.3)
        self.assertEqual(Path(self.bytes).read_text(), before + "RECOVERED")


class Timeout(unittest.TestCase):
    def test_concurrent_send_is_refused_and_success_releases_the_lock(self):
        with tempfile.TemporaryDirectory() as d:
            sock = str(Path(d) / "s")
            claimed = Path(d) / "claimed"
            fake = Path(d) / "tmux"
            fake.write_text(f'''#!/bin/bash
[ "$3" = display-message ] && {{ echo 0; exit 0; }}
[ "$3" = if-shell ] && {{ sh -c "$4"; touch '{claimed}'; sleep 1; }}
''')
            fake.chmod(0o755)
            args = ["--tmux", str(fake), "-S", sock, "-t", "=t:0", "--", "Enter"]
            first = subprocess.Popen(["bash", str(HELPER), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 3
                while not claimed.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(claimed.exists())
                self.assertEqual(run(*args).returncode, 125)
                _, stderr = first.communicate(timeout=5)
                self.assertEqual(first.returncode, 0, stderr)
                self.assertFalse(Path(sock + ".pane-keys-lock").exists())
                self.assertEqual(run(*args).returncode, 0)
            finally:
                if first.poll() is None:
                    first.kill()
                first.communicate()

    def test_claimed_timeout_blocks_every_later_sender_until_recovery(self):
        with tempfile.TemporaryDirectory() as d:
            sock = str(Path(d) / "s")
            fake = Path(d) / "tmux"
            log = Path(d) / "calls"
            fake.write_text(f'''#!/bin/bash
echo "$*" >> '{log}'
[ "$3" = display-message ] && {{ echo 0; exit 0; }}
[ "$3" = if-shell ] && {{ sh -c "$4"; exec sleep 30; }}
''')
            fake.chmod(0o755)
            r = run("--tmux", str(fake), "-S", sock, "-t", "=t:0", "--timeout", "1", "--", "Enter")
            self.assertEqual(r.returncode, 125, r.stderr)
            calls = log.read_text()
            for target, key in (("=t:0", "Enter"), ("=other:0", "Escape")):
                retry = run("--tmux", str(fake), "-S", sock, "-t", target, "--", key)
                self.assertEqual(retry.returncode, 125, retry.stderr)
                self.assertEqual(log.read_text(), calls)
            self.assertTrue(Path(sock + ".pane-keys-lock").is_dir())

    def test_a_send_that_never_returns_fails_with_124_inside_the_bound(self):
        with tempfile.TemporaryDirectory() as d:
            fake = Path(d) / "tmux"
            fake.write_text('#!/bin/bash\ncase "$*" in *display-message*) echo 0;; *send-keys*) exec sleep 30;; esac\n')
            fake.chmod(0o755)
            start = time.monotonic()
            r = run("--tmux", str(fake), "-S", str(Path(d) / "s"), "-t", "=core:0", "--timeout", "1", "--", "-l", "--", "hi")
            self.assertEqual(r.returncode, 124, r.stderr)
            self.assertIn("timed out after 1s", r.stderr)
            self.assertLess(time.monotonic() - start, 5)

    def test_send_keys_status_passes_through_and_usage_is_2(self):
        with tempfile.TemporaryDirectory() as d:
            fake = Path(d) / "tmux"
            log = Path(d) / "log"
            fake.write_text(f'#!/bin/bash\necho "$*" >> {log}\ncase "$*" in *display-message*) echo 1;; *send-keys*) exit 3;; esac\n')
            fake.chmod(0o755)
            r = run("--tmux", str(fake), "-S", str(Path(d) / "s"), "-t", "=c:0", "--", "Enter")
            self.assertEqual(r.returncode, 3)
            calls = log.read_text().splitlines()[1:]
            self.assertEqual(calls[0], f"-S {Path(d) / 's'} copy-mode -q -t =c:0")
            self.assertRegex(calls[1], rf"^-S {re.escape(str(Path(d) / 's'))} if-shell mv '[^']+/ticket' '[^']+/ticket\.claimed' send-keys -t '=c:0' 'Enter'$")
        self.assertEqual(run("-S", str(Path(d) / "s"), "-t", "=c:0").returncode, 2)
        self.assertEqual(run("-S", str(Path(d) / "s"), "-t", "=c:0", "--timeout", "0.5", "--", "Enter").returncode, 2)

    def test_hanging_probe_is_bounded_and_send_still_runs(self):
        with tempfile.TemporaryDirectory() as d:
            fake = Path(d) / "tmux"
            fake.write_text('#!/bin/bash\ncase "$*" in *display-message*) exec sleep 30;; *send-keys*) echo SENT;; esac\n')
            fake.chmod(0o755)
            start = time.monotonic()
            r = run("--tmux", str(fake), "-S", str(Path(d) / "s"), "-t", "=c:0", "--timeout", "1", "--", "Enter", timeout=5)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.strip(), "SENT")
            self.assertLess(time.monotonic() - start, 4)

    def test_a_probe_whose_descendant_holds_stdout_cannot_hang_the_helper(self):
        # A killed client whose server is stopped leaves its fds behind: a child holding stdout is that shape.
        with tempfile.TemporaryDirectory() as d:
            fake = Path(d) / "tmux"
            fake.write_text('#!/bin/bash\ncase "$*" in *display-message*) sleep 8;; *send-keys*) echo SENT;; esac\n')
            fake.chmod(0o755)
            start = time.monotonic()
            r = run("--tmux", str(fake), "-S", str(Path(d) / "s"), "-t", "=c:0", "--timeout", "1", "--", "Enter", timeout=12)
            self.assertEqual((r.returncode, r.stdout.strip()), (0, "SENT"), r.stderr)
            self.assertLess(time.monotonic() - start, 4)

    def test_a_send_whose_descendant_holds_stdout_times_out_inside_the_bound(self):
        with tempfile.TemporaryDirectory() as d:
            fake = Path(d) / "tmux"
            fake.write_text('#!/bin/bash\ncase "$*" in *display-message*) echo 0;; *send-keys*) sleep 8;; esac\n')
            fake.chmod(0o755)
            start = time.monotonic()
            r = run("--tmux", str(fake), "-S", str(Path(d) / "s"), "-t", "=c:0", "--timeout", "1", "--", "Enter", timeout=12)
            self.assertEqual(r.returncode, 124, r.stderr)
            self.assertLess(time.monotonic() - start, 4)

    def test_term_resistant_send_is_killed_before_it_can_deliver_late(self):
        with tempfile.TemporaryDirectory() as d:
            fake = Path(d) / "tmux"
            marker = Path(d) / "delivered"
            fake.write_text(f'''#!/bin/bash
case "$*" in
  *display-message*) echo 0;;
  *send-keys*) exec "{sys.executable}" -c 'import signal,time; from pathlib import Path; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(4); Path("{marker}").touch()';;
esac
''')
            fake.chmod(0o755)
            start = time.monotonic()
            r = run("--tmux", str(fake), "-S", str(Path(d) / "s"), "-t", "=c:0", "--timeout", "1", "--", "Enter", timeout=5)
            self.assertEqual(r.returncode, 124, r.stderr)
            self.assertLess(time.monotonic() - start, 3.5)
            time.sleep(4)
            self.assertFalse(marker.exists())

    def test_hanging_mode_exit_does_not_send_keys(self):
        with tempfile.TemporaryDirectory() as d:
            fake = Path(d) / "tmux"
            fake.write_text('#!/bin/bash\ncase "$*" in *display-message*) echo 1;; *copy-mode*) exec sleep 30;; *send-keys*) echo SENT;; esac\n')
            fake.chmod(0o755)
            r = run("--tmux", str(fake), "-S", str(Path(d) / "s"), "-t", "=c:0", "--timeout", "1", "--", "Enter", timeout=5)
            self.assertEqual(r.returncode, 124, r.stderr)
            self.assertNotIn("SENT", r.stdout)

    def test_python_argv(self):
        self.assertEqual(tmux_pane_keys.argv("/s", "=c:0", "Escape", tmux="/bin/tmux"),
                         ["bash", str(HELPER), "--tmux", "/bin/tmux", "-S", "/s", "-t", "=c:0", "--", "Escape"])


class OneSender(unittest.TestCase):
    # A send names its pane with -t (or types with -l / -N); `bind ... send-keys -M` and prose do not.
    CALL = re.compile(r"""send-keys["',]*\s+["']?-[tlN]\b""")

    def test_no_other_code_runs_send_keys(self):
        hits = []
        for root in ("src", "skills", "scripts"):
            for path in (REPO / root).rglob("*"):
                if path.suffix not in (".sh", ".py", ".ts", ".mjs", ".js") or path == HELPER or "node_modules" in path.parts:
                    continue
                for n, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
                    code = line.split("#", 1)[0] if path.suffix in (".sh", ".py") else line.split("//", 1)[0]
                    if self.CALL.search(code):
                        hits.append(f"{path.relative_to(REPO)}:{n}: {line.strip()}")
        self.assertEqual(hits, [], "send keys through src/tmux-pane-keys.sh:\n" + "\n".join(hits))


if __name__ == "__main__":
    unittest.main()
