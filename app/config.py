"""Runtime configuration, loaded from environment / .env with sensible defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Project-root-relative working dirs for captured diagrams and generated audio.
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DIAGRAMS_DIR = DATA_DIR / "diagrams"
AUDIO_DIR = DATA_DIR / "audio"
DESCRIPTIONS_DIR = DATA_DIR / "descriptions"  # cached vision descriptions of diagrams


@dataclass
class Settings:
    llm_provider: str = os.getenv("LLM_PROVIDER", "claude")
    # Split engines: a vision engine describes diagrams, a writer engine narrates.
    # Same value for both = one combined pass. vision_provider "off" = captions only.
    vision_provider: str = os.getenv("VISION_PROVIDER", "claude_code")
    writer_provider: str = os.getenv("WRITER_PROVIDER", "codex")
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    claude_model: str = os.getenv("CLAUDE_MODEL", "claude-opus-4-8")
    ollama_model: str = os.getenv("OLLAMA_MODEL", "llama3.2-vision")
    # Ollama can run on another box on the LAN; point this at its host:port.
    ollama_host: str = os.getenv("OLLAMA_HOST", "http://localhost:11434")
    # Seconds to wait on the Ollama server (it can be slow to warm up a model).
    ollama_timeout: float = float(os.getenv("OLLAMA_TIMEOUT", "600"))
    # Send diagram images to the model? "auto" detects vision support; a text-only
    # model (e.g. qwen3) then narrates from the page text + diagram captions.
    ollama_vision: str = os.getenv("OLLAMA_VISION", "auto")

    # Claude Code as the brain: uses your logged-in Claude subscription (no API key).
    claude_bin: str = os.getenv("CLAUDE_BIN", "claude")
    claude_code_model: str = os.getenv("CLAUDE_CODE_MODEL", "")  # "" = CLI default
    claude_code_timeout: float = float(os.getenv("CLAUDE_CODE_TIMEOUT", "600"))

    # Codex as the brain: uses your logged-in OpenAI/ChatGPT subscription (no API key).
    codex_bin: str = os.getenv("CODEX_BIN", "codex")
    codex_model: str = os.getenv("CODEX_MODEL", "")  # "" = CLI default
    codex_timeout: float = float(os.getenv("CODEX_TIMEOUT", "600"))

    # OpenAI-compatible API engines (Gemini, Grok/xAI, OpenRouter). Each needs a key.
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    xai_api_key: str = os.getenv("XAI_API_KEY", "")
    grok_model: str = os.getenv("GROK_MODEL", "grok-2-vision-1212")
    openrouter_api_key: str = os.getenv("OPENROUTER_API_KEY", "")
    openrouter_model: str = os.getenv("OPENROUTER_MODEL", "")  # e.g. google/gemini-2.5-flash

    tts_voice: str = os.getenv("TTS_VOICE", "af_heart")
    tts_speed: float = float(os.getenv("TTS_SPEED", "1.0"))

    cdp_url: str = os.getenv("CDP_URL", "http://localhost:9222")

    # Optional URL to refresh model pricing from when data/pricing.json goes stale
    # (>7 days). Empty = keep the bundled/local rates (no network).
    pricing_url: str = os.getenv("PRICING_URL", "")

    # For the stats card: a CLI/subscription engine (Claude Code, Codex) sends a
    # big cached agent context on every call, inflating its token counts. This is
    # the rough fraction of those input tokens a bare API call would actually send
    # (the rest is agent scaffolding), used to show a realistic "as direct API"
    # estimate alongside the actual numbers.
    api_input_factor: float = float(os.getenv("API_INPUT_FACTOR", "0.10"))

    # The debugging Chrome (must match scripts/launch-chrome.sh) — the player
    # opens in this profile, not your personal Chrome.
    chrome_app: str = os.getenv(
        "CHROME_APP", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    )
    chrome_profile_dir: str = os.getenv(
        "CHROME_PROFILE_DIR", str(ROOT / ".chrome-profile")
    )

    host: str = os.getenv("HOST", "127.0.0.1")
    port: int = int(os.getenv("PORT", "8765"))

    def ensure_dirs(self) -> None:
        DIAGRAMS_DIR.mkdir(parents=True, exist_ok=True)
        AUDIO_DIR.mkdir(parents=True, exist_ok=True)
        DESCRIPTIONS_DIR.mkdir(parents=True, exist_ok=True)


settings = Settings()
settings.ensure_dirs()


# Dirs holding generated / page-derived content (TTS audio, captured diagrams,
# cached vision descriptions). Everything here is purged on exit so no
# source-derived material lingers between sessions. (preferences.json sits in
# DATA_DIR itself and is deliberately kept; chat history lives client-side.)
_PURGE_DIRS = (DIAGRAMS_DIR, AUDIO_DIR, DESCRIPTIONS_DIR)


def _human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{int(n)} B"


def purge_generated() -> int:
    """Delete all generated/cached content (audio, diagrams, vision descriptions).
    Returns the number of files removed. Keeps the (empty) dirs + preferences.json."""
    removed = 0
    for d in _PURGE_DIRS:
        if not d.exists():
            continue
        for f in d.iterdir():
            if f.is_file():
                try:
                    f.unlink()
                    removed += 1
                except OSError:
                    pass
    return removed


def storage_usage() -> dict:
    """Bytes + file count of generated content held locally this session."""
    total = files = 0
    for d in _PURGE_DIRS:
        if not d.exists():
            continue
        for f in d.iterdir():
            if f.is_file():
                try:
                    total += f.stat().st_size
                    files += 1
                except OSError:
                    pass
    return {"bytes": total, "files": files, "human": _human_size(total)}
