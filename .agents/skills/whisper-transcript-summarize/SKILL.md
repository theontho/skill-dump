---
name: whisper-transcript-summarize
description: "Downloads video or audio streams with yt-dlp, transcribes the audio with whisper.cpp/whisper-cpp, cleans speaker-labeled transcripts, then produces translated summaries. WHEN: \"transcribe audio\", \"summarize audio\", \"summarize video with whisper\", \"whisper transcript\", \"audio transcript\", \"multilingual transcript\", \"speaker transcript\"."
license: MIT
metadata:
  author: theontho
  version: "1.0.0"
---

# Whisper Transcript Summarize

Download the actual audio stream from URLs supported by `yt-dlp`, transcribe it locally with a Whisper model through `whisper.cpp`/`whisper-cpp`, save a cleaned transcript in the original language, then create a target-language summary.

## When to Use

- User asks to summarize or transcribe a video/audio URL when subtitles are unavailable or should not be trusted
- User wants an audio-derived transcript rather than platform captions
- User asks for multilingual or speaker-aware transcription
- User asks for a summary translated into the language of their request

## Steps

### 1 — Install prerequisites if missing

Install `yt-dlp`:

```bash
pip install yt-dlp
```

Install `whisper.cpp`/`whisper-cpp` and download a multilingual model. Prefer a multilingual model for non-English or mixed-language audio:

```bash
# macOS
brew install whisper-cpp

# Or build whisper.cpp from source, then download a model such as:
# models/ggml-large-v3.bin, models/ggml-medium.bin, or models/ggml-base.bin
```

The helper detects common binary names: `whisper-cli`, `whisper-cpp`, and `main`. If the binary is elsewhere, pass `--whisper-bin`.

### 2 — Download audio and transcribe it

Run the helper script:

```bash
python .agents/skills/whisper-transcript-summarize/scripts/transcribe_audio.py <URL> [<URL> ...] --model /path/to/ggml-model.bin --output-dir transcripts/
```

The script will:

- Download and extract the real audio stream with `yt-dlp`
- Run `whisper.cpp`/`whisper-cpp` on the downloaded audio
- Use automatic language detection by default (`--language auto`) so multilingual models can handle non-English and mixed-language audio
- Request diarization/speaker-turn support when the installed Whisper binary advertises it
- Normalize detected speakers to `Speaker 1` through `Speaker 5`
- Preserve explicit gender labels if the transcription/diarization output includes them
- Save a cleaned original-language transcript as `<Title>.txt` in `--output-dir`

Useful options:

```bash
# Force a spoken language when auto-detection is wrong
python .agents/skills/whisper-transcript-summarize/scripts/transcribe_audio.py <URL> --model /path/to/model.bin --language es

# Disable diarization requests
python .agents/skills/whisper-transcript-summarize/scripts/transcribe_audio.py <URL> --model /path/to/model.bin --no-diarize

# Point at a custom whisper.cpp binary
python .agents/skills/whisper-transcript-summarize/scripts/transcribe_audio.py <URL> --model /path/to/model.bin --whisper-bin /path/to/whisper-cli
```

### 3 — Read the original-language transcript

After the script succeeds, read the generated `.txt` file(s). Treat the transcript as the source of truth for the summary.

If the transcript contains multiple languages, keep the original transcript multilingual. Do not translate the transcript file unless the user explicitly asks for a translated transcript.

### 4 — Generate the target-language summary

Using the cleaned transcript, produce a Markdown summary file beside the transcript. Name it `<Title>.summary.md`.

Choose the summary language from the user's request:

- If the user asks in a specific target language, write the summary in that language
- Otherwise, write the summary in the language used by the user invoking the skill
- Translate meaning with the LLM driving the skill; do not ask Whisper to translate unless the user explicitly requests an English-only transcription

Include:

1. **Title** – the media title, or the filename without extension if unsure
2. **Language note** – source language(s), target summary language, and any obvious multilingual sections
3. **Speaker note** – detected speaker labels, and mention if speaker labels are approximate
4. **One-sentence overview** – what the media is about in ≤ 30 words
5. **Key points** – 5–10 bullets
6. **Notable quotes** – 1–3 direct quotes, translated only if the summary target language differs from the transcript
7. **Conclusion / takeaway** – what the listener should remember or do next

## Notes

- Speaker diarization depends on the installed Whisper binary/model. If unsupported, the script still produces a clean transcript without reliable speaker IDs.
- Do not infer gender from voice unless the transcription/diarization tool explicitly provides a label or the user supplies known speaker metadata. Preserve provided labels, but call them approximate when summarizing.
- For long recordings, choose a larger multilingual model for quality and expect longer runtime.
- Multiple URLs can be processed in one call; each produces its own original-language transcript and summary.
