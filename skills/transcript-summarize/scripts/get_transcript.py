#!/usr/bin/env python3
"""Download and clean video transcripts using yt-dlp and Whisper.

Usage:
    python3 get_transcript.py <url> [<url> ...] --output-dir DIR

The script downloads subtitles via yt-dlp. If no subtitles are found, it downloads
the audio and transcribes it using faster-whisper. It deduplicates rolling-caption
SRT/VTT entries into clean plain text, normalizes speaker-change indicators to
'>[SPEAKER CHANGE]> \n' with blank-line separation, and saves each transcript as
<VideoTitle>.txt in the output directory.
"""

import argparse
import html
import os
import re
import shutil
import subprocess
import sys
import tempfile
from typing import List, Optional, Tuple

# Optional dependency for audio transcription
try:
    from faster_whisper import WhisperModel
    HAS_WHISPER = True
except ImportError:
    HAS_WHISPER = False


# ---------------------------------------------------------------------------
# JavaScript-runtime detection
# ---------------------------------------------------------------------------

def detect_nodejs() -> Optional[str]:
    """Return 'node' if Node.js is installed and working, otherwise None."""
    if shutil.which("node"):
        try:
            r = subprocess.run(
                ["node", "--version"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                return "node"
        except Exception:
            pass
    return None


def build_yt_dlp_player_args(js_runtime: Optional[str]) -> List[str]:
    """
    Return extra yt-dlp flags that maximise subtitle-download success.
    """
    if js_runtime == "node":
        return ["--extractor-args", "youtube:player_client=web,ios,mweb"]
    else:
        return ["--extractor-args", "youtube:player_client=ios,mweb,web"]


# ---------------------------------------------------------------------------
# SRT / VTT parsing
# ---------------------------------------------------------------------------

_SRT_INDEX_RE = re.compile(r"^\d+$")
_SRT_TIMESTAMP_RE = re.compile(r"^\d{2}:\d{2}:\d{2}[,\.]\d{3}")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_VTT_META_RE = re.compile(r"^(WEBVTT|NOTE|STYLE|REGION)")
_VTT_TIMESTAMP_CLEAN_RE = re.compile(
    r"\s+(align|position|line|size|vertical):[^\s]+"
)


def _strip_tags(text: str) -> str:
    return html.unescape(_HTML_TAG_RE.sub("", text)).strip()


def parse_srt(content: str) -> List[str]:
    """
    Parse SRT/VTT content and return deduplicated text lines.
    """
    blocks = re.split(r"\n[ \t]*\n", content.strip())
    seen: set[str] = set()
    lines: List[str] = []

    for block in blocks:
        block_lines = [l.strip() for l in block.strip().splitlines()]
        texts: List[str] = []
        for ln in block_lines:
            if not ln:
                continue
            if _SRT_INDEX_RE.match(ln):
                continue
            if _SRT_TIMESTAMP_RE.match(ln):
                continue
            clean = _strip_tags(ln)
            if clean:
                texts.append(clean)

        for t in texts:
            if t not in seen:
                seen.add(t)
                lines.append(t)

    return lines


def convert_vtt_to_srt(content: str) -> str:
    """Convert WebVTT to a minimal SRT-compatible string for parse_srt()."""
    out: List[str] = []
    counter = 1
    src_lines = content.splitlines()
    i = 0

    while i < len(src_lines):
        raw = src_lines[i]
        stripped = raw.strip()

        if _VTT_META_RE.match(stripped):
            while i < len(src_lines) and src_lines[i].strip():
                i += 1
            i += 1
            continue

        if "-->" in stripped:
            ts_line = re.sub(r"(\d{2}:\d{2}:\d{2})\.(\d{3})", r"\1,\2", stripped)
            ts_line = _VTT_TIMESTAMP_CLEAN_RE.sub("", ts_line)
            out.append(str(counter))
            out.append(ts_line)
            counter += 1
            i += 1
            while i < len(src_lines) and src_lines[i].strip():
                out.append(src_lines[i])
                i += 1
            out.append("")
            continue

        i += 1

    return "\n".join(out)


# ---------------------------------------------------------------------------
# Transcript formatting
# ---------------------------------------------------------------------------

def format_transcript(lines: List[str]) -> str:
    """
    Join lines into a readable transcript.
    """
    out: List[str] = []
    for line in lines:
        speaker_line = normalize_speaker_change(line)
        if speaker_line:
            if out:
                out.append("")
            out.append(speaker_line)
        else:
            out.append(line)
    return "\n".join(out)


def normalize_speaker_change(line: str) -> Optional[str]:
    """Return a normalized speaker-change line, or None for regular text."""
    if line.startswith(">>"):
        return ">[SPEAKER CHANGE]> \n" + line[2:].lstrip()
    if line.startswith(">"):
        return ">[SPEAKER CHANGE]> \n" + line[1:].lstrip()
    return None


# ---------------------------------------------------------------------------
# Whisper Transcription and Diarization
# ---------------------------------------------------------------------------

def transcribe_with_whisper(
    audio_path: str,
    language: Optional[str] = None,
    model_size: str = "base"
) -> List[str]:
    """
    Transcribe audio using faster-whisper.
    """
    if not HAS_WHISPER:
        print("Error: faster-whisper not installed.", file=sys.stderr)
        return []

    print(f"Transcribing audio with Whisper ({model_size})...")
    
    # Map sub_langs like 'en.*,en' to a simple 'en'
    whisper_lang = None
    if language:
        match = re.search(r"([a-z]{2})", language.lower())
        if match:
            whisper_lang = match.group(1)

    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, info = model.transcribe(audio_path, language=whisper_lang, beam_size=5)
    
    transcript_lines = []
    for segment in segments:
        text = segment.text.strip()
        if text:
            transcript_lines.append(text)

    return transcript_lines


# ---------------------------------------------------------------------------
# File-name helpers
# ---------------------------------------------------------------------------

def sanitize_filename(name: str) -> str:
    """Return a filesystem-safe version of a video title."""
    safe = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", name)
    safe = safe.strip(". ")
    return safe or "transcript"


# ---------------------------------------------------------------------------
# Main download logic
# ---------------------------------------------------------------------------

def download_transcript(
    url: str,
    output_dir: str,
    js_runtime: Optional[str],
    sub_langs: str,
    model_size: str = "base"
) -> Optional[str]:
    """
    Download, clean, and save a transcript for *url*.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        player_args = build_yt_dlp_player_args(js_runtime)

        # 1. Get metadata
        meta_cmd = [
            "yt-dlp",
            "--skip-download",
            "--print", "%(title)s",
            *player_args,
            url,
        ]
        try:
            meta_result = subprocess.run(
                meta_cmd, capture_output=True, text=True, timeout=30,
            )
            if meta_result.returncode != 0:
                print(f"Error getting metadata for {url}:\n{meta_result.stderr}", file=sys.stderr)
                return None
            video_title = meta_result.stdout.strip().splitlines()[0]
        except Exception as e:
            print(f"Error getting metadata for {url}: {e}", file=sys.stderr)
            return None

        safe_title = sanitize_filename(video_title)

        # 2. Try to download subtitles
        dl_cmd = [
            "yt-dlp",
            "--skip-download",
            "--write-subs",
            "--write-auto-subs",
            "--sub-langs", sub_langs,
            "--sub-format", "srt/vtt/best",
            "--convert-subs", "srt",
            "--output", os.path.join(tmpdir, "sub.%(ext)s"),
            *player_args,
            url,
        ]

        try:
            dl_result = subprocess.run(
                dl_cmd, capture_output=True, text=True, timeout=120,
            )
        except Exception as e:
            print(f"yt-dlp error for {url}: {e}", file=sys.stderr)
            return None

        # Locate subtitle file
        raw_content: Optional[str] = None
        is_vtt = False
        for fname in sorted(os.listdir(tmpdir)):
            if fname.startswith("sub."):
                if fname.endswith(".srt"):
                    with open(os.path.join(tmpdir, fname), encoding="utf-8", errors="replace") as fh:
                        raw_content = fh.read()
                    is_vtt = False
                    break
                if fname.endswith(".vtt"):
                    with open(os.path.join(tmpdir, fname), encoding="utf-8", errors="replace") as fh:
                        raw_content = fh.read()
                    is_vtt = True

        lines: List[str] = []
        if raw_content:
            if is_vtt:
                raw_content = convert_vtt_to_srt(raw_content)
            lines = parse_srt(raw_content)
        else:
            # 3. Fallback to audio download and Whisper transcription
            print(f"No subtitles found for {url}. Downloading audio for transcription...", file=sys.stderr)
            if not HAS_WHISPER:
                print("Error: Subtitles missing and faster-whisper not installed. Cannot proceed.", file=sys.stderr)
                return None
            
            audio_dl_cmd = [
                "yt-dlp",
                "--extract-audio",
                "--audio-format", "mp3",
                "--output", os.path.join(tmpdir, "audio.%(ext)s"),
                *player_args,
                url,
            ]
            try:
                subprocess.run(audio_dl_cmd, check=True, capture_output=True, timeout=300)
                audio_path = os.path.join(tmpdir, "audio.mp3")
                if os.path.exists(audio_path):
                    lines = transcribe_with_whisper(audio_path, sub_langs, model_size)
            except Exception as e:
                print(f"Audio download/transcription failed for {url}: {e}", file=sys.stderr)
                return None

        if not lines:
            print(f"Transcript was empty for: {url}", file=sys.stderr)
            return None

        transcript = format_transcript(lines)

        output_path = os.path.join(output_dir, f"{safe_title}.txt")
        with open(output_path, "w", encoding="utf-8") as fh:
            fh.write(transcript)
            fh.write("\n")

        print(f"Saved: {output_path}")
        return output_path


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download and clean video transcripts using yt-dlp and Whisper.",
    )
    parser.add_argument("urls", nargs="+", help="One or more video URLs.")
    parser.add_argument(
        "--output-dir", "-o", default=".",
        help="Directory to save .txt transcript files."
    )
    parser.add_argument(
        "--sub-langs", default="en.*,en",
        help="Subtitle language selector (e.g., 'en.*,en')."
    )
    parser.add_argument(
        "--model", default="base",
        help="Whisper model size (tiny, base, small, medium, large-v3). Default: base."
    )
    args = parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)

    js_runtime = detect_nodejs()
    if js_runtime:
        print(f"JS runtime detected: {js_runtime}")
    else:
        print("Note: Node.js not found. Falling back to JS-free player clients.")

    if not HAS_WHISPER:
        print("Note: faster-whisper not found. Audio transcription fallback will be unavailable.")

    any_failed = False
    for url in args.urls:
        print(f"\nProcessing: {url}")
        result = download_transcript(
            url, args.output_dir, js_runtime, args.sub_langs, args.model
        )
        if result is None:
            any_failed = True

    sys.exit(1 if any_failed else 0)


if __name__ == "__main__":
    main()
