"""Verify usage schedules through the real installer with isolated platform boundaries."""

import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class UsageInstallTests(unittest.TestCase):
    def test_usage_install_renders_schedules_but_never_activates_them(self):
        output = ROOT / "out"
        output.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=output) as directory:
            base = Path(directory)
            source = base / "source"
            for name in ("bin", "tools/agent-usage"):
                shutil.copytree(
                    ROOT / name,
                    source / name,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                )
            for name in ("skills", "agents"):
                (source / name).mkdir()
            subprocess.run(["git", "init", "-q", str(source)], check=True)
            for system in ("Linux", "Darwin"):
                with self.subTest(system=system):
                    home = base / system
                    binary = home / "fake-bin"
                    binary.mkdir(parents=True)
                    uname = binary / "uname"
                    uname.write_text(
                        f'#!/bin/sh\ncase "$1" in -s) echo {system};; *) echo fixture;; esac\n'
                    )
                    uname.chmod(0o755)
                    # A fake bus exists; accidental activation would change its state.
                    service = binary / "systemctl"
                    enabled = home / "schedule-enabled"
                    service.write_text(
                        "#!" + sys.executable + "\n"
                        "import sys\nfrom pathlib import Path\n"
                        f"enabled=Path({str(enabled)!r})\n"
                        "if 'enable' in sys.argv or 'start' in sys.argv:\n"
                        " enabled.write_text('active')\n"
                    )
                    service.chmod(0o755)
                    env = dict(
                        os.environ,
                        HOME=str(home),
                        XDG_CONFIG_HOME=str(home / ".config"),
                        XDG_STATE_HOME=str(home / ".local/state"),
                        SKILL_DUMP_BIN_DIR=str(home / ".local/bin"),
                        PATH=str(binary) + os.pathsep + os.environ["PATH"],
                        TMPDIR=str(base),
                    )
                    result = subprocess.run(
                        [
                            str(source / "bin/skill-dump"),
                            "install",
                            "--only",
                            "agent-usage",
                            "--no-skills",
                            "--no-agent-instructions",
                            "--no-git-hooks",
                        ],
                        env=env,
                        text=True,
                        capture_output=True,
                        timeout=30,
                        check=False,
                    )
                    self.assertEqual(
                        result.returncode, 0, result.stdout + result.stderr
                    )
                    self.assertFalse(enabled.exists())
                    command = home / ".local/bin/agent-usage"
                    result = subprocess.run(
                        [str(command), "--help"],
                        env=env,
                        text=True,
                        capture_output=True,
                        timeout=30,
                        check=False,
                    )
                    self.assertEqual(
                        result.returncode, 0, result.stdout + result.stderr
                    )
                    self.assertIn("collect", result.stdout)
                    if system == "Linux":
                        service = home / ".config/systemd/user/agent-usage.service"
                        self.assertIn(str(command), service.read_text())
                        self.assertTrue((service.parent / "agent-usage.timer").exists())
                    else:
                        plist = (
                            home / "Library/LaunchAgents/com.theontho.agent-usage.plist"
                        )
                        with plist.open("rb") as stream:
                            job = plistlib.load(stream)
                        self.assertEqual(
                            job["ProgramArguments"], [str(command), "collect"]
                        )
                        self.assertTrue(job["Disabled"])


if __name__ == "__main__":
    unittest.main()
