"""Local neural text-to-speech with Kokoro (ONNX).

Runs entirely on-device. Audio is content-addressed (hash of voice+speed+text)
and written to data/audio, so re-narrating a page replays instantly instead of
re-synthesizing.
"""

from __future__ import annotations

import asyncio
import hashlib

import soundfile as sf

from app.config import AUDIO_DIR, ROOT, settings

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
    key = f"{voice}|{speed}|{text}".encode()
    return f"seg-{hashlib.sha1(key).hexdigest()[:16]}.wav"


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
        samples, sample_rate = kokoro.create(text, voice=voice, speed=speed, lang="en-us")
        sf.write(str(out), samples, sample_rate)

    # Serialize: concurrent prefetches must not run Kokoro at the same time.
    async with _synth_lock:
        if out.exists():  # another waiter may have just produced it
            return name
        await asyncio.to_thread(work)
    return name
