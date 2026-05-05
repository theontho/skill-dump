#!/usr/bin/env python3
"""Download audio with yt-dlp and transcribe it with whisper.cpp/whisper-cpp.

The helper saves a cleaned, original-language transcript for each URL. It asks
whisper.cpp for speaker-turn/diarization output when supported, then normalizes
speaker labels to "Speaker 1" through "Speaker 5".
"""

from __future__ import annotations

import argparse
import html
import os
import re
import shutil
import subprocess
import sys
import tempfile


WHISPER_BIN_CANDIDATES = ("whisper-cli", "whisper-cpp", "main")
SPEAKER_LIMIT = 5

_BAD_FILENAME_CHARS_RE = re.compile(r"[<>:\"/\\|?*\x00-\x1f]")
_SRT_INDEX_RE = re.compile(r"^\d+$")
_SRT_TIMESTAMP_RE = re.compile(r"^\d{2}:\d{2}:\d{2}[,.]\d{3}\s+-->\s+")
_BRACKET_TIMESTAMP_RE = re.compile(
    r"^\s*\[[0-9:. ,]+(?:-->|-+>)[0-9:. ,]+\]\s*",
)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_SPEAKER_LABEL_RE = re.compile(
    r"^\s*(?:\[?\s*(speaker|spk|voice)[ _-]*(\d{1,2})\s*\]?|"
    r"\[?\s*(male|female|man|woman)[ _-]*(speaker)?[ _-]*(\d{1,2})?\s*\]?)"
    r"\s*[:\-]\s*(.*)$",
    re.IGNORECASE,
)
_SPEAKER_TURN_RE = re.compile(
    r"\s*(?:\[SPEAKER_TURN\]|\[speaker turn\]|\(speaker turn\))\s*",
    re.IGNORECASE,
)


def detect_whisper_binary(explicit_path: str | None = None) -> str | None:
    """Return a usable whisper.cpp binary path, or None if none is found."""
    if explicit_path:
        return explicit_path if os.path.exists(explicit_path) else None

    for name in WHISPER_BIN_CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    return None


def sanitize_filename(name: str) -> str:
    """Return a filesystem-safe media title."""
    safe = _BAD_FILENAME_CHARS_RE.sub("_", name)
    safe = safe.strip(". ")
    return safe or "transcript"


