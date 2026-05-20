# Transcript Summarize

Download, clean, and summarize video transcripts from any URL supported by yt-dlp.

## Installation

This skill uses [uv](https://github.com/astral-sh/uv) for dependency management.

```bash
# Install uv if you haven't
curl -LsSf https://astral-sh.uv/install.sh | sh

# The dependencies are managed automatically when running via uv run
```

## Usage

```bash
uv run --project /Users/mac/.gemini/skills/transcript-summarize /Users/mac/.gemini/skills/transcript-summarize/scripts/get_transcript.py <URL>
```
