"""Shared data shapes passed between capture -> llm -> tts -> player."""

from __future__ import annotations

from pydantic import BaseModel


class Diagram(BaseModel):
    """One diagram/image lifted off the page, in document order."""

    idx: int
    png_path: str  # local PNG (element screenshot)
    alt: str = ""
    context: str = ""  # nearby caption/heading text, for the LLM
    description: str = ""  # richer description from the vision engine (split mode)


class PageCapture(BaseModel):
    """Everything pulled from the active Chrome tab."""

    url: str
    title: str
    text: str
    diagrams: list[Diagram] = []


class Segment(BaseModel):
    """One spoken chunk of the lecture, optionally tied to a diagram."""

    idx: int
    speak: str
    image_idx: int | None = None  # index into PageCapture.diagrams
    show: str = ""  # on-screen text for this segment (example/scenario/definition)
    pause: bool = False  # stop here (e.g. a content review question) — don't auto-advance
    audio_path: str | None = None  # filled in by TTS


class Lesson(BaseModel):
    """A fully-built narrated lesson the player consumes."""

    url: str
    title: str
    segments: list[Segment]
    diagrams: list[Diagram]
