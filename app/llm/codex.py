"""Codex provider: uses your logged-in OpenAI/ChatGPT subscription via the `codex`
CLI in headless mode (`codex exec`). No API key, no per-call billing.

Diagrams are attached directly with `-i/--image`, so the model sees them (GPT
vision). The narration JSON shape is enforced with `--output-schema`, and the
final message is captured via `-o/--output-last-message`.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import tempfile

from app.config import DIAGRAMS_DIR, ROOT, settings
from app.llm.base import (
    MAX_VISION_IMAGES,
    SYSTEM_PROMPT,
    build_user_text,
    parse_segments,
)
from app.models import PageCapture, Segment

# OpenAI structured-output schema (strict: all props required, no extras).
_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "speak": {"type": "string"},
                    "image_idx": {"type": ["integer", "null"]},
                    "show": {"type": "string"},
                    "pause": {"type": "boolean"},
                },
                "required": ["speak", "image_idx", "show", "pause"],
            },
        }
    },
    "required": ["segments"],
}


class CodexProvider:
    def __init__(self) -> None:
        self.bin = settings.codex_bin
        self.model = settings.codex_model

    async def generate_segments(
        self, capture: PageCapture, use_images: bool = True
    ) -> list[Segment]:
        diagrams = capture.diagrams[:MAX_VISION_IMAGES] if use_images else []
        head = [SYSTEM_PROMPT, ""]
        if use_images:
            manifest = "\n".join(
                f"  [{d.idx}] (attached image {i + 1})" for i, d in enumerate(diagrams)
            )
            head += ["The diagrams are attached as images, in this order:", manifest, ""]
        prompt = "\n".join([*head, build_user_text(capture), "", "Output ONLY the JSON object."])

        with tempfile.TemporaryDirectory() as td:
            out_file = f"{td}/out.txt"
            schema_file = f"{td}/schema.json"
            with open(schema_file, "w") as f:
                json.dump(_SCHEMA, f)

            args = [
                self.bin, "exec",
                "-s", "read-only",
                "--skip-git-repo-check",
                "-o", out_file,
                "--output-schema", schema_file,
            ]
            if self.model:
                args += ["-m", self.model]
            for d in diagrams:
                args += ["-i", str(DIAGRAMS_DIR / d.png_path)]

            def run() -> subprocess.CompletedProcess:
                return subprocess.run(
                    args,
                    input=prompt,
                    capture_output=True,
                    text=True,
                    cwd=str(ROOT),
                    timeout=settings.codex_timeout,
                )

            try:
                proc = await asyncio.to_thread(run)
            except FileNotFoundError as e:
                raise RuntimeError(
                    f"Could not find the '{self.bin}' CLI. Install Codex or set CODEX_BIN."
                ) from e

            try:
                with open(out_file) as f:
                    result = f.read()
            except FileNotFoundError:
                raise RuntimeError(
                    f"codex produced no output. stderr: {(proc.stderr or '')[:400]}"
                ) from None

        if not result.strip():
            raise RuntimeError(f"codex returned empty output. stderr: {(proc.stderr or '')[:400]}")
        return parse_segments(result)
