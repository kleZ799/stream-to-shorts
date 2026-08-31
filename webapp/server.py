"""FastAPI app behind the Shorts UI.

Requests never block on the pipeline — POST /api/jobs enqueues and returns
immediately, and the browser follows progress over SSE.

Run it with:
    python -m webapp
"""
import asyncio
import json
import os
import re
import shutil
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from shorts_generator.layout_spec import ASPECT_PRESETS, LayoutSpec, parse_layout_prompt
from .jobs import STORE

STATIC_DIR = Path(__file__).parent / "static"
UPLOAD_DIR = Path("webapp_uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

VIDEO_SUFFIXES = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".ts", ".flv"}

app = FastAPI(title="Stream to Shorts")


# --- request models -------------------------------------------------------

class ResolveRequest(BaseModel):
    url: str


class LayoutPreviewRequest(BaseModel):
    prompt: str = ""
    use_llm: bool = True


class JobRequest(BaseModel):
    source: str
    prompt: str = ""
    num_clips: Optional[int] = None
    download_format: str = "best"
    language: Optional[str] = None
    use_llm: bool = True


# --- routes ---------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/api/options")
async def options() -> dict:
    """What the UI needs to render its controls."""
    return {
        "aspect_ratios": [
            {"value": k, "width": v[0], "height": v[1]} for k, v in ASPECT_PRESETS.items()
        ],
        "layouts": [
            {"value": "stacked", "label": "Webcam on top",
             "hint": "Your camera above, gameplay below. Best for streams."},
            {"value": "facetrack", "label": "Follow the face",
             "hint": "One frame, crop tracks the speaker. Best for talking heads."},
            {"value": "center", "label": "Gameplay only",
             "hint": "Plain centre crop, no webcam panel."},
        ],
        "corners": ["bottom-left", "bottom-right", "top-left", "top-right"],
    }


@app.post("/api/layout/preview")
async def layout_preview(req: LayoutPreviewRequest) -> dict:
    """Parse a layout prompt without running anything, so the UI can show
    the user what their words actually did before they commit to a render."""
    spec = await asyncio.to_thread(parse_layout_prompt, req.prompt, None, req.use_llm)
    return {"spec": spec.to_dict(), "summary": spec.describe(),
            "notes": spec.notes, "warning": spec.warning()}


@app.post("/api/resolve")
async def resolve(req: ResolveRequest) -> dict:
    """Classify a pasted link: single video, or a channel to pick from."""
    from shorts_generator.local.downloader import is_channel_or_playlist, list_channel_videos

    url = req.url.strip()
    if not url:
        raise HTTPException(400, "No URL given")

    if not is_channel_or_playlist(url):
        return {"type": "video", "source": url}

    try:
        videos = await asyncio.to_thread(list_channel_videos, url, 12)
    except Exception as e:
        raise HTTPException(400, f"Could not read that channel: {e}")
    if not videos:
        raise HTTPException(400, "No videos found at that link.")
    return {"type": "channel", "videos": videos}


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)) -> dict:
    """Accept a dropped video file and return the path to run it from."""
    name = os.path.basename(file.filename or "upload.mp4")
    if Path(name).suffix.lower() not in VIDEO_SUFFIXES:
        raise HTTPException(400, f"{Path(name).suffix or 'That file type'} isn't a video format I can read.")

    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    dest = UPLOAD_DIR / safe
    n = 1
    while dest.exists():
        dest = UPLOAD_DIR / f"{Path(safe).stem}_{n}{Path(safe).suffix}"
        n += 1

    with dest.open("wb") as out:
        shutil.copyfileobj(file.file, out)

    return {"type": "video", "source": str(dest.resolve()), "name": dest.name,
            "size": dest.stat().st_size}


@app.post("/api/jobs")
async def create_job(req: JobRequest) -> dict:
    source = req.source.strip()
    if not source:
        raise HTTPException(400, "No source given")

    spec = await asyncio.to_thread(parse_layout_prompt, req.prompt, None, req.use_llm)
    if req.num_clips:
        spec.num_clips = req.num_clips
        spec.validate()

    job = STORE.create(source, spec, download_format=req.download_format,
                       language=req.language)
    return job.snapshot()


@app.get("/api/jobs")
async def list_jobs() -> dict:
    return {"jobs": STORE.list()}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str) -> dict:
    job = STORE.get(job_id)
    if not job:
        raise HTTPException(404, "No such job")
    return job.snapshot()


@app.get("/api/jobs/{job_id}/stream")
async def stream_job(job_id: str) -> StreamingResponse:
    """Server-sent events: one message per state change, then close."""
    job = STORE.get(job_id)
    if not job:
        raise HTTPException(404, "No such job")

    async def gen():
        last = -1
        idle = 0
        while True:
            snap = job.snapshot()
            if snap["version"] != last:
                last = snap["version"]
                idle = 0
                yield f"data: {json.dumps(snap)}\n\n"
            else:
                idle += 1
                if idle % 30 == 0:      # keep-alive comment every ~15s
                    yield ": ping\n\n"
            if snap["status"] in ("done", "error"):
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


@app.get("/api/jobs/{job_id}/clips/{filename}")
async def get_clip(job_id: str, filename: str) -> FileResponse:
    job = STORE.get(job_id)
    if not job:
        raise HTTPException(404, "No such job")

    # Never let a filename escape the job's own directory.
    safe = os.path.basename(filename)
    path = (Path(job.out_dir) / safe).resolve()
    if not str(path).startswith(str(Path(job.out_dir).resolve())) or not path.exists():
        raise HTTPException(404, "No such clip")

    return FileResponse(path, media_type="video/mp4", filename=safe)


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
