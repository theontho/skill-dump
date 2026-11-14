"""Shared isolated CLI fixtures; never reads real agent data or uses the network."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = "WGSExtract/wgsextract-cli"


class UsageFixture(unittest.TestCase):
    def setUp(self):
        output = ROOT.parents[1] / "out"
        output.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=output)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.state = self.base / "state"
        self.copilot = self.base / "copilot"
        self.claude = self.base / "claude"
        self.db = self.base / "opencode.db"
        self.config = self.base / "config.toml"
        self.config.write_text(
            f"storage = {json.dumps(str(self.state))}\n"
            'machine = "fixture-machine"\ntimezone = "America/Los_Angeles"\n'
            f"copilot = {json.dumps(str(self.copilot))}\n"
            f"opencode = {json.dumps(str(self.db))}\n"
            f"claude = {json.dumps(str(self.claude))}\n"
            f'[[repositories]]\nname = "{REPO}"\npaths = ["/work/repo"]\n'
        )

    def run_cli(self, *args, ok=True, env=None):
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "agent-usage"),
                "--config",
                str(self.config),
                *args,
            ],
            text=True,
            check=False,
            capture_output=True,
            timeout=30,
            env=env,
        )
        self.assertEqual(
            result.returncode, 0 if ok else 1, result.stdout + result.stderr
        )
        return result.stdout + result.stderr

    def events(self, name, rows, repo=REPO, cwd="/work/repo", git_root=None):
        folder = self.copilot / name
        folder.mkdir(parents=True)
        workspace = f"repository: {repo}\ncwd: {cwd}\n"
        if git_root is not None:
            workspace += f"git_root: {git_root}\n"
        (folder / "workspace.yaml").write_text(workspace)
        (folder / "events.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in rows)
        )

    def usage(self, identifier="one", **changes):
        usage = {
            "inputTokens": 100,
            "cacheReadTokens": 40,
            "cacheWriteTokens": 20,
            "outputTokens": 10,
            "reasoningTokens": 4,
            "model": "gpt-6.1-sol",
            "cost": 999,
            "accounting": {"usageId": identifier, "sourceSessionId": "child"},
            "parentToolCallId": "parent-tool",
        }
        usage.update(changes)
        return {
            "type": "session.usage_record",
            "timestamp": "2026-11-12T08:30:00Z",
            "data": {"usage": usage},
        }

    def daily(self, day="2026-11-12"):
        return json.loads((self.state / "fixture-machine" / f"{day}.json").read_text())