def command_help(command: str) -> str:
    """Return help text for *command*, or an empty string if probing fails."""
    try:
        result = subprocess.run(
            [command, "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return f"{result.stdout}\n{result.stderr}"


def diarization_flags(help_text: str, requested: bool) -> list[str]:
    """Return whisper.cpp diarization flags supported by the binary help text."""
    if not requested:
        return []
    if "--diarize" in help_text:
        return ["--diarize"]
    if "--tinydiarize" in help_text:
        return ["--tinydiarize"]
    if "-tdrz" in help_text:
        return ["-tdrz"]
    return []


def build_whisper_command(
    whisper_bin: str,
    audio_path: str,
    output_base: str,
    model: str,
    language: str,
    help_text: str,
    request_diarize: bool,
    translate_to_english: bool,
) -> list[str]:
    """Build the whisper.cpp command used to create a text transcript."""
    cmd = [
        whisper_bin,
        "-m",
        model,
        "-f",
        audio_path,
        "-of",
        output_base,
        "-otxt",
    ]

    if language and language != "auto":
        cmd.extend(["-l", language])
    if translate_to_english:
        cmd.append("-tr")
    cmd.extend(diarization_flags(help_text, request_diarize))
    return cmd


def download_audio(url: str, tmpdir: str) -> tuple[str, str] | None:
    """Download and extract audio for *url*, returning (title, audio_path)."""
    output_template = os.path.join(tmpdir, "%(title)s.%(ext)s")
    cmd = [
        "yt-dlp",
        "-x",
        "--audio-format",
        "wav",
        "--audio-quality",
        "0",
        "--output",
        output_template,
        "--print",
        "%(title)s",
        url,
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        print(f"Error: timed out downloading audio for {url}", file=sys.stderr)
        return None
    except FileNotFoundError:
        print("Error: yt-dlp not found. Install with: pip install yt-dlp", file=sys.stderr)
        return None

    if result.returncode != 0:
        print(f"yt-dlp error for {url}:\n{result.stderr}", file=sys.stderr)
        return None

    title = (result.stdout.strip().splitlines() or ["transcript"])[0]
    audio_files = [
        os.path.join(tmpdir, name)
        for name in os.listdir(tmpdir)
        if name.lower().endswith((".wav", ".mp3", ".m4a", ".opus", ".flac", ".aac"))
    ]
    if not audio_files:
        print(f"No audio file found for: {url}", file=sys.stderr)
        return None
    return title, sorted(audio_files)[0]


def _strip_markup(line: str) -> str:
    line = html.unescape(_HTML_TAG_RE.sub("", line))
    line = _BRACKET_TIMESTAMP_RE.sub("", line)
    return line.strip()


def _normalize_speaker_label(
    raw_id: str | None,
    gender: str | None,
    speaker_map: dict[str, int],
) -> str:
    raw_key = raw_id or str(len(speaker_map) + 1)
    if raw_key not in speaker_map:
        speaker_map[raw_key] = min(len(speaker_map) + 1, SPEAKER_LIMIT)
    label = f"Speaker {speaker_map[raw_key]}"
    if gender:
        normalized_gender = {
            "man": "male",
            "woman": "female",
        }.get(gender.lower(), gender.lower())
        label = f"{label} ({normalized_gender})"
    return label


def clean_transcript(raw_text: str, max_speakers: int = SPEAKER_LIMIT) -> str:
    """Clean whisper output and normalize speaker labels."""
    speaker_map: dict[str, int] = {}
    current_turn = 0
    output: list[str] = []
    seen_blank = False

    for raw_line in raw_text.splitlines():
        line = _strip_markup(raw_line)
        if not line:
            if output and not seen_blank:
                output.append("")
                seen_blank = True
            continue
        if _SRT_INDEX_RE.match(line) or _SRT_TIMESTAMP_RE.match(line):
            continue

        speaker_match = _SPEAKER_LABEL_RE.match(line)
        if speaker_match:
            _, speaker_id, gender_only, _, gender_speaker_id, text = speaker_match.groups()
            speaker_id = speaker_id or gender_speaker_id
            gender = gender_only
            speaker = _normalize_speaker_label(speaker_id, gender, speaker_map)
            text = _SPEAKER_TURN_RE.sub("", text).strip()
            line = f"{speaker}: {text}" if text else f"{speaker}:"
        else:
            turn_match = _SPEAKER_TURN_RE.search(line)
            if turn_match:
                current_turn = (current_turn % min(max_speakers, SPEAKER_LIMIT)) + 1
                line = _SPEAKER_TURN_RE.sub("", line).strip()
                if line:
                    line = f"Speaker {current_turn}: {line}"
                else:
                    continue

        if line:
            output.append(line)
            seen_blank = False

    while output and output[-1] == "":
        output.pop()
    return "\n".join(output)


def read_whisper_output(output_base: str, stdout: str) -> str:
    """Read whisper output from .txt, falling back to captured stdout."""
    txt_path = f"{output_base}.txt"
    if os.path.exists(txt_path):
        with open(txt_path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    return stdout


def transcribe_url(
    url: str,
    output_dir: str,
    whisper_bin: str,
    model: str,
    language: str,
    request_diarize: bool,
    translate_to_english: bool,
) -> str | None:
    """Download, transcribe, clean, and save one URL transcript."""
    with tempfile.TemporaryDirectory() as tmpdir:
        downloaded = download_audio(url, tmpdir)
        if downloaded is None:
            return None

        title, audio_path = downloaded
        safe_title = sanitize_filename(title)
        output_base = os.path.join(tmpdir, "whisper-output")
        help_text = command_help(whisper_bin)
        command = build_whisper_command(
            whisper_bin,
            audio_path,
            output_base,
            model,
            language,
            help_text,
            request_diarize,
            translate_to_english,
        )

        if request_diarize and not diarization_flags(help_text, True):
            print(
                "Note: whisper binary did not advertise diarization flags; "
                "continuing without speaker detection.",
                file=sys.stderr,
            )

        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=7200)
        except subprocess.TimeoutExpired:
            print(f"Error: timed out transcribing audio for {url}", file=sys.stderr)
            return None
        except FileNotFoundError:
            print(f"Error: whisper binary not found: {whisper_bin}", file=sys.stderr)
            return None

        if result.returncode != 0:
            print(f"whisper error for {url}:\n{result.stderr}", file=sys.stderr)
            return None

        raw_transcript = read_whisper_output(output_base, result.stdout)
        cleaned = clean_transcript(raw_transcript)
        if not cleaned:
            print(f"Transcript was empty after cleanup for: {url}", file=sys.stderr)
            return None

        output_path = os.path.join(output_dir, f"{safe_title}.txt")
        # Transcripts can contain private speech; keep new files readable only by the current user.
        output_fd = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(output_fd, "w", encoding="utf-8") as fh:
            fh.write(cleaned)
            fh.write("\n")

        print(f"Saved: {output_path}")
        return output_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download audio and transcribe it with whisper.cpp/whisper-cpp.",
    )
    parser.add_argument("urls", nargs="+", help="One or more video/audio URLs.")
    parser.add_argument(
        "--model",
        required=True,
        help="Path to a whisper.cpp ggml model file.",
    )
    parser.add_argument(
        "--output-dir",
        "-o",
        default=".",
        help="Directory to save .txt transcript files (default: current directory).",
    )
    parser.add_argument(
        "--language",
        "-l",
        default="auto",
        help="Spoken language code for whisper.cpp, or 'auto' for detection.",
    )
    parser.add_argument(
        "--whisper-bin",
        help="Path to whisper.cpp binary. Defaults to whisper-cli, whisper-cpp, or main.",
    )
    parser.add_argument(
        "--no-diarize",
        action="store_true",
        help="Do not request speaker diarization/speaker-turn flags.",
    )
    parser.add_argument(
        "--translate-to-english",
        action="store_true",
        help="Ask Whisper to translate to English instead of preserving original language.",
    )
    args = parser.parse_args()

    # New transcript directories default to owner-only access because transcripts may contain private speech.
    os.makedirs(args.output_dir, mode=0o700, exist_ok=True)
    whisper_bin = detect_whisper_binary(args.whisper_bin)
    if whisper_bin is None:
        print(
            "Error: whisper.cpp binary not found. Install whisper-cpp or pass --whisper-bin.",
            file=sys.stderr,
        )
        sys.exit(1)

    any_failed = False
    for url in args.urls:
        print(f"\nProcessing: {url}")
        result = transcribe_url(
            url,
            args.output_dir,
            whisper_bin,
            args.model,
            args.language,
            not args.no_diarize,
            args.translate_to_english,
        )
        if result is None:
            any_failed = True

    sys.exit(1 if any_failed else 0)


if __name__ == "__main__":
    main()
