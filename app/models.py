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
    is_video: bool = False  # this "image" is a video poster — offer "watch on the page"
    anchor: str = ""  # same-page fragment to bring the source tab to (e.g. "#video1")


class Step(BaseModel):
    """One step of a step-by-step instruction page (iFixit, WikiHow, ...).

    Present only for pages that capture as an ordered procedure; image_idxs point
    into PageCapture.diagrams, anchor is a same-page fragment to scroll to."""

    number: str = ""  # e.g. "Step 1" or "1" (as shown on the page)
    title: str = ""
    text: str = ""
    image_idxs: list[int] = []  # indices into PageCapture.diagrams
    anchor: str = ""  # e.g. "#s385022" — scroll the page to this step


class PageCapture(BaseModel):
    """Everything pulled from the active Chrome tab."""

    url: str
    title: str
    text: str
    diagrams: list[Diagram] = []
    steps: list[Step] = []  # non-empty only for recognized step-by-step pages


class Segment(BaseModel):
    """One spoken chunk of the lecture, optionally tied to a diagram."""

    idx: int
    speak: str
    image_idx: int | None = None  # index into PageCapture.diagrams (single-image / legacy)
    image_idxs: list[int] = []  # step-mode: a group of diagrams shown as a slideshow
    show: str = ""  # on-screen text for this segment (example/scenario/definition)
    pause: bool = False  # stop here (e.g. a content review question) — don't auto-advance
    step_idx: int | None = None  # step-mode: which PageCapture.steps / Lesson.steps entry
    source_anchor: str | None = None  # step-mode: same-page fragment to "open this step"
    audio_path: str | None = None  # filled in by TTS


class LessonStats(BaseModel):
    """Session statistics shown on the final slide: the engines used, the tokens
    they consumed, and an ESTIMATED API-equivalent cost (the CLI/subscription
    engines bill nothing per token — cost_note explains)."""

    vision_provider: str = ""
    writer_provider: str = ""
    vision_model: str = ""
    writer_model: str = ""
    # per-pass breakdown (vision = reading diagrams, writer = writing narration)
    vision_input_tokens: int = 0
    vision_output_tokens: int = 0
    vision_cost_usd: float | None = None
    writer_input_tokens: int = 0
    writer_output_tokens: int = 0
    writer_cost_usd: float | None = None
    # totals
    input_tokens: int = 0
    output_tokens: int = 0
    tokens_estimated: bool = False  # token counts are approximate (no engine report)
    estimated_cost_usd: float | None = None
    # "as a direct API call" estimate: CLI engines inflate input with cached agent
    # context; this strips that to a realistic bare-API figure (see api_input_factor)
    api_input_tokens: int = 0
    api_cost_usd: float | None = None
    api_input_factor: float = 0.0
    billing: str = ""  # "subscription" | "api" | "local" | "mixed"
    cost_note: str = ""  # human sentence, e.g. "no charge accrued (subscription)"
    built_at: str = ""  # ISO timestamp


class Lesson(BaseModel):
    """A fully-built narrated lesson the player consumes."""

    url: str
    title: str
    segments: list[Segment]
    diagrams: list[Diagram]
    steps: list[Step] = []  # step-mode: ordered steps (number/title) for labels/links
    stats: LessonStats | None = None  # session stats for the final slide
