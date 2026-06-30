"""FastAPI app: serves the local player UI and the narrate pipeline endpoint."""

from __future__ import annotations

import asyncio
import io
import re
import subprocess
import sys
import webbrowser
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import __version__
from app.config import AUDIO_DIR, DIAGRAMS_DIR, purge_generated, settings, storage_usage

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    # On exit, purge all generated / page-derived content (audio, diagrams,
    # vision descriptions) so nothing source-derived lingers between sessions.
    # Chat history is client-side and preferences.json is kept on purpose.
    purge_generated()


app = FastAPI(title="Text-to-Instructor", version=__version__, lifespan=lifespan)

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
    step_mode: str = "auto"  # "auto" (detect step pages) | "on" (force) | "off"


class OpenStepRequest(BaseModel):
    anchor: str = ""  # empty = just bring the source tab to the front (e.g. videos)


class RevoiceRequest(BaseModel):
    voice: str


# The current lesson + voice, so audio can be rendered lazily per segment and the
# voice can change without re-capturing or re-running the brain.
_last_lesson = None
_current_voice = settings.tts_voice
_narrate_task = None  # the in-flight narrate task, so Stop can cancel it


@app.post("/api/narrate")
async def narrate(req: NarrateRequest) -> dict:
    """Capture the active Chrome tab and return the narration script (no audio yet)."""
    from app.pipeline import build_lesson  # lazy import keeps server boot cheap

    global _last_lesson, _current_voice, _narrate_task
    _narrate_task = asyncio.current_task()
    try:
        lesson = await build_lesson(
            vision=req.vision, writer=req.writer, step_mode=req.step_mode
        )
    except asyncio.CancelledError:
        from app.proc import kill_all

        kill_all()  # kill any engine subprocess still running
        raise
    except ConnectionError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    except Exception as e:  # surface a readable message to the UI
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}") from e
    finally:
        _narrate_task = None
    _last_lesson = lesson
    _current_voice = req.voice or settings.tts_voice
    return lesson.model_dump()


@app.post("/api/stop")
async def stop() -> dict:
    """Cancel an in-flight narrate and kill any running engine subprocesses."""
    from app.proc import kill_all

    killed = kill_all()
    task = _narrate_task
    if task is not None and not task.done():
        task.cancel()
    return {"stopped": True, "killed": killed}


@app.get("/api/progress")
def progress() -> dict:
    """Current build stage, polled by the UI while a lesson is being prepared."""
    from app.pipeline import get_progress

    return get_progress()


@app.get("/api/storage")
def storage() -> dict:
    """How much generated content (audio/diagrams/descriptions) is on disk now.
    Powers the session storage meter; everything here is cleared on exit."""
    return storage_usage()


@app.get("/api/debug")
def debug(text: bool = True) -> dict:
    """Troubleshooting dump of the LAST build: the page text actually used, the
    diagrams captured (idx/file/alt/description), and the full segment script
    (slide-by-slide). In-memory only, never written to disk. Pass ?text=false to
    omit the (potentially large) page text and just get the structure + script.

    Examples:
      curl -s localhost:8765/api/debug | python -m json.tool
      curl -s 'localhost:8765/api/debug?text=false'   # skip the page text
    """
    from app.pipeline import get_debug

    data = get_debug()
    if data is None:
        return {"status": "no build yet — run Teach this page first"}
    if not text:
        data = {k: v for k, v in data.items() if k != "text"}
    return data


@app.get("/api/announce")
async def announce(
    text: str = "Your lesson is ready. Press play to begin.", voice: str = ""
) -> dict:
    """Synthesize a short spoken cue (in the lesson voice) — used to say the
    lesson is ready when it finishes while the player tab isn't in focus."""
    from app import tts

    name = await tts.synthesize(text, voice=voice or _current_voice)
    return {"audio_path": name}


@app.post("/api/open_step")
async def open_step(req: OpenStepRequest) -> dict:
    """Step Mode: scroll the existing lesson Chrome tab to a step's anchor so the
    learner can correlate the narrated step with the real page."""
    from app.capture import scroll_to_anchor

    ok = await scroll_to_anchor(req.anchor)
    return {"ok": ok}


# NOTE: The "Save diagrams" export is intentionally disabled in the UI (the
# button is commented out in index.html / player.js) to avoid retaining the
# source page's copyrighted images. The implementation is preserved here so it
# can be re-enabled for an opt-in build (e.g. a client wanting training-PDF
# export). With no UI entry point, this route is simply never called.
@app.get("/api/diagrams.zip")
def diagrams_zip() -> Response:
    """Download the current page's diagrams as a zip, named by their captions."""
    if _last_lesson is None or not _last_lesson.diagrams:
        raise HTTPException(status_code=404, detail="No diagrams to save yet. Narrate a page first.")

    def clean(s: str, fallback: str) -> str:
        s = re.sub(r"[^\w \-]", "", s or "").strip()[:60].strip()
        return s or fallback

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for d in _last_lesson.diagrams:
            path = DIAGRAMS_DIR / d.png_path
            if path.exists():
                name = f"{d.idx:02d} - {clean(d.alt or d.context, f'diagram-{d.idx}')}.png"
                z.write(path, name)
    title = clean(_last_lesson.title, "diagrams")
    headers = {"Content-Disposition": f'attachment; filename="{title} - diagrams.zip"'}
    return Response(buf.getvalue(), media_type="application/zip", headers=headers)


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
    step_mode: str | None = None


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
