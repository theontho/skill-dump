#!/usr/bin/env python3
"""Focused tests for transcribe_audio.py."""

import unittest

import transcribe_audio


class WhisperCommandTests(unittest.TestCase):
    def test_build_whisper_command_uses_language_and_diarization_when_supported(self) -> None:
        command = transcribe_audio.build_whisper_command(
            "whisper-cli",
            "/tmp/audio.wav",
            "/tmp/out",
            "/models/ggml.bin",
            "es",
            "usage: whisper-cli --diarize",
            True,
            False,
        )

        self.assertEqual(command[:7], [
            "whisper-cli",
            "-m",
            "/models/ggml.bin",
            "-f",
            "/tmp/audio.wav",
            "-of",
            "/tmp/out",
        ])
        self.assertIn("-otxt", command)
        self.assertIn("-l", command)
        self.assertEqual(command[command.index("-l") + 1], "es")
        self.assertIn("--diarize", command)

    def test_build_whisper_command_omits_auto_language_and_unsupported_diarization(self) -> None:
        command = transcribe_audio.build_whisper_command(
            "whisper-cli",
            "/tmp/audio.wav",
            "/tmp/out",
            "/models/ggml.bin",
            "auto",
            "usage: whisper-cli",
            True,
            True,
        )

        self.assertNotIn("-l", command)
        self.assertNotIn("--diarize", command)
        self.assertIn("-tr", command)


class TranscriptCleanupTests(unittest.TestCase):
    def test_clean_transcript_normalizes_speaker_labels_and_preserves_gender(self) -> None:
        raw = """
1
00:00:00,000 --> 00:00:01,000
[00:00:00.000 --> 00:00:01.000] [SPEAKER_00]: Hola &amp; welcome
Female speaker 2: Bonjour
<b>plain line</b>
"""

        self.assertEqual(
            transcribe_audio.clean_transcript(raw),
            "Speaker 1: Hola & welcome\nSpeaker 2 (female): Bonjour\nplain line",
        )

    def test_clean_transcript_labels_speaker_turn_markers(self) -> None:
        raw = """
[SPEAKER_TURN] Hello
continuing
[SPEAKER_TURN] Reply
"""

        self.assertEqual(
            transcribe_audio.clean_transcript(raw),
            "Speaker 1: Hello\ncontinuing\nSpeaker 2: Reply",
        )

    def test_clean_transcript_reuses_unidentified_gender_labels(self) -> None:
        raw = """
Female speaker: First line
Female speaker: Second line
Male speaker: Reply
"""

        self.assertEqual(
            transcribe_audio.clean_transcript(raw),
            "Speaker 1 (female): First line\n"
            "Speaker 1 (female): Second line\n"
            "Speaker 2 (male): Reply",
        )


if __name__ == "__main__":
    unittest.main()
