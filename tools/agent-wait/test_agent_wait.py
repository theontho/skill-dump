"""Exercise the installed CLI shape with real processes and an isolated coordinator."""

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1]
WAIT = TOOLS / "agent-wait/agent-wait"
COORD = TOOLS / "agent-coord/agent_coord.py"
LINUX = sys.platform.startswith("linux")
WINDOWS = sys.platform == "win32"


class WaitTest(unittest.TestCase):
    def setUp(self):
        scratch = TOOLS.parent / "out"
        scratch.mkdir(exist_ok=True)
        tmp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.env = {
            **os.environ,
            "PATH": f"{self.root}{os.pathsep}{os.defpath}",
            "AGENT_COORD_RUNTIME_DIR": str(self.root / "runtime"),
            "AGENT_COORD_STATE_DIR": str(self.root / "state"),
        }
        if WINDOWS:
            # The gh.exe fixture is a copy of the native Python executable.
            # It loads the real interpreter and runs the boundary script named
            # by the command's first argument (run or pr).
            self.env["PATH"] += os.pathsep + str(Path(sys.executable).parent)
            self.env["PYTHONHOME"] = sys.base_prefix
        if LINUX:
            (self.root / "agent-coord").symlink_to(COORD)
            log = (self.root / "service.log").open("w")
            self.addCleanup(log.close)
            self.spawn([sys.executable, COORD, "serve"], stdout=log, stderr=log)
            self.until(lambda: (self.root / "runtime/sock").exists())

    def spawn(self, argv, **options):
        proc = subprocess.Popen(argv, cwd=self.root, env=self.env, text=True, **options)
        self.addCleanup(self.stop, proc)
        return proc

    @staticmethod
    def stop(proc):
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
        proc.communicate(timeout=3)

    def until(self, predicate, *, timeout=5):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail("condition did not occur")
            time.sleep(0.025)

    def stub(self, name, body):
        source = f"#!{sys.executable}\nimport json, os, sys, time\nfrom pathlib import Path\n{body}"
        if WINDOWS:
            shutil.copy2(sys.executable, self.root / f"{name}.exe")
            for script in ("run", "pr"):
                (self.root / script).write_text(source)
        else:
            path = self.root / name
            path.write_text(source)
            path.chmod(0o755)

    def assert_pid_exited(self, pid):
        # os.kill(pid, 0) is a terminating operation on Windows.
        self.result(self.watcher("pid", str(pid), timeout="1s"), 0, f"exited pid={pid}")

    def coord(self, *args):
        return subprocess.check_output(
            [sys.executable, COORD, *args],
            cwd=self.root,
            env=self.env,
            text=True,
            timeout=3,
        )

    def history(self):
        return [
            json.loads(line) for line in self.coord("history", "--json").splitlines()
        ]

    def watcher(self, *args, timeout="4s", owner=None, notify=False):
        argv = [sys.executable, WAIT, "--timeout", timeout, "--interval", "0.05s"]
        if owner is not None:
            argv.extend(["--owner-pid", str(owner)])
        if notify:
            argv.extend(["--as", "watch-worker", "--notify", "test-owner"])
        return self.spawn(
            [*argv, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )

    def result(self, proc, code, suffix):
        out, err = proc.communicate(timeout=6)
        self.assertEqual(proc.returncode, code, (out, err))
        self.assertEqual(len(out.splitlines()), 1, out)
        self.assertTrue(out.rstrip().endswith(suffix), (out, err))
        self.assertEqual(err, "")
        return out.strip()

    def test_file_watch_is_listed_and_notifies_without_allocating_compute(self):
        proc = self.watcher("file", "done", notify=LINUX)
        if LINUX:
            before = json.loads(self.coord("status", "--json"))
            self.until(lambda: "watch:file" in self.coord("status"))
            status = json.loads(self.coord("status", "--json"))
            self.assertEqual(status["leases"], before["leases"])
            self.assertEqual(status["watchers"][0]["owner_pid"], os.getpid())
        (self.root / "done").touch()
        line = self.result(proc, 0, "appeared")
        if LINUX:
            self.assertIn(line, self.coord("--as", "test-owner", "inbox"))
            self.assertEqual(json.loads(self.coord("status", "--json"))["watchers"], [])

    def test_pidfile_waits_for_pid_and_then_handles_unreaped_exit(self):
        target = self.spawn([sys.executable, "-c", "import time; time.sleep(30)"])
        proc = self.watcher("pidfile", "job.pid")
        (self.root / "job.pid").write_text(str(target.pid))
        target.terminate()
        self.result(proc, 0, f"exited pid={target.pid}")

    def test_missing_pid_is_already_done(self):
        target = self.spawn([sys.executable, "-c", "pass"])
        target.wait(timeout=3)
        self.result(self.watcher("pid", str(target.pid)), 0, f"exited pid={target.pid}")

    def test_missing_file_has_a_hard_deadline(self):
        proc = self.watcher("file", "not-created", timeout="0.2s")
        self.result(proc, 124, "timeout")

    def test_orphan_stops_even_during_an_external_probe(self):
        self.stub(
            "gh", "Path('child.pid').write_text(str(os.getpid()))\ntime.sleep(30)\n"
        )
        owner = self.spawn([sys.executable, "-c", "import time; time.sleep(30)"])
        proc = self.watcher("run", "23456789012", owner=owner.pid)
        self.until(lambda: (self.root / "child.pid").exists())
        owner.kill()
        owner.wait(timeout=3)
        self.result(proc, 125, "orphaned")
        self.assert_pid_exited(int((self.root / "child.pid").read_text()))

    def test_hard_timeout_kills_a_hung_github_probe(self):
        self.stub(
            "gh",
            """
import subprocess
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
Path('child.pid').write_text(f'{os.getpid()} {child.pid}')
time.sleep(30)
""",
        )
        started = time.monotonic()
        proc = self.watcher("run", "23456789012", timeout="1.2s")
        self.result(proc, 124, "timeout")
        self.assertLess(time.monotonic() - started, 4)
        for pid in (self.root / "child.pid").read_text().split():
            self.assert_pid_exited(int(pid))

    def test_github_run_waits_for_completion_and_preserves_failed_conclusion(self):
        self.stub(
            "gh",
            """
assert sys.argv[1] == 'view' if sys.platform == 'win32' else sys.argv[1:3] == ['run', 'view']
assert sys.argv[-1] == '23456789012'
seen = Path('seen')
if seen.exists():
    print(json.dumps({'status': 'completed', 'conclusion': 'cancelled'}))
else:
    print(json.dumps({'status': 'in_progress', 'conclusion': ''}))
    seen.touch()
""",
        )
        self.result(
            self.watcher("run", "23456789012", "-R", "owner/repo"), 1, "cancelled"
        )

    def test_pr_lifecycle_and_checks(self):
        self.stub(
            "gh",
            """
if ('view' in sys.argv[1:3]):
    print(json.dumps({'state': 'MERGED'}))
else:
    seen = Path('seen')
    if not seen.exists():
        seen.touch()
        print('no checks reported on the branch', file=sys.stderr)
        sys.exit(1)
    if not Path('pending').exists():
        Path('pending').touch()
        print(json.dumps([{'bucket': 'pending'}, {'bucket': 'fail'}]))
        sys.exit(8)
    print(json.dumps([{'bucket': 'pass'}, {'bucket': 'fail'}]))
    sys.exit(1)
""",
        )
        self.result(self.watcher("pr", "42"), 0, "merged")
        self.result(self.watcher("pr", "42", "--checks"), 1, "fail")
        self.assertTrue((self.root / "pending").exists())

    @unittest.skipIf(WINDOWS, "systemd waits are not supported on native Windows")
    def test_systemd_distinguishes_unit_failure_from_manager_error(self):
        self.stub(
            "systemctl",
            """
if sys.argv[-1] == 'broken.service':
    seen = Path('seen')
    print('LoadState=loaded')
    print('ActiveState=failed' if seen.exists() else 'ActiveState=deactivating')
    seen.touch()
else:
    print('Failed to connect to bus', file=sys.stderr)
    sys.exit(1)
""",
        )
        self.result(self.watcher("systemd", "broken.service"), 1, "failed")
        self.result(
            self.watcher("systemd", "other.service"), 2, "Failed to connect to bus"
        )

    @unittest.skipIf(WINDOWS, "launchd waits are not supported on native Windows")
    def test_launchd_stops_when_job_unloads(self):
        self.stub(
            "launchctl",
            """
seen = Path('seen')
if seen.exists():
    print('Could not find service', file=sys.stderr)
    sys.exit(113)
print('state = running\\npid = 100')
seen.touch()
""",
        )
        self.result(self.watcher("launchd", "gui/501/org.example.job"), 0, "unloaded")

    @unittest.skipUnless(LINUX, "agent-coord ships on Linux")
    def test_lease_wait_returns_after_existing_lease_is_released(self):
        lease = self.coord(
            "hold",
            "--cores",
            "1",
            "--mem",
            "0",
            "--label",
            "work",
            "--pid",
            str(os.getpid()),
        ).strip()
        proc = self.watcher("lease", lease)
        self.until(lambda: "watch:lease" in self.coord("status"))
        self.assertIsNone(proc.poll())
        self.coord("release", lease)
        self.result(proc, 0, "ended")

    @unittest.skipUnless(LINUX, "agent-coord ships on Linux")
    def test_watcher_reaping_does_not_remove_restarted_pidfile_lease(self):
        argv = [sys.executable, "-c", "import time; time.sleep(30)"]
        driver, replacement = self.spawn(argv), self.spawn(argv)
        watch, owner = self.spawn(argv), self.spawn(argv)
        pidfile = self.root / "driver.pid"
        pidfile.write_text(str(driver.pid))
        lease = self.coord("--as", "driver", "hold", "--io", "--label", "driver",
                           "--pidfile", str(pidfile)).strip()
        self.coord("--as", "driver", "phase", lease, "--exclusive-lock", "gpu")
        self.coord("hold", "--label", "watch:orphan", "--expect", "25s",
                   "--pid", str(watch.pid), "--owner-pid", str(owner.pid))
        owner.kill()
        self.assertEqual(watch.wait(timeout=8), -signal.SIGTERM)
        driver.kill()
        driver.wait(timeout=5)
        next_pidfile = pidfile.with_suffix(".new")
        next_pidfile.write_text(str(replacement.pid))
        next_pidfile.replace(pidfile)

        def reattached():
            snapshot = json.loads(self.coord("status", "--json"))
            return (not snapshot["watchers"] and any(
                row["id"] == lease and row["pid"] == replacement.pid
                and "gpu" not in row["locks"] for row in snapshot["leases"]
            ))

        self.until(reattached, timeout=12)
        self.coord("--as", "driver", "phase", lease, "--exclusive-lock", "gpu")
        self.coord("--as", "driver", "phase", lease, "--release", "gpu")

    @unittest.skipUnless(LINUX, "agent-coord ships on Linux")
    def test_coordinator_reaps_an_expired_watcher_that_ignores_term(self):
        proc = self.spawn(
            [
                sys.executable,
                "-c",
                (
                    "import signal,time; from pathlib import Path; "
                    "signal.signal(signal.SIGTERM, signal.SIG_IGN); Path('ready').touch(); time.sleep(30)"
                ),
            ]
        )
        self.until(lambda: (self.root / "ready").exists())
        self.coord(
            "--as",
            "worker",
            "hold",
            "--label",
            "watch:stuck",
            "--expect",
            "0.3s",
            "--pid",
            str(proc.pid),
            "--owner-pid",
            str(os.getpid()),
        )
        self.assertEqual(proc.wait(timeout=8), -signal.SIGKILL)
        self.assertNotIn("watch:stuck", self.coord("status"))
        self.assertTrue(
            any(
                e["event"] == "watch_ended" and e["why"] == "expired"
                for e in self.history()
            )
        )

    @unittest.skipUnless(LINUX, "agent-coord ships on Linux")
    def test_coordinator_detects_owner_death_independent_of_helper(self):
        argv = [sys.executable, "-c", "import time; time.sleep(30)"]
        proc, owner = self.spawn(argv), self.spawn(argv)
        self.coord(
            "--as",
            "worker",
            "hold",
            "--label",
            "watch:orphan",
            "--expect",
            "30s",
            "--pid",
            str(proc.pid),
            "--owner-pid",
            str(owner.pid),
        )
        owner.kill()
        self.assertEqual(proc.wait(timeout=5), -signal.SIGTERM)
        self.assertNotIn("watch:orphan", self.coord("status"))
        self.assertTrue(
            any(
                e["event"] == "watch_ended" and e["why"] == "owner exited"
                for e in self.history()
            )
        )
