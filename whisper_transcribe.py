#!/usr/bin/env python3
"""Standalone helper: transcribes an audio file with word-level timestamps
using faster-whisper. Runs inside .venv_whisper (an isolated venv, kept
separate from dashboard.py's stdlib-only process) and is invoked via
subprocess -- the same pattern already used for the ffmpeg binary.

Usage: python3 whisper_transcribe.py <audio_path> [language|auto]
Prints one JSON object to stdout:
  {"text": str, "language": str, "language_probability": float,
   "words": [{"word": str, "start": float, "end": float}, ...]}
"""
import sys, os, json

# The model is already downloaded once (~/.cache/huggingface/hub/models--Systran--
# faster-whisper-small, ~464MB). Without this, huggingface_hub still tries to reach
# the Hub on every single call to check for updates -- and a slow/unreliable network
# path there (observed: 60s timeout, twice in a row, on an otherwise-instant local
# model load) silently stalls the render's "timing" stage for a minute+ instead of the
# few seconds a cached-model transcription actually takes. Offline mode makes the
# cache authoritative and skips that network round-trip entirely.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "usage: whisper_transcribe.py <audio_path> [language]"}))
        sys.exit(1)
    audio_path = sys.argv[1]
    language = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] != "auto" else None

    from faster_whisper import WhisperModel

    model = WhisperModel("small", device="cpu", compute_type="int8")
    segments, info = model.transcribe(audio_path, word_timestamps=True, language=language)

    words = []
    text_parts = []
    for seg in segments:
        text_parts.append(seg.text.strip())
        if seg.words:
            for w in seg.words:
                words.append({"word": w.word.strip(), "start": round(w.start, 3), "end": round(w.end, 3)})

    print(json.dumps({
        "text": " ".join(text_parts),
        "language": info.language,
        "language_probability": round(info.language_probability, 3),
        "words": words,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
