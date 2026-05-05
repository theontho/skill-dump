---
name: transcript-summarize
description: "Downloads video transcripts via subtitles using yt-dlp, deduplicates and cleans them into plain-text files, then produces an AI summary of each video. WHEN: \"summarize video\", \"video summary\", \"transcript summary\", \"summarize YouTube\", \"get transcript\", \"video transcript\", \"watch summary\"."
license: MIT
metadata:
  author: theontho
  version: "1.0.0"
---

# Transcript Summarize

Download, clean, and summarise videos from any URL supported by yt-dlp (YouTube, Vimeo, Twitter/X, etc.).

## When to Use

- User asks to summarise a video
- User shares a video URL and wants a written overview
- User wants a readable transcript without watching the video

## Steps

### 1 — Download and clean the transcript

Run the helper script, passing the video URL(s):

```bash
python .agents/skills/transcript-summarize/scripts/get_transcript.py <URL> [<URL> ...] --output-dir transcripts/ --sub-langs en.*,en
```

The script will:
- Download subtitles (manual first, auto-generated as fallback) via `yt-dlp`
- Convert SRT/VTT to plain text, removing timestamps and HTML tags
- Deduplicate rolling-caption repeats
- Replace leading `>` / `>>` speaker-change indicators with `>[SPEAKER CHANGE]>` and insert blank lines before speaker changes
- Save each result as `<VideoTitle>.txt` in `--output-dir`

**Subtitle language rule:** Default to English subtitles (`--sub-langs en.*,en`). If the user explicitly asks for another language, or the user's request is written in another language, use that language's yt-dlp subtitle code instead (for example `--sub-langs es.*,es` for Spanish or `--sub-langs fr.*,fr` for French).

**Install yt-dlp if missing:**
```bash
pip install yt-dlp
```

**Node.js and JS runtime:** If [Node.js](https://nodejs.org) is installed, the script detects it automatically and configures yt-dlp to use it for JavaScript deobfuscation, which increases success on sites with dynamic player challenges (e.g. newer YouTube builds).  Without Node.js, the script falls back to JS-free player clients (`ios`, `mweb`).  Install Node.js to maximise reliability:
```bash
# macOS
brew install node
# Ubuntu/Debian
sudo apt install nodejs
```

### 2 — Read the transcript

After the script succeeds, read the generated `.txt` file(s) from the output directory.

### 3 — Generate a summary

Using the cleaned transcript text, produce a well-structured summary that includes:

1. **Title** – the video title (use the filename without extension if unsure)
2. **One-sentence overview** – what the video is about in ≤ 30 words
3. **Key points** – bullet list of the main ideas or arguments (5–10 items)
4. **Notable quotes** – 1–3 direct quotes that capture the speaker's voice (if present)
5. **Conclusion / takeaway** – what the viewer should remember or do next

Keep the tone neutral and factual. If the transcript is too short or garbled to summarise meaningfully, report that and suggest re-running with a different subtitle language (`--sub-langs`).

## Notes

- Subtitles must exist for the video. Fully audio-only content (no CC) will return an empty transcript.
- Use English subtitles unless the user specifies another language or asks in another language; then use the language of the request.
- Multiple URLs can be processed in one call; each produces its own `.txt` file and its own summary.
