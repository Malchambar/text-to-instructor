"""Model token pricing for the session-stats panel.

Pricing is stored locally in data/pricing.json (preserved across purge, like
preferences.json). It's seeded from the bundled defaults below and, when it goes
stale (>7 days), refreshed on the next run from PRICING_URL if that's set —
otherwise the existing/bundled rates are kept. All figures are an ESTIMATE of
API-equivalent cost: the default engines (Claude Code, Codex) run on your
subscription via the CLI, so no per-token charge is actually billed.
"""

from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timezone

from app.config import DATA_DIR, settings

PRICING_FILE = DATA_DIR / "pricing.json"
_STALE_DAYS = 7

# USD per 1,000,000 tokens. Rough public list prices — edit pricing.json to taste.
_BUNDLED: dict = {
    "currency": "USD",
    "per_million": True,
    "rates": {
        "claude-opus": {"input": 15.0, "output": 75.0},
        "claude-sonnet": {"input": 3.0, "output": 15.0},
        "claude-haiku": {"input": 1.0, "output": 5.0},
        "gpt-5": {"input": 1.25, "output": 10.0},
        "gemini-flash": {"input": 0.30, "output": 2.50},
        "gemini-pro": {"input": 1.25, "output": 10.0},
        "grok": {"input": 2.0, "output": 10.0},
        "default": {"input": 1.0, "output": 5.0},
    },
}

_cache: dict | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_stale(data: dict) -> bool:
    ts = data.get("fetched_at")
    if not ts:
        return True
    try:
        when = datetime.fromisoformat(ts)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
    except Exception:
        return True
    return (datetime.now(timezone.utc) - when).days >= _STALE_DAYS


def _write(data: dict) -> None:
    try:
        PRICING_FILE.write_text(json.dumps(data, indent=2))
    except OSError:
        pass


def _fetch_remote() -> dict | None:
    """Best-effort weekly refresh from a maintained pricing URL (PRICING_URL).
    Returns a validated dict with a `rates` table, or None on any problem."""
    url = (settings.pricing_url or "").strip()
    if not url:
        return None
    try:
        with urllib.request.urlopen(url, timeout=6) as r:
            data = json.load(r)
        if isinstance(data, dict) and isinstance(data.get("rates"), dict):
            return data
    except Exception:
        return None
    return None


def load_pricing() -> dict:
    """Return the pricing dict, seeding/refreshing data/pricing.json as needed."""
    global _cache
    if _cache is not None:
        return _cache

    data: dict
    if PRICING_FILE.exists():
        try:
            data = json.loads(PRICING_FILE.read_text())
        except Exception:
            data = dict(_BUNDLED)
    else:
        data = dict(_BUNDLED)
        data["fetched_at"] = _now_iso()
        _write(data)

    if _is_stale(data):
        fresh = _fetch_remote()
        if fresh is not None:
            fresh["fetched_at"] = _now_iso()
            data = fresh
            _write(data)
        else:
            # couldn't refresh — keep current rates but bump the check stamp so we
            # don't retry the network on every single run this week
            data.setdefault("rates", _BUNDLED["rates"])
            data["fetched_at"] = _now_iso()
            _write(data)

    _cache = data
    return data


def _rate_key(model: str, provider: str) -> str:
    m = (model or "").lower()
    if "opus" in m:
        return "claude-opus"
    if "sonnet" in m:
        return "claude-sonnet"
    if "haiku" in m:
        return "claude-haiku"
    if "gemini" in m:
        return "gemini-pro" if "pro" in m else "gemini-flash"
    if "grok" in m:
        return "grok"
    if any(x in m for x in ("gpt", "codex", "o4", "o3")):
        return "gpt-5"
    # fall back to the engine when the model id isn't telling
    p = (provider or "").lower()
    return {
        "claude_code": "claude-opus",
        "claude": "claude-opus",
        "codex": "gpt-5",
        "gemini": "gemini-flash",
        "grok": "grok",
    }.get(p, "default")


def estimate_cost(provider: str, model: str, tok_in: int, tok_out: int) -> float | None:
    """Estimated API-equivalent cost (USD) for tokens. None if no usable tokens."""
    if not (tok_in or tok_out):
        return None
    rates = load_pricing().get("rates", _BUNDLED["rates"])
    r = rates.get(_rate_key(model, provider)) or rates.get("default") or {"input": 1.0, "output": 5.0}
    cost = (tok_in / 1_000_000) * r["input"] + (tok_out / 1_000_000) * r["output"]
    return round(cost, 6)


def engine_billing(provider: str) -> str:
    """How an engine is paid for: 'subscription' (CLI, no per-token charge),
    'api' (billed to the user's API key), or 'local' (free)."""
    p = (provider or "").lower()
    if p in ("claude_code", "codex"):
        return "subscription"
    if p in ("ollama", "off", ""):
        return "local"
    return "api"


# Fraction of a subscription-CLI engine's INPUT tokens that a bare API call would
# actually send — the rest is cached agent scaffolding you aren't billed for.
# Claude Code carries a huge tool/system prompt (little is real content); Codex's
# input is mostly the real page text (only ~20% scaffolding).
_API_INPUT_FACTOR = {"claude_code": 0.10, "codex": 0.80}


def api_input_factor(provider: str) -> float:
    """Per-engine 'real content' fraction for the "≈ if API" estimate. API/local
    engines are already bare API calls, so they're unadjusted (1.0)."""
    if engine_billing(provider) != "subscription":
        return 1.0
    return _API_INPUT_FACTOR.get((provider or "").lower(), settings.api_input_factor)
