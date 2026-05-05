#!/usr/bin/env python3
"""Download and clean video transcripts using yt-dlp.

Usage:
    python get_transcript.py <url> [<url> ...] [--output-dir DIR]

The script downloads subtitles via yt-dlp, deduplicates rolling-caption SRT/VTT
entries into clean plain text, adds blank lines before '>>' speaker-change
indicators, and saves each transcript as <VideoTitle>.txt in the output directory.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile


# ---------------------------------------------------------------------------
# JavaScript-runtime detection
# ---------------------------------------------------------------------------

def detect_nodejs() -> str | None:
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


def build_yt_dlp_player_args(js_runtime: str | None) -> list[str]:
    """
    Return extra yt-dlp flags that maximise subtitle-download success.

    When Node.js is available, yt-dlp can use it for JS deobfuscation; we still
    pin a safe player-client sequence.  Without Node, we fall back to clients
    that do not require JS signature decryption (ios, mweb).
    """
    if js_runtime == "node":
        # web first (highest quality captions), node handles any JS challenge
        return ["--extractor-args", "youtube:player_client=web,ios,mweb"]
    else:
        # ios / mweb skip JS-heavy paths entirely
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
    return _HTML_TAG_RE.sub("", text).strip()


def parse_srt(content: str) -> list[str]:
    """
    Parse SRT/VTT-turned-SRT content and return deduplicated text lines.

    Auto-generated captions often repeat the same sentence across several cue
    blocks (rolling display).  We keep only *new* text seen for the first time
    to produce a clean, non-redundant transcript.
    """
    blocks = re.split(r"\n[ \t]*\n", content.strip())
    seen: set[str] = set()
    lines: list[str] = []

    for block in blocks:
        block_lines = [l.strip() for l in block.strip().splitlines()]
        texts: list[str] = []
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
    out: list[str] = []
    counter = 1
    src_lines = content.splitlines()
    i = 0

    while i < len(src_lines):
        raw = src_lines[i]
        stripped = raw.strip()

        # Skip WEBVTT header, NOTE, STYLE, REGION blocks
        if _VTT_META_RE.match(stripped):
            while i < len(src_lines) and src_lines[i].strip():
                i += 1
            i += 1
            continue

        # Cue-id line (plain integer or arbitrary string before a timestamp)
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

def format_transcript(lines: list[str]) -> str:
    """
    Join lines into a readable transcript.

    Lines starting with '>>' denote a speaker change; a blank line is inserted
    before each such line (except the very first line) so that speaker turns are
    visually separated.
    """
    out: list[str] = []
    for line in lines:
        if line.startswith(">>"):
            if out:
                out.append("")      # blank line before each speaker change
            out.append(line)
        else:
            out.append(line)
    return "\n".join(out)


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
    js_runtime: str | None,
) -> str | None:
    """
    Download, clean, and save a transcript for *url*.

    Returns the path to the saved .txt file, or None on failure.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        player_args = build_yt_dlp_player_args(js_runtime)

        cmd = [
            "yt-dlp",
            "--skip-download",
            # Prefer manual subs; fall back to auto-generated
            "--write-subs",
            "--write-auto-subs",
            "--sub-langs", "en.*,en",
            "--sub-format", "srt/vtt/best",
            "--convert-subs", "srt",
            "--output", os.path.join(tmpdir, "%(title)s.%(ext)s"),
            "--print", "%(title)s",
            *player_args,
            url,
        ]

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120,
            )
        except subprocess.TimeoutExpired:
            print(f"Error: timed out downloading transcript for {url}", file=sys.stderr)
            return None
        except FileNotFoundError:
            print(
                "Error: yt-dlp not found.  Install with:  pip install yt-dlp",
                file=sys.stderr,
            )
            return None

        if result.returncode != 0:
            print(f"yt-dlp error for {url}:\n{result.stderr}", file=sys.stderr)
            return None

        # The first printed line is the video title
        video_title = (result.stdout.strip().splitlines() or ["transcript"])[0]
        safe_title = sanitize_filename(video_title)

        # Locate the downloaded subtitle file
        raw_content: str | None = None
        is_vtt = False
        for fname in sorted(os.listdir(tmpdir)):
            if fname.endswith(".srt"):
                with open(os.path.join(tmpdir, fname), encoding="utf-8", errors="replace") as fh:
                    raw_content = fh.read()
                break
            if fname.endswith(".vtt"):
                with open(os.path.join(tmpdir, fname), encoding="utf-8", errors="replace") as fh:
                    raw_content = fh.read()
                is_vtt = True
                # Keep looking for a .srt file

        if not raw_content:
            print(f"No subtitle file found for: {url}", file=sys.stderr)
            return None

        if is_vtt:
            raw_content = convert_vtt_to_srt(raw_content)

        lines = parse_srt(raw_content)
        if not lines:
            print(f"Transcript was empty after deduplication for: {url}", file=sys.stderr)
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
        description="Download and clean video transcripts using yt-dlp.",
    )
    parser.add_argument(
        "urls",
        nargs="+",
        help="One or more video URLs.",
    )
    parser.add_argument(
        "--output-dir", "-o",
        default=".",
        help="Directory to save .txt transcript files (default: current directory).",
    )
    args = parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)

    js_runtime = detect_nodejs()
    if js_runtime:
        print(f"JS runtime detected: {js_runtime}")
    else:
        print(
            "Note: Node.js not found.  Falling back to JS-free player clients.\n"
            "      Install Node.js to improve yt-dlp success on JS-heavy sites.",
        )

    any_failed = False
    for url in args.urls:
        print(f"\nProcessing: {url}")
        result = download_transcript(url, args.output_dir, js_runtime)
        if result is None:
            any_failed = True

    sys.exit(1 if any_failed else 0)


if __name__ == "__main__":
    main()
