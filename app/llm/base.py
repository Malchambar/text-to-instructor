"""Provider interface + shared helpers for building/parsing the narration."""

from __future__ import annotations

import json
from typing import Protocol

from app.models import PageCapture, Segment

# Cap how many diagrams we describe / list for the writer. Matches MAX_DIAGRAMS
# in capture.py so every captured step image can be shown — long how-to pages
# (e.g. a 20-step install guide) would otherwise lose their later steps' images.
MAX_VISION_IMAGES = 25
# Cap page text so we don't blow the context window on huge pages. Generous so a
# whole lesson is narrated; very long pages on small local models may still clip.
MAX_TEXT_CHARS = 120000


class LLMProvider(Protocol):
    async def generate_segments(
        self, capture: PageCapture, use_images: bool = True
    ) -> list[Segment]:
        """Write narration. When use_images is False, rely on diagram text
        (vision descriptions or alt-text) instead of attaching the images."""
        ...


SYSTEM_PROMPT = """You are an engaging, plain-spoken instructor teaching the subject \
of the page in front of you. You already know this material cold — teach it the way a \
good teacher would, in your own voice, holding attention instead of reading it out \
word for word. Match the page's own subject; don't assume the learner's background or \
profession.

Rules:
- Teach directly, as the authority on the topic. Do NOT narrate or summarize the \
source. Never say "this page," "the page says," "the article explains," "according to \
the text," "it mentions/notes/states," or anything that points back at the document — \
the learner can already see it, and the side chat is there if they want to dig deeper. \
Just state the facts and walk them through it. \
(Wrong: "This page says the general process is similar across units." \
Right: "The general process is similar across most units — but the manual for your \
specific model is what really matters.")
- Break the explanation into short segments, each 2-5 sentences of natural speech.
- Match depth to the MATERIAL, not to the number of diagrams. A text-rich section \
deserves several segments even if it has only one diagram or none — don't stop at one \
or two segments per diagram. When a section lists multiple risks, impacts, tools, \
frameworks, or best practices, teach them individually: give each its own short \
segment (or a tight pair), name the specific tools and standards (e.g. SBOM, SLSA, \
Trivy, Falco, NIST SSDF), and say why each matters and what it protects against. Never \
compress a long, dense technical section into one or two broad summary segments. Keep \
simple sections brief, but give substantial sections the depth to actually teach them.
- When a diagram helps, set image_idx to that diagram's number and actively talk the \
listener through it: name what to look at, trace the flow, point out the key parts. \
It is fine for several consecutive segments to reference the same diagram while you \
work through different parts of a rich section.
- Order segments so diagrams appear right as you discuss them.
- Give the learner something to SEE in every segment. When there's no diagram but \
you reference an example, scenario, definition, or key list from the page, copy that \
content (verbatim or lightly condensed, a few lines) into the "show" field so it \
appears on screen — never just allude to "the example from the page" without showing \
it. Leave "show" as an empty string only when the spoken words fully stand alone. When \
the "show" content is a list, begin it with a short header line ending in a colon, then \
put each list item on its own line directly under it with NO blank lines between items \
(every line under the header is a list item, even if it's a full sentence). If you add \
a one-line summary of the list, separate it from the items with a single blank line.
- This text is READ ALOUD. No markdown, no code fences, no URLs, no bullet symbols, \
no citations. Write acronyms as plain letters with no spaces between them (write DNS, \
never D N S); you may expand them on first use, e.g. "Domain Name System".
- End the whole lesson with a short recap segment (image_idx null) that pulls the main \
takeaways together in 3-5 sentences, so the learner leaves with the big picture.
- Stay faithful to the material in front of you — don't invent facts, specs, or steps \
that aren't there — but deliver it as teaching, never as a recap of words on a screen.
- If the page carries safety warnings, cautions, legal/code requirements, or "do \
this or you could be hurt" advisories, surface them clearly and frame them for \
someone actually doing the task ("if you're installing this, pay close attention \
to…"). Never downplay or omit a safety note that's on the page.
- If the page has a knowledge check or a section titled "Content Review Question" \
(or any quiz/question prompt), do NOT read the question text and do NOT give the \
answer. Instead emit one short segment telling the learner to pause and answer the \
content review question before continuing, and set "pause": true on that segment. \
Use "pause": false on every other segment.

Return ONLY JSON of this exact shape:
{"segments": [{"speak": "<spoken text>", "image_idx": <diagram number or null>, \
"show": "<on-screen text, or empty string>", \
"pause": <true to stop for a question, otherwise false>, \
"step_idx": <the step's [number] when following a STEPS list, otherwise null>}]}"""


