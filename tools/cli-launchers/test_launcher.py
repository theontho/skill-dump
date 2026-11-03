"""Verify migrated launchers without downloading or executing any real agent."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent


class LauncherTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        mise = self.root / "mise"
        mise.write_text("""#!/usr/bin/env python3
import json, os, sys
with open(os.environ["CALL_LOG"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1] == "use":
    sys.exit(int(os.environ.get("INSTALL_STATUS", "0")))
sys.exit(int(os.environ.get("COMMAND_STATUS", "0")))
""")
        mise.chmod(0o755)
        self.env = dict(os.environ, PATH=f"{self.root}:{os.environ['PATH']}",
                        CALL_LOG=str(self.root / "calls"))

    def run_launcher(self, name, *arguments):
        launcher = self.root / name
        launcher.symlink_to(ROOT / "launcher")
        return subprocess.run([str(launcher), *arguments], env=self.env,
                              text=True, capture_output=True, timeout=5)

    def calls(self):
        return [json.loads(line) for line in (self.root / "calls").read_text().splitlines()]

    def test_symlink_name_selects_package_and_preserves_arguments_and_exit_status(self):
        self.env["COMMAND_STATUS"] = "7"
        result = self.run_launcher("claude", "--prompt", "$(not a command)", "two words")
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(self.calls(), [
            ["use", "-g", "--quiet", "claude"],
            ["x", "claude", "--", "claude", "--prompt", "$(not a command)", "two words"],
        ])

    def test_special_backend_executes_original_command_name(self):
        result = self.run_launcher("omp", "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), [
            ["use", "-g", "--quiet", "github:can1357/oh-my-pi"],
            ["x", "github:can1357/oh-my-pi", "--", "omp", "--help"],
        ])

    def test_install_failure_does_not_execute_program(self):
        self.env["INSTALL_STATUS"] = "9"
        result = self.run_launcher("codex")
        self.assertEqual(result.returncode, 9, result.stderr)
        self.assertEqual(len(self.calls()), 1)


if __name__ == "__main__":
    unittest.main()
