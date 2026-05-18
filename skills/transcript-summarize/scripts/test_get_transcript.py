#!/usr/bin/env python3
"""Focused tests for get_transcript.py."""

import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import get_transcript


class TranscriptFormattingTests(unittest.TestCase):
    def test_parse_srt_deduplicates_and_decodes_html_entities(self) -> None:
        srt = """
1
00:00:00,000 --> 00:00:01,000
&gt;&gt; Alice: Hello &amp; welcome

2
00:00:01,000 --> 00:00:02,000
&gt;&gt; Alice: Hello &amp; welcome

3
00:00:02,000 --> 00:00:03,000
Goodbye
"""

        self.assertEqual(
            get_transcript.parse_srt(srt),
            [">> Alice: Hello & welcome", "Goodbye"],
        )

    def test_format_transcript_marks_speaker_changes_and_adds_newlines(self) -> None:
        transcript = get_transcript.format_transcript([
            "Intro",
            ">> Alice: Hello",
            "> Bob: Hi",
            "Regular line",
        ])

        self.assertEqual(
            transcript,
            (
                "Intro\n\n"
                ">[SPEAKER CHANGE]>Alice: Hello\n\n"
                ">[SPEAKER CHANGE]>Bob: Hi\n"
                "Regular line"
            ),
        )


class DownloadCommandTests(unittest.TestCase):
    def test_download_transcript_uses_requested_subtitle_language(self) -> None:
        seen_commands: list[list[str]] = []

        def fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            seen_commands.append(cmd)
            out_template = cmd[cmd.index("--output") + 1]
            tmpdir = os.path.dirname(out_template)
            with open(os.path.join(tmpdir, "Example.en.srt"), "w", encoding="utf-8") as fh:
                fh.write(
                    "1\n"
                    "00:00:00,000 --> 00:00:01,000\n"
                    "&gt;&gt; Speaker: Hola\n"
                )
            return subprocess.CompletedProcess(cmd, 0, stdout="Example\n", stderr="")

        with tempfile.TemporaryDirectory() as output_dir:
            with patch("get_transcript.subprocess.run", side_effect=fake_run):
                path = get_transcript.download_transcript(
                    "https://example.com/video",
                    output_dir,
                    "node",
                    "es.*,es",
                )

            self.assertIsNotNone(path)
            self.assertIn("--sub-langs", seen_commands[0])
            self.assertEqual(
                seen_commands[0][seen_commands[0].index("--sub-langs") + 1],
                "es.*,es",
            )
            with open(path or "", encoding="utf-8") as fh:
                self.assertEqual(fh.read(), ">[SPEAKER CHANGE]>Speaker: Hola\n")


if __name__ == "__main__":
    unittest.main()
