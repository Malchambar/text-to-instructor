"""Local neural text-to-speech with Kokoro (ONNX).

Runs entirely on-device. Audio is content-addressed (hash of voice+speed+text)
and written to data/audio, so re-narrating a page replays instantly instead of
re-synthesizing.
"""

from __future__ import annotations

import asyncio
import hashlib
import re

import numpy as np
import soundfile as sf

from app.config import AUDIO_DIR, ROOT, settings

# Bumped when synthesis behavior changes, so stale cached audio isn't reused.
_TTS_VERSION = "2"
# Kokoro can babble/repeat on long input, so synthesize in chunks this size.
_CHUNK_CHARS = 320

MODELS_DIR = ROOT / "models"
MODEL_PATH = MODELS_DIR / "kokoro-v1.0.onnx"
VOICES_PATH = MODELS_DIR / "voices-v1.0.bin"

_MODEL_HELP = (
    "Kokoro model files are missing. Download them once and place in models/:\n"
    "  kokoro-v1.0.onnx  and  voices-v1.0.bin\n"
    "from https://github.com/thewh1teagle/kokoro-onnx/releases"
)

_kokoro = None  # lazily loaded singleton (model load is a few seconds)
_synth_lock = asyncio.Lock()  # Kokoro isn't thread-safe; serialize synthesis


def _load():
    global _kokoro
    if _kokoro is None:
        if not MODEL_PATH.exists() or not VOICES_PATH.exists():
            raise RuntimeError(_MODEL_HELP)
        from kokoro_onnx import Kokoro

        _kokoro = Kokoro(str(MODEL_PATH), str(VOICES_PATH))
    return _kokoro


def _audio_name(text: str, voice: str, speed: float) -> str:
    key = f"{_TTS_VERSION}|{voice}|{speed}|{text}".encode()
    return f"seg-{hashlib.sha1(key).hexdigest()[:16]}.wav"


def _clean(text: str) -> str:
    """Make text speakable: turn arrows/symbols into words, drop odd characters.
    Acronyms are spaced here (audio only) so the voice spells them out — DNS ->
    'D N S' — while the on-screen text the model wrote stays clean."""
    text = text.replace("->", " to ").replace("→", " to ").replace("—", ", ")
    text = re.sub(r"[*_`#>|]", " ", text)  # markdown leftovers
    text = re.sub(r"(?<=\w)/(?=\w)", " ", text)  # CI/CD -> CI CD (not "slash"), TCP/IP, and/or
    # Space out short all-caps acronyms (2-4 letters: DNS, IP, TCP, EDR, URL) so
    # they're read letter-by-letter. Longer all-caps (e.g. TALOS) read as words.
    text = re.sub(r"\b([A-Z]{2,4})\b", lambda m: " ".join(m.group(1)), text)
    return re.sub(r"\s+", " ", text).strip()


def _chunk(text: str, max_chars: int = _CHUNK_CHARS) -> list[str]:
    """Split into sentence-sized pieces so Kokoro never gets a long passage."""
    pieces = re.split(r"(?<=[.!?;:])\s+", text)
    chunks: list[str] = []
    cur = ""
    for p in pieces:
        p = p.strip()
        if not p:
            continue
        if len(p) > max_chars:  # very long sentence: break on commas
            for sub in re.split(r"(?<=,)\s+", p):
                sub = sub.strip()
                if not sub:
                    continue
                if len(cur) + len(sub) + 1 <= max_chars:
                    cur = f"{cur} {sub}".strip()
                else:
                    if cur:
                        chunks.append(cur)
                    cur = sub
        elif len(cur) + len(p) + 1 <= max_chars:
            cur = f"{cur} {p}".strip()
        else:
            if cur:
                chunks.append(cur)
            cur = p
    if cur:
        chunks.append(cur)
    return chunks or [text]


async def synthesize(text: str, voice: str | None = None, speed: float | None = None) -> str:
    """Render `text` to a WAV in data/audio; return its filename. Cached by content."""
    voice = voice or settings.tts_voice
    speed = float(speed or settings.tts_speed)
    name = _audio_name(text, voice, speed)
    out = AUDIO_DIR / name
    if out.exists():
        return name

    def work() -> None:
        kokoro = _load()
        chunks = _chunk(_clean(text))
        parts: list[np.ndarray] = []
        sample_rate = 24000
        for c in chunks:
            samples, sample_rate = kokoro.create(c, voice=voice, speed=speed, lang="en-us")
            parts.append(np.asarray(samples))
            parts.append(np.zeros(int(sample_rate * 0.12), dtype=parts[-1].dtype))  # gap
        full = np.concatenate(parts) if parts else np.zeros(1, dtype=np.float32)
        sf.write(str(out), full, sample_rate)

    # Serialize: concurrent prefetches must not run Kokoro at the same time.
    async with _synth_lock:
        if out.exists():  # another waiter may have just produced it
            return name
        await asyncio.to_thread(work)
    return name
