"""User preferences persisted to a local file, so selections survive restarts."""

from __future__ import annotations

import json

from app.config import DATA_DIR, settings

PREFS_FILE = DATA_DIR / "preferences.json"
KEYS = ("voice", "speed", "vision", "writer", "auto_advance")


def _defaults() -> dict:
    return {
        "voice": settings.tts_voice,
        "speed": settings.tts_speed,
        "vision": settings.vision_provider,
        "writer": settings.writer_provider,
        "auto_advance": True,
    }


def load() -> dict:
    """Effective preferences: server defaults overlaid with the saved file."""
    prefs = _defaults()
    try:
        prefs.update(json.loads(PREFS_FILE.read_text()))
    except Exception:
        pass
    return {k: prefs.get(k) for k in KEYS}


def save(update: dict) -> dict:
    """Merge a partial update into the saved preferences and persist them."""
    prefs = load()
    for k in KEYS:
        if k in update and update[k] is not None:
            prefs[k] = update[k]
    PREFS_FILE.write_text(json.dumps(prefs, indent=2))
    return prefs
