"""Verify token routing with fake credentials, never a real 1Password account."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent


class HfOpTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        op = self.root / "op-session"
        op.write_text("""#!/bin/bash
[[ "$*" == "exec op read --no-newline op://test/reference/field" ]] || exit 64
[[ "${TEST_OP_FAILURE:-0}" == 0 ]] || exit 77
printf '%s' fake-op-token
""")
        op.chmod(0o755)
        self.env = dict(os.environ, PATH=f"{self.root}:{os.environ['PATH']}",
                        HF_HOME=str(self.root / "hf"), HF_TOKEN_OP_REF="op://test/reference/field",
                        HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
        self.env.pop("HF_TOKEN", None)

    def run_command(self, expected_token):
        return subprocess.run([
            str(ROOT / "hf-op"), sys.executable, "-c",
            "import os,sys; assert os.environ['HF_TOKEN'] == sys.argv[1]; "
            "assert os.environ['HF_HUB_OFFLINE'] == '0'; "
            "assert os.environ['TRANSFORMERS_OFFLINE'] == '0'; print('command ran')",
            expected_token,
        ], env=self.env, text=True, capture_output=True, timeout=5)

    def test_op_token_reaches_only_child_environment(self):
        result = self.run_command("fake-op-token")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "command ran\n")
        self.assertEqual(result.stderr, "")

    def test_existing_hf_login_is_used_when_op_is_unavailable(self):
        self.env["TEST_OP_FAILURE"] = "1"
        token = Path(self.env["HF_HOME"]) / "token"
        token.parent.mkdir()
        token.write_text("fake-saved-token\n")
        result = self.run_command("fake-saved-token")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "command ran\n")
        self.assertIn("using the stored Hugging Face login", result.stderr)
        self.assertNotIn("fake-saved-token", result.stderr)

    def test_no_token_fails_without_executing_command(self):
        self.env["TEST_OP_FAILURE"] = "1"
        result = self.run_command("unused")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("hf-op: no token", result.stderr)


if __name__ == "__main__":
    unittest.main()
