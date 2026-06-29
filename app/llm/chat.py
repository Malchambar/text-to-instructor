"""In-app chat: ask questions about the current page, optionally searching the web.

Uses the same engines as narration. Browser-capable engines (Claude Code, Codex)
can search the web when `web=True`; the API engines answer from the page + their
own knowledge. The current page (set by the last narrate) is provided as context.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import tempfile

from app.config import ROOT, settings

CLI_ENGINES = {"claude_code", "codex"}

SYSTEM = (
    "You are a friendly, sharp tutor helping a network engineer understand the page "
    "they're studying. Answer clearly and concisely. Use the page below as your primary "
    "reference; for things beyond it, use your own knowledge (and the web if web search "
    "is enabled). Keep answers focused and practical."
)

MAX_CONTEXT_CHARS = 120000


def _preamble(context: dict | None) -> str:
    if not context:
        return "No lesson page is loaded yet. Answer from general knowledge."
    lines = [
        "CURRENT PAGE THE STUDENT IS STUDYING:",
        f"TITLE: {context.get('title', '')}",
        f"URL: {context.get('url', '')}",
        "",
        "PAGE TEXT:",
        (context.get("text") or "")[:MAX_CONTEXT_CHARS],
    ]
    if context.get("diagrams"):
        lines += ["", "DIAGRAMS ON THE PAGE:"]
        for d in context["diagrams"]:
            lines.append(f"  [{d['idx']}] {d['desc']}")
    return "\n".join(lines)


def _transcript(messages: list[dict]) -> str:
    rows = []
    for m in messages:
        who = "Student" if m.get("role") == "user" else "Tutor"
        rows.append(f"{who}: {m.get('content', '')}")
    return "\n".join(rows)


async def chat(engine: str, messages: list[dict], web: bool, context: dict | None) -> str:
    engine = (engine or "codex").lower().replace("-", "_")
    preamble = _preamble(context)
    if engine in CLI_ENGINES:
        return await _chat_cli(engine, messages, web, preamble)
    return await _chat_completion(engine, messages, preamble)


# --- CLI engines (single-prompt; can browse) ---

async def _chat_cli(engine: str, messages: list[dict], web: bool, preamble: str) -> str:
    prompt = "\n\n".join([
        SYSTEM,
        preamble,
        "CONVERSATION SO FAR:\n" + _transcript(messages),
        "Reply to the student's latest message as the tutor. Plain text, no markdown headers.",
    ])

    if engine == "claude_code":
        args = [settings.claude_bin, "-p", "--output-format", "json"]
        if web:
            args += ["--allowedTools", "WebSearch WebFetch"]
        if settings.claude_code_model:
            args += ["--model", settings.claude_code_model]

        def run():
            return subprocess.run(
                args, input=prompt, capture_output=True, text=True,
                cwd=str(ROOT), timeout=settings.claude_code_timeout,
            )

        proc = await asyncio.to_thread(run)
        env = json.loads(proc.stdout)
        if env.get("is_error"):
            raise RuntimeError(str(env.get("result"))[:400])
        return env.get("result", "")

    # codex
    with tempfile.TemporaryDirectory() as td:
        out = f"{td}/out.txt"
        args = [settings.codex_bin, "exec", "-s", "read-only", "--skip-git-repo-check", "-o", out]
        if web:
            args += ["--search"]
        if settings.codex_model:
            args += ["-m", settings.codex_model]

        def run():
            return subprocess.run(
                args, input=prompt, capture_output=True, text=True,
                cwd=str(ROOT), timeout=settings.codex_timeout,
            )

        proc = await asyncio.to_thread(run)
        try:
            with open(out) as f:
                return f.read()
        except FileNotFoundError:
            raise RuntimeError((proc.stderr or "")[:400]) from None


# --- API engines (chat completions; no web) ---

async def _chat_completion(engine: str, messages: list[dict], preamble: str) -> str:
    system = f"{SYSTEM}\n\n{preamble}"
    convo = [{"role": m.get("role", "user"), "content": m.get("content", "")} for m in messages]

    if engine in ("gemini", "grok", "openrouter"):
        from openai import AsyncOpenAI

        from app.llm.openai_compat import resolve

        base, key, model, env = resolve(engine)
        if not key:
            raise RuntimeError(f"{env} is not set.")
        client = AsyncOpenAI(base_url=base, api_key=key)
        resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system}, *convo],
            max_tokens=2000,
        )
        return resp.choices[0].message.content or ""

    if engine == "claude":
        from anthropic import AsyncAnthropic

        if not settings.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set.")
        client = AsyncAnthropic(api_key=settings.anthropic_api_key)
        msg = await client.messages.create(
            model=settings.claude_model, max_tokens=2000, system=system, messages=convo,
        )
        return "".join(b.text for b in msg.content if b.type == "text")

    if engine == "ollama":
        from ollama import AsyncClient

        client = AsyncClient(host=settings.ollama_host, timeout=settings.ollama_timeout)
        resp = await client.chat(
            model=settings.ollama_model,
            messages=[{"role": "system", "content": system}, *convo],
        )
        return resp["message"]["content"]

    raise ValueError(f"Unknown chat engine: {engine!r}")
