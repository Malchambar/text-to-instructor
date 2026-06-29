"""FastAPI app: serves the local player UI and the narrate pipeline endpoint."""

from __future__ import annotations

import subprocess
import sys
import webbrowser
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import __version__
from app.config import AUDIO_DIR, DIAGRAMS_DIR, settings

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Text-to-Instructor", version=__version__)

# Player assets, plus captured diagrams and generated audio, served straight off disk.
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/diagrams", StaticFiles(directory=DIAGRAMS_DIR), name="diagrams")
app.mount("/audio", StaticFiles(directory=AUDIO_DIR), name="audio")


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "version": __version__}


@app.get("/api/settings")
def get_settings() -> dict:
    """Non-secret settings the UI shows (never returns the API key)."""
    return {
        "vision_provider": settings.vision_provider,
        "writer_provider": settings.writer_provider,
        "ollama_model": settings.ollama_model,
        "ollama_host": settings.ollama_host,
        "tts_voice": settings.tts_voice,
        "tts_speed": settings.tts_speed,
        "cdp_url": settings.cdp_url,
    }


class NarrateRequest(BaseModel):
    voice: str | None = None
    vision: str | None = None
    writer: str | None = None


class RevoiceRequest(BaseModel):
    voice: str


# The current lesson + voice, so audio can be rendered lazily per segment and the
# voice can change without re-capturing or re-running the brain.
_last_lesson = None
_current_voice = settings.tts_voice


@app.post("/api/narrate")
async def narrate(req: NarrateRequest) -> dict:
    """Capture the active Chrome tab and return the narration script (no audio yet)."""
    from app.pipeline import build_lesson  # lazy import keeps server boot cheap

    global _last_lesson, _current_voice
    try:
        lesson = await build_lesson(vision=req.vision, writer=req.writer)
    except ConnectionError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    except Exception as e:  # surface a readable message to the UI
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}") from e
    _last_lesson = lesson
    _current_voice = req.voice or settings.tts_voice
    return lesson.model_dump()


@app.get("/api/segment_audio/{idx}")
async def segment_audio(idx: int) -> dict:
    """Render (and cache) one segment's audio in the current voice; return its path."""
    from app.pipeline import synth_segment

    if _last_lesson is None or not (0 <= idx < len(_last_lesson.segments)):
        raise HTTPException(status_code=404, detail="No such segment in the current lesson.")
    try:
        name = await synth_segment(_last_lesson, idx, _current_voice)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}") from e
    return {"idx": idx, "audio_path": name}


@app.post("/api/revoice")
async def revoice(req: RevoiceRequest) -> dict:
    """Switch the current lesson's voice; segments re-render on demand as you play."""
    global _current_voice
    if _last_lesson is None:
        raise HTTPException(status_code=400, detail="No lesson to re-voice yet. Narrate a page first.")
    _current_voice = req.voice
    return {"ok": True, "voice": _current_voice}


@app.get("/api/preferences")
def get_preferences() -> dict:
    from app.prefs import load

    return load()


class PrefsRequest(BaseModel):
    voice: str | None = None
    speed: float | None = None
    vision: str | None = None
    writer: str | None = None
    auto_advance: bool | None = None


@app.post("/api/preferences")
def save_preferences(req: PrefsRequest) -> dict:
    from app.prefs import save

    return save(req.model_dump(exclude_none=True))


class ChatRequest(BaseModel):
    messages: list[dict]
    engine: str | None = None
    web: bool = False


@app.post("/api/chat")
async def chat_endpoint(req: ChatRequest) -> dict:
    """Answer a question about the current page (optionally searching the web)."""
    from app.llm.chat import chat
    from app.pipeline import last_context

    engine = req.engine or settings.writer_provider or "codex"
    try:
        reply = await chat(engine, req.messages, req.web, last_context())
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}") from e
    return {"reply": reply}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


def _open_player(url: str) -> None:
    """Open the player as a tab in the debugging Chrome profile.

    Targets the same --user-data-dir launch-chrome.sh uses, so the player lands
    in the already-running instructing window — not the default browser (Safari)
    and not your personal Chrome profile.
    """
    if sys.platform == "darwin" and Path(settings.chrome_app).exists():
        try:
            subprocess.Popen(
                [settings.chrome_app, f"--user-data-dir={settings.chrome_profile_dir}", url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,  # detach so it can't block the server
            )
            return
        except Exception:
            pass
    webbrowser.open(url)  # last resort: default browser


def run() -> None:
    """Console entry point (`t2i`): start the server and open the player."""
    import uvicorn

    url = f"http://{settings.host}:{settings.port}/"
    _open_player(url)
    print(f"Text-to-Instructor running at {url}")
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    run()