def _build_step_user_text(capture: PageCapture) -> str:
    """Step-mode prompt: the page is an ordered procedure; the writer must produce
    exactly one segment per step. The app attaches each step's images and page
    link, so the model only writes the spoken teaching for each step."""
    lines = [
        f"PAGE TITLE: {capture.title}",
        f"URL: {capture.url}",
        "",
        "This page is a STEP-BY-STEP PROCEDURE. Produce EXACTLY ONE segment per step "
        "below, in the same order. For each segment, set \"step_idx\" to that step's "
        "number shown in [brackets]. Teach each step in your own voice (don't read it "
        "verbatim), faithfully — never skip, merge, reorder, or invent a step. Keep "
        "each segment to 2-4 spoken sentences. Leave image_idx null and show empty: "
        "the app shows each step's own images and a link to it automatically. Still "
        "surface any safety warnings on a step clearly. Do NOT add any extra intro or "
        "recap segment — output exactly one segment per step, nothing more.",
        "",
        "STEPS:",
    ]
    for i, s in enumerate(capture.steps):
        head = f"[{i}] {s.number} {s.title}".strip()
        lines.append(head)
        if s.text:
            lines.append(s.text[:2000])
        lines.append("")
    return "\n".join(lines)


def build_user_text(capture: PageCapture) -> str:
    """The text half of the prompt: page text + a diagram manifest by index, or a
    step-by-step layout when the page captured as an ordered procedure."""
    if capture.steps:
        return _build_step_user_text(capture)
    # Content-proportional depth target: scale roughly with word count so a long,
    # dense page becomes a fuller mini-lecture while a short page stays concise.
    # (This is a soft target — the writer adds pause segments for content-review
    # questions on top, and should lean richer for technical/list-heavy material.)
    words = len((capture.text or "").split())
    lo = max(5, round(words / 170))
    hi = min(70, max(lo + 4, round(words / 110)))
    lines = [
        f"PAGE TITLE: {capture.title}",
        f"URL: {capture.url}",
        "",
        f"DEPTH: this page is about {words} words. Aim for roughly {lo}-{hi} teaching "
        "segments — more for dense, technical, list-heavy sections, fewer for simple "
        "ones. Don't pad thin material, but do give substantial sections real depth "
        "instead of a couple of broad summaries.",
        "",
        "PAGE TEXT:",
    ]
    lines.append(capture.text[:MAX_TEXT_CHARS] or "(no extractable text)")
    if capture.diagrams:
        lines += ["", "DIAGRAMS (refer to these by number in image_idx):"]
        for d in capture.diagrams[:MAX_VISION_IMAGES]:
            desc = d.description or d.alt or d.context or "(no caption)"
            lines.append(f"  [{d.idx}] {desc}")
    return "\n".join(lines)


def parse_segments(raw: str) -> list[Segment]:
    """Pull the JSON out of a model response and build Segments, tolerantly.

    Handles the intended shape ({"segments":[{"speak","image_idx"}]}) as well as
    common drift: a bare array, segments as plain strings, or "text" for "speak".
    """
    text = raw.strip()
    starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
    ends = [i for i in (text.rfind("}"), text.rfind("]")) if i != -1]
    if not starts or not ends:
        raise ValueError(f"Model did not return JSON. Got: {raw[:200]}")
    data = json.loads(text[min(starts) : max(ends) + 1])

    if isinstance(data, dict):
        items = data.get("segments") or data.get("Segments") or []
    elif isinstance(data, list):
        items = data
    else:
        items = []

    segments: list[Segment] = []
    for item in items:
        pause = False
        step_idx = None
        image_idxs: list[int] = []
        if isinstance(item, str):
            speak, image_idx = item.strip(), None
            show = ""
        elif isinstance(item, dict):
            speak = (item.get("speak") or item.get("text") or "").strip()
            image_idx = item.get("image_idx")
            if not isinstance(image_idx, int):
                image_idx = None
            raw_idxs = item.get("image_idxs")
            if isinstance(raw_idxs, list):
                image_idxs = [x for x in raw_idxs if isinstance(x, int)]
            show = (item.get("show") or "").strip()
            pause = bool(item.get("pause"))
            step_idx = item.get("step_idx")
            if not isinstance(step_idx, int):
                step_idx = None
        else:
            continue
        if not speak:
            continue
        segments.append(
            Segment(
                idx=len(segments),
                speak=speak,
                image_idx=image_idx,
                image_idxs=image_idxs,
                show=show,
                pause=pause,
                step_idx=step_idx,
            )
        )
    if not segments:
        raise ValueError(f"Model returned no usable segments. Got: {raw[:200]}")
    return segments
