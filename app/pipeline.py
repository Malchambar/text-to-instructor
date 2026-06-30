"""Orchestrates a narrate request: capture -> script -> audio -> Lesson."""

from __future__ import annotations

from app import tts
from app.capture import capture_active_tab
from app.config import DIAGRAMS_DIR, settings
from app.llm import get_provider
from app.llm.vision import describe_diagrams
from app.models import Lesson

_OFF = {"off", "none", ""}

# The most recently captured page, kept as reference context for the chat panel.
_last_context: dict | None = None

# Coarse build progress, polled by the UI so it can narrate what's happening
# ("Reading the page…", "Writing the lesson…") instead of a silent wait.
_progress: dict = {"stage": "idle", "label": ""}


def get_progress() -> dict:
    return dict(_progress)


def _set_progress(stage: str, label: str) -> None:
    _progress["stage"] = stage
    _progress["label"] = label


def last_context() -> dict | None:
    return _last_context


def _clear_diagrams() -> None:
    for f in DIAGRAMS_DIR.glob("*.png"):
        f.unlink(missing_ok=True)


def _norm(name: str) -> str:
    return name.lower().replace("-", "_")


async def build_lesson(vision: str | None = None, writer: str | None = None) -> Lesson:
    """Read the active Chrome tab and return the narration script (no audio yet).

    Engine roles (see config): a `vision` engine describes the diagrams and a
    `writer` engine narrates. Three cases:
      - vision == "off": writer narrates from Cisco's alt-text captions only.
      - vision == writer: one combined pass (the engine sees the images itself).
      - otherwise: split — describe with `vision`, then write from descriptions.

    Audio is rendered lazily per segment by `synth_segment`, so playback starts
    fast; playback speed is live in the player, so audio is rendered neutral.
    """
    _clear_diagrams()
    _set_progress("reading", "Reading the page…")

    # capture_active_tab raises a diagnostic ValueError if the page yields nothing.
    try:
        capture = await capture_active_tab(on_stage=_set_progress)
    except Exception:
        _set_progress("idle", "")
        raise

    vision = vision or settings.vision_provider
    writer = writer or settings.writer_provider or settings.llm_provider
    brain = get_provider(writer)

    if _norm(vision) in _OFF:
        _set_progress("writing", "Writing the lesson…")
        segments = await brain.generate_segments(capture, use_images=False)
    elif _norm(vision) == _norm(writer):
        _set_progress("writing", "Reading the diagrams and writing the lesson…")
        segments = await brain.generate_segments(capture, use_images=True)
    else:
        _set_progress("vision", f"Looking at {len(capture.diagrams)} diagram(s)…")
        await describe_diagrams(vision, capture.diagrams)
        _set_progress("writing", "Writing the lesson…")
        segments = await brain.generate_segments(capture, use_images=False)

    _set_progress("done", "")

    global _last_context
    _last_context = {
        "title": capture.title,
        "url": capture.url,
        "text": capture.text,
        "diagrams": [
            {"idx": d.idx, "desc": d.description or d.alt or d.context or ""}
            for d in capture.diagrams
        ],
    }

    return Lesson(
        url=capture.url,
        title=capture.title,
        segments=segments,
        diagrams=capture.diagrams,
    )


async def synth_segment(lesson: Lesson, idx: int, voice: str) -> str:
    """Render one segment's audio in `voice` and return its filename (disk-cached)."""
    seg = lesson.segments[idx]
    return await tts.synthesize(seg.speak, voice=voice)
