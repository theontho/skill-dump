#!/bin/bash

# Convenience script to run the transcript extraction using uv
# Usage: ./get_transcript.sh <URL> [additional args]

SKILL_DIR="/Users/mac/.gemini/skills/transcript-summarize"
SCRIPT_PATH="$SKILL_DIR/scripts/get_transcript.py"

if [ -z "$1" ]; then
    echo "Usage: $0 <URL> [additional args]"
    exit 1
fi

uv run --project "$SKILL_DIR" "$SCRIPT_PATH" "$@"
