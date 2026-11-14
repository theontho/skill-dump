"""Synthetic, counts-only CLI integration tests; no real user records or network."""

import argparse
import json
import os
import plistlib
import runpy
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

from metadata import project
from sources import record
from usage import estimate

ROOT = Path(__file__).resolve().parent
REPO = "WGSExtract/wgsextract-cli"


class UsageTests(unittest.TestCase):
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

    def events(self, name, rows, repo=REPO):
        folder = self.copilot / name
        folder.mkdir(parents=True)
        (folder / "workspace.yaml").write_text(f"repository: {repo}\ncwd: /work/repo\n")
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

    def test_child_usage_replay_and_cached_prompt_are_not_double_counted(self):
        event = self.usage()
        self.events(
            "root",
            [
                {
                    "type": "user.message",
                    "data": {"content": "PRIVATE fixture message"},
                },
                event,
                self.usage(
                    "two",
                    inputTokens=300,
                    cacheReadTokens=0,
                    cacheWriteTokens=0,
                    parentToolCallId=None,
                ),
            ],
        )
        self.events("replayed-child", [event])
        self.run_cli("collect", "--days", "1", "--end", "2026-11-12", "--no-github")
        doc = self.daily()
        total = doc["repositories"][REPO]
        self.assertEqual(total["calls"], 2)
        self.assertEqual(total["prompt_uncached"], 360)
        self.assertEqual(total["prompt_cached"], 40)
        self.assertEqual(total["prompt_mean"], 200)
        self.assertEqual(total["prompt_p90"], 300)
        self.assertAlmostEqual(total["estimated_usd"], 0.000934)
        self.assertIsNone(total["merged_prs"])
        self.assertEqual(
            {row["role"] for row in doc["agents"]}, {"supervisor", "implementer"}
        )
        self.assertNotIn("PRIVATE", json.dumps(doc))
        self.assertNotIn("parent-tool", json.dumps(doc))
        self.run_cli("collect", "--days", "1", "--end", "2026-11-12", "--no-github")
        self.assertEqual(self.daily()["repositories"], doc["repositories"])

    def test_local_day_boundary_and_unknown_model_cost_is_not_zero(self):
        self.events(
            "root",
            [
                dict(self.usage("utc"), timestamp="2026-11-12T07:59:59Z"),
                self.usage("unknown", model="not-a-public-model"),
            ],
        )
        self.run_cli("collect", "--days", "2", "--end", "2026-11-12", "--no-github")
        self.assertEqual(self.daily("2026-11-11")["repositories"][REPO]["calls"], 1)
        total = self.daily()["repositories"][REPO]
        self.assertIsNone(total["estimated_usd"])
        self.assertEqual(total["unpriced_records"], 1)

    def test_explicit_repository_wins_over_a_checkout_path_match(self):
        self.events("unrelated", [self.usage()], repo="another/project")
        self.run_cli("collect", "--days", "1", "--end", "2026-11-12", "--no-github")
        self.assertEqual(self.daily()["repositories"][REPO]["calls"], 0)

    def test_public_rate_estimate_handles_long_context_without_tiering_session_totals(
        self,
    ):
        rates = json.loads((ROOT / "rates.json").read_text())
        call = record(
            "copilot",
            "agent",
            REPO,
            "",
            "gpt-6.1-sol",
            "supervisor",
            300_000,
            0,
            0,
            100,
        )
        self.assertAlmostEqual(estimate(call, rates), 1.2015)
        call["calls"] = 0  # Lifetime totals cannot reveal individual context tiers.
        self.assertAlmostEqual(estimate(call, rates), 0.601)
        call.update(
            calls=1,
            prompt_uncached=1_000_000,
            cache_write=1_000_000,
            model="claude-opus-5.5",
            cache_ttl_seconds=3600,
            output=0,
        )
        self.assertAlmostEqual(estimate(call, rates), 8)

    def test_opencode_wal_counts_are_not_misrepresented_as_api_calls(self):
        connection = sqlite3.connect(self.db)
        self.addCleanup(connection.close)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript("""
            CREATE TABLE session_v2 (
              id TEXT, directory TEXT, time_created INTEGER, model TEXT,
              tokens_input INTEGER, tokens_output INTEGER, tokens_reasoning INTEGER,
              tokens_cache_read INTEGER, tokens_cache_write INTEGER, cost REAL,
              parent_id TEXT);
            CREATE TABLE account (secret TEXT);
            INSERT INTO account VALUES ('PRIVATE fixture account');
        """)
        stamp = int(datetime(2026, 11, 12, 12, tzinfo=UTC).timestamp() * 1000)
        for directory in ("/work/repo/scratch/child", "/work/repository-neighbor"):
            connection.execute(
                "INSERT INTO session_v2 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "session",
                    directory,
                    stamp,
                    '{"id":"claude-opus-5-5"}',
                    20,
                    10,
                    3,
                    70,
                    10,
                    999,
                    None,
                ),
            )
        connection.commit()
        self.run_cli("collect", "--days", "1", "--end", "2026-11-12", "--no-github")
        doc = self.daily()
        total = doc["repositories"][REPO]
        self.assertEqual(total["calls"], 0)
        self.assertEqual(total["session_records"], 1)
        self.assertEqual(total["prompt_uncached"], 30)
        self.assertEqual(total["prompt_cached"], 70)
        self.assertIsNone(total["prompt_p90"])
        self.assertAlmostEqual(total["estimated_usd"], 0.000344)
        self.assertFalse(list(self.state.glob(".opencode-*")))
        self.assertNotIn("PRIVATE", json.dumps(doc))
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM session_v2").fetchone()[0], 2
        )

    def test_claude_message_chunks_count_usage_once_without_decoding_content(self):
        folder = self.claude / "project"
        folder.mkdir(parents=True)
        event: dict[str, Any] = {
            "type": "assistant",
            "timestamp": "2026-11-12T12:00:00Z",
            "cwd": "/work/repo",
            "isSidechain": True,
            "requestId": "request",
            "message": {
                "id": "msg",
                "model": "claude-opus-4-6-20260205",
                "content": [
                    {"text": "PRIVATE fixture response", "usage": {"input_tokens": 999}}
                ],
                "usage": {
                    "input_tokens": 20,
                    "cache_read_input_tokens": 70,
                    "cache_creation_input_tokens": 10,
                    "output_tokens": 5,
                },
            },
        }
        first = json.dumps(event) + "\n"
        event["message"]["usage"]["output_tokens"] = 17
        (folder / "session.jsonl").write_text(first + json.dumps(event) + "\n" + first)
        self.run_cli("collect", "--days", "1", "--end", "2026-11-12", "--no-github")
        doc = self.daily()
        total = doc["repositories"][REPO]
        self.assertEqual(total["calls"], 1)
        self.assertEqual(total["prompt_uncached"], 30)
        self.assertEqual(total["prompt_cached"], 70)
        self.assertEqual(total["output"], 17)
        self.assertEqual(doc["agents"][0]["role"], "implementer")
        self.assertNotIn("PRIVATE", json.dumps(doc))

    def fake_gh(self):
        binary = self.base / "gh"
        binary.write_text(
            "#!" + sys.executable + "\n"
            "import json,sys\n"
            "if 'graphql' not in sys.argv: sys.exit(99)\n"
            "page2 = any(x.startswith('cursor=') for x in sys.argv)\n"
            "print(json.dumps({'data':{'search':{'issueCount':2,"
            "'nodes':[{'mergedAt':'2026-11-12T12:00:00Z' if page2 else '2026-11-12T07:00:00Z'}],"
            "'pageInfo':{'hasNextPage':not page2,'endCursor':'next'}}}}))\n"
        )
        binary.chmod(0o755)
        return dict(os.environ, PATH=str(self.base) + os.pathsep + os.environ["PATH"])

    def test_weekly_report_paginates_pr_metadata_and_dry_run_never_posts(self):
        self.events("root", [self.usage()])
        self.run_cli(
            "collect", "--days", "14", "--end", "2026-11-12", env=self.fake_gh()
        )
        self.assertEqual(self.daily()["repositories"][REPO]["merged_prs"], 1)
        self.assertEqual(
            self.daily("2026-11-11")["repositories"][REPO]["merged_prs"], 1
        )
        report = self.run_cli(
            "report",
            "--weekly",
            "--repo",
            REPO,
            "--end",
            "2026-11-12",
            "--post-issue",
            "765",
            "--dry-run",
            env=self.fake_gh(),
        )
        self.assertTrue(report.startswith("(AI):"))
        self.assertIn("Machine: fixture-machine", report)
        self.assertIn("This week 2026-11-06–2026-11-12", report)
        self.assertIn("| 2 | 55 |", report)
        self.assertIn("| 0 | n/a |", report)
        self.run_cli(
            "report", "--weekly", "--repo", REPO, "--post-issue", "766", ok=False
        )
        self.run_cli(
            "report",
            "--weekly",
            "--repo",
            "other/repo",
            "--post-issue",
            "765",
            ok=False,
        )

    def test_missing_days_and_github_failure_cannot_create_misleading_ratios(self):
        self.events("root", [self.usage()])
        self.run_cli("collect", "--days", "1", "--end", "2026-11-12", "--no-github")
        report = self.run_cli(
            "report", "--weekly", "--repo", REPO, "--end", "2026-11-12"
        )
        self.assertIn("| 1/7 |", report)
        self.assertIn("| n/a | n/a |", report)
        env = self.fake_gh()
        (self.base / "gh").write_text("#!/bin/sh\nexit 1\n")
        self.run_cli("collect", "--days", "2", "--end", "2026-11-12", env=env, ok=False)
        self.assertFalse((self.state / "fixture-machine/2026-11-11.json").exists())

    def test_metadata_projection_skips_nested_bodies_and_escaped_strings(self):
        source = '{"message":{"content":[{"text":"a \\"quote\\" } \\\\ slash"}],"usage":{"n":3}}}'
        self.assertEqual(
            project(source, {"message": {"usage": {"n": True}}}),
            {"message": {"usage": {"n": 3}}},
        )
        with self.assertRaises(ValueError):
            project('{"message":', {"message": True})

    def test_launchd_schedule_is_collection_only(self):
        with (ROOT / "com.theontho.agent-usage.plist").open("rb") as stream:
            plist = plistlib.load(stream)
        self.assertEqual(plist["ProgramArguments"][-1], "collect")
        self.assertTrue(plist["Disabled"])
        self.assertIn("Hour", plist["StartCalendarInterval"])

    def test_launchd_opt_in_and_opt_out_persist_across_login(self):
        state = self.base / "launchd.json"
        state.write_text('{"disabled":true,"loaded":false}')
        binary = self.base / "launchctl"
        binary.write_text(
            "#!" + sys.executable + "\n"
            "import json,sys\nfrom pathlib import Path\n"
            f"path=Path({str(state)!r})\n"
            "state=json.loads(path.read_text())\naction=sys.argv[1]\n"
            "if action=='enable': state['disabled']=False\n"
            "elif action=='disable': state['disabled']=True\n"
            "elif action=='print': sys.exit(0 if state['loaded'] else 1)\n"
            "elif action=='bootstrap':\n"
            " if state['disabled']: sys.exit(1)\n"
            " state['loaded']=True\n"
            "elif action=='bootout': state['loaded']=False\n"
            "path.write_text(json.dumps(state))\n"
        )
        binary.chmod(0o755)
        program = runpy.run_path(str(ROOT / "agent-usage"))
        with (
            patch("platform.system", return_value="Darwin"),
            patch.dict(
                os.environ, PATH=str(self.base) + os.pathsep + os.environ["PATH"]
            ),
        ):
            for _ in range(2):
                program["timer"](argparse.Namespace(action="enable"))
            self.assertEqual(
                json.loads(state.read_text()), {"disabled": False, "loaded": True}
            )
            for _ in range(2):
                program["timer"](argparse.Namespace(action="disable"))
            after = json.loads(state.read_text())
            self.assertEqual(after, {"disabled": True, "loaded": False})


if __name__ == "__main__":
    unittest.main()
