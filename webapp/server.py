"""FastAPI app behind the Shorts UI.

Requests never block on the pipeline — POST /api/jobs enqueues and returns
immediately, and the browser follows progress over SSE.

Run it with:
    python -m webapp
"""
import asyncio
import contextlib
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from shorts_generator.layout_spec import ASPECT_PRESETS, LayoutSpec, parse_layout_prompt
from .jobs import STORE, regenerate_seo

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


class SettingsRequest(BaseModel):
    provider: str = "gemini"
    api_key: str = ""
    model: Optional[str] = None
    # Store the key but keep the current provider — this is how an OpenAI key
    # gets saved as the thing that takes over when Gemini's day runs out,
    # rather than as a switch away from Gemini.
    as_fallback: bool = False


@app.get("/api/settings")
async def get_settings() -> dict:
    """What the UI needs to decide whether to show first-run setup.

    Never returns the key itself — only whether one is present, and where
    it came from, so the user knows which knob actually controls it.
    """
    from shorts_generator import user_config
    from shorts_generator.config import current_model, current_provider

    provider = current_provider()
    from_env = bool(os.getenv("GEMINI_API_KEY") or os.getenv("OPENAI_API_KEY"))
    return {
        "has_key": user_config.has_llm_key(),
        "provider": provider,
        "model": current_model(provider),
        "source": "environment" if from_env else "settings file",
        "config_path": str(user_config.config_path()),
        "ffmpeg": shutil.which("ffmpeg") is not None,
    }


@app.get("/api/usage")
async def get_usage() -> dict:
    """Today's API spend, so the daily cap is visible before it bites."""
    from shorts_generator import usage, user_config
    from shorts_generator.config import current_model, current_provider

    snap = usage.snapshot()
    provider = current_provider()
    snap["provider"] = provider
    snap["model"] = current_model(provider)
    # Whether a spent Gemini day would actually be survivable.
    snap["fallback_ready"] = bool(user_config.get("OPENAI_API_KEY"))
    return snap


@app.post("/api/settings")
async def set_settings(req: SettingsRequest) -> dict:
    from shorts_generator import user_config

    provider = (req.provider or "gemini").strip().lower()
    if provider not in ("gemini", "openai"):
        raise HTTPException(400, "Provider must be 'gemini' or 'openai'.")

    key = (req.api_key or "").strip()
    if not key:
        raise HTTPException(400, "Paste an API key first.")

    values = {} if req.as_fallback else {"LLM_PROVIDER": provider}
    values["GEMINI_API_KEY" if provider == "gemini" else "OPENAI_API_KEY"] = key
    if req.model:
        values["GEMINI_MODEL" if provider == "gemini" else "OPENAI_MODEL"] = req.model.strip()

    path = user_config.save(values)
    return {"saved": True, "config_path": str(path),
            "has_key": user_config.has_llm_key()}


class LocationRequest(BaseModel):
    path: str


class RevealRequest(BaseModel):
    path: str


@app.get("/api/locations")
async def get_locations() -> dict:
    from shorts_generator import user_config

    return {
        "root": str(user_config.output_root()),
        "source": str(user_config.source_dir()),
        "shorts": str(user_config.shorts_dir()),
    }


@app.post("/api/locations")
async def set_location(req: LocationRequest) -> dict:
    """Move where future videos are saved. Existing files stay put."""
    from shorts_generator import user_config

    path = (req.path or "").strip().strip('"')
    if not path:
        raise HTTPException(400, "Enter a folder path.")
    try:
        root = await asyncio.to_thread(user_config.set_output_root, path)
    except ValueError as e:
        raise HTTPException(400, str(e))

    return {
        "saved": True,
        "root": str(root),
        "source": str(user_config.source_dir()),
        "shorts": str(user_config.shorts_dir()),
    }


# Downloaded sources are the only big thing the app leaves behind: a two-hour
# stream is several GB, and it is dead weight once the clips are rendered.
_CLEANABLE_MEDIA = {".mp4", ".mkv", ".webm", ".mov", ".m4a", ".wav", ".aac"}
_CLEANABLE_PARTIAL = {".part", ".ytdl", ".temp"}


def _cleanup_scan() -> list:
    """Reclaimable files in the source folder.

    Deliberately narrow: the downloaded videos and half-finished downloads,
    nothing else. Rendered clips live in a different folder and are the whole
    point of the app. Transcripts stay too — a .srt is a few hundred KB and
    saves re-transcribing hours of audio on the next run.
    """
    from shorts_generator import user_config

    src = user_config.source_dir().resolve()
    items = []
    for f in sorted(src.iterdir()):
        if not f.is_file():
            continue
        name = f.name.lower()
        if f.suffix.lower() in _CLEANABLE_PARTIAL or name.endswith(".mp4.part"):
            kind = "partial"
        elif f.suffix.lower() in _CLEANABLE_MEDIA:
            kind = "source"
        else:
            continue
        try:
            size = f.stat().st_size
        except OSError:
            continue
        items.append({"name": f.name, "path": str(f), "bytes": size, "kind": kind})
    return items


@app.get("/api/cleanup")
async def cleanup_scan() -> dict:
    """What a cleanup would remove, so the UI can show it before asking."""
    from shorts_generator import user_config

    items = await asyncio.to_thread(_cleanup_scan)
    return {
        "items": items,
        "count": len(items),
        "bytes": sum(i["bytes"] for i in items),
        "folder": str(user_config.source_dir()),
    }


@app.post("/api/cleanup")
async def cleanup_run() -> dict:
    """Delete those files. Refuses while a job is in flight, since the source
    it is reading from is exactly what would be removed."""
    busy = [j for j in STORE.list() if j["status"] in ("queued", "running")]
    if busy:
        raise HTTPException(409, "A job is still running — wait for it to finish, then clear space.")

    items = await asyncio.to_thread(_cleanup_scan)
    freed = 0
    removed, failed = [], []
    for it in items:
        try:
            Path(it["path"]).unlink()
        except OSError as e:
            failed.append(f"{it['name']}: {e.strerror or e}")
            continue
        freed += it["bytes"]
        removed.append(it["name"])
    return {"freed": freed, "removed": removed, "failed": failed, "count": len(removed)}


def _open_in_file_manager(target: Path) -> None:
    """Show `target` in the OS file manager.

    A file gets *selected* in its folder rather than opened — the point is to
    land on it, not to launch it in a video player.
    """
    import subprocess
    import sys as _sys

    is_file = target.is_file()
    if os.name == "nt":
        if is_file:
            # explorer's /select, takes one glued argument and rejects the
            # quoting subprocess applies to a list, so hand it a command line.
            # A Windows filename can never contain a quote, so this can't be
            # broken out of — but check anyway, since it is a shell-ish call.
            if '"' in str(target):
                raise ValueError("That filename can't be shown safely.")
            subprocess.run(f'explorer /select,"{target}"')   # noqa: S603 - local desktop app
        else:
            os.startfile(str(target))       # noqa: S606 - local desktop app
    elif _sys.platform == "darwin":
        subprocess.run(["open", "-R", str(target)] if is_file else ["open", str(target)],
                       check=True)
    else:
        # No portable "select this file" on Linux; the containing folder is
        # the next best thing.
        subprocess.run(["xdg-open", str(target if not is_file else target.parent)],
                       check=True)


@app.post("/api/reveal")
async def reveal(req: RevealRequest) -> dict:
    """Open a folder in the OS file manager.

    Only paths inside the configured save location are allowed — this runs a
    local command, so it must never be steerable to an arbitrary path.
    """
    from shorts_generator import user_config

    target = Path(req.path or "").expanduser()
    if not target.exists():
        raise HTTPException(404, "That folder doesn't exist yet.")
    if target.is_file():
        target = target.parent

    root = user_config.output_root().resolve()
    try:
        target.resolve().relative_to(root)
    except ValueError:
        raise HTTPException(403, "That folder is outside your save location.")

    try:
        await asyncio.to_thread(_open_in_file_manager, target)
    except Exception as e:
        raise HTTPException(500, f"Could not open the folder: {e}")
    return {"opened": str(target)}


class ShowRequest(BaseModel):
    file: Optional[str] = None


@app.post("/api/jobs/{job_id}/reveal")
async def reveal_clip(job_id: str, req: ShowRequest) -> dict:
    """Show one clip (or its run's folder) in the file manager.

    Addressed by job and filename rather than by a path from the browser: the
    server already knows where a job's clips live, so nothing the page sends
    can point this at somewhere else on the disk. It also means a run made
    before the save location moved still opens where its files actually are.
    """
    job = _job_or_404(job_id)

    if req.file:
        safe = os.path.basename(req.file)
        if STORE.clip(job, safe) is None:
            raise HTTPException(404, "No such clip")
        target = _clip_path(job, safe)
        if not target.exists():
            raise HTTPException(404, "That clip's file is missing — it may have been moved.")
    else:
        target = Path(job.out_dir)
        if not target.exists():
            raise HTTPException(404, "That run's folder is gone.")

    try:
        await asyncio.to_thread(_open_in_file_manager, target)
    except Exception as e:
        raise HTTPException(500, f"Could not show that file: {e}")
    return {"opened": str(target), "folder": str(target.parent if req.file else target)}


@app.post("/api/open-upload")
async def open_upload() -> dict:
    """Open YouTube's upload page in the user's real browser.

    The URL is hardcoded on purpose — this opens an external page, so it must
    not be steerable by anything the page sends.
    """
    import webbrowser

    url = "https://www.youtube.com/upload"
    ok = await asyncio.to_thread(webbrowser.open, url)
    return {"opened": ok, "url": url}


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


@app.get("/api/library")
async def library() -> dict:
    """Every clip this app has ever made that is still on disk.

    Rescans the folder first, so clips rendered by an earlier session — or by
    a copy of the app that was closed and reopened — come back rather than
    disappearing with the process that made them.
    """
    await asyncio.to_thread(STORE.restore)
    runs = []
    for snap in STORE.list():
        clips = [c for c in snap["clips"] if c.get("url")]
        if not clips:
            continue
        runs.append({
            "id": snap["id"],
            "created_at": snap["created_at"],
            "source": snap["source"],
            "source_title": snap["source_title"],
            "shorts_dir": snap["shorts_dir"],
            "restored": snap["restored"],
            "clips": clips,
        })
    return {"runs": runs, "count": sum(len(r["clips"]) for r in runs)}


@app.post("/api/jobs/{job_id}/seo")
async def write_seo(job_id: str, force: bool = False) -> dict:
    """Write (or rewrite) the upload metadata for a job's clips.

    Runs on demand rather than at listing time: it costs an LLM call, and for
    clips old enough to have no transcript on record it costs a short
    transcription too.
    """
    job = _job_or_404(job_id)
    if not job.clips:
        raise HTTPException(409, "This run has no clips to write metadata for.")
    try:
        written = await asyncio.to_thread(regenerate_seo, STORE, job, force)
    except Exception as e:
        raise HTTPException(500, f"Could not write the metadata: {e}")
    return {"written": written, "clips": job.clips}


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


# --- clip editing ---------------------------------------------------------
#
# Everything below works on clips a finished job already produced, so the user
# can fix a cut without re-running the whole pipeline.

MAX_CLIP_SECONDS = 300


class TrimRequest(BaseModel):
    start: float
    end: float
    mute: bool = False


class SaveClipRequest(BaseModel):
    name: Optional[str] = None


def _job_or_404(job_id: str):
    job = STORE.get(job_id)
    if not job:
        raise HTTPException(404, "No such job")
    return job


def _clip_path(job, filename: str) -> Path:
    """Resolve a clip filename inside its job directory, and nowhere else."""
    safe = os.path.basename(filename)
    root = Path(job.out_dir).resolve()
    path = (root / safe).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        raise HTTPException(403, "That file is outside the job folder.")
    return path


def _strip_audio(path: Path) -> None:
    """Rewrite a clip without its audio track, in place."""
    import subprocess

    tmp = path.with_name(path.stem + "_muted" + path.suffix)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(path),
         "-c", "copy", "-an", "-movflags", "+faststart", str(tmp)],
        check=True,
    )
    os.replace(tmp, path)


@app.post("/api/jobs/{job_id}/clips/{filename}/trim")
async def trim_clip(job_id: str, filename: str, req: TrimRequest) -> dict:
    """Re-cut one clip at new timestamps, straight from the downloaded source.

    Re-rendering (rather than trimming the rendered file) means the span can
    grow as well as shrink, and the layout stays exactly what the user asked
    for the first time round.
    """
    from shorts_generator.render import render_highlights

    job = _job_or_404(job_id)
    clip = STORE.clip(job, os.path.basename(filename))
    if clip is None:
        raise HTTPException(404, "No such clip")
    if not job.source_path or not os.path.exists(job.source_path):
        raise HTTPException(409, "The source video for this job is gone — re-run it to edit.")

    start, end = float(req.start), float(req.end)
    if start < 0:
        raise HTTPException(400, "Start can't be before the beginning of the video.")
    if end - start < 1.0:
        raise HTTPException(400, "A clip needs to be at least a second long.")
    if end - start > MAX_CLIP_SECONDS:
        raise HTTPException(400, f"Keep clips under {MAX_CLIP_SECONDS // 60} minutes.")

    old_path = _clip_path(job, clip["file"])
    highlight = {
        "title": clip.get("title") or "Clip",
        "start_time": start,
        "end_time": end,
        "score": clip.get("score"),
        "hook_sentence": clip.get("hook_sentence") or "",
        "virality_reason": clip.get("virality_reason") or "",
    }
    prefix = f"edit_{clip.get('index', 1):02d}_{int(time.time())}"

    def _render():
        out = render_highlights(job.source_path, [highlight], job.spec,
                                out_dir=job.out_dir, name_prefix=prefix)
        return out[0] if out else {}

    try:
        result = await asyncio.to_thread(_render)
    except Exception as e:
        raise HTTPException(500, f"Could not re-cut that clip: {e}")

    new_path = result.get("clip_url")
    if not new_path or not os.path.exists(new_path):
        raise HTTPException(500, result.get("error") or "The re-cut produced no file.")

    if req.mute:
        try:
            await asyncio.to_thread(_strip_audio, Path(new_path))
        except Exception as e:
            raise HTTPException(500, f"Could not mute that clip: {e}")

    new_name = os.path.basename(new_path)
    updated = STORE.replace_clip(job, clip["file"], {
        "file": new_name,
        "url": f"/api/jobs/{job.id}/clips/{new_name}",
        "start_time": start,
        "end_time": end,
        "duration": round(end - start, 1),
        "muted": bool(req.mute),
        "edited": True,
    })

    # The old render is dead weight once the new one is in the list.
    if old_path.name != new_name:
        with contextlib.suppress(OSError):
            old_path.unlink()

    return updated or {}


@app.delete("/api/jobs/{job_id}/clips/{filename}")
async def delete_clip(job_id: str, filename: str) -> dict:
    """Throw a clip away — file and all."""
    job = _job_or_404(job_id)
    safe = os.path.basename(filename)
    if STORE.clip(job, safe) is None:
        raise HTTPException(404, "No such clip")

    path = _clip_path(job, safe)
    with contextlib.suppress(OSError):
        path.unlink()
    STORE.remove_clip(job, safe)
    return {"deleted": safe, "remaining": len(job.clips)}


@app.post("/api/jobs/{job_id}/clips/{filename}/save")
async def save_clip(job_id: str, filename: str, req: SaveClipRequest) -> dict:
    """Copy a clip out of the job folder into the user's save location.

    Job folders are working space; this is how a clip the user actually wants
    ends up somewhere they'll find it later.
    """
    from shorts_generator import user_config

    job = _job_or_404(job_id)
    safe = os.path.basename(filename)
    if STORE.clip(job, safe) is None:
        raise HTTPException(404, "No such clip")

    src = _clip_path(job, safe)
    if not src.exists():
        raise HTTPException(404, "That clip's file is missing.")

    stem = re.sub(r"[^A-Za-z0-9 ._-]", "", (req.name or Path(safe).stem)).strip() or "short"
    dest_dir = user_config.shorts_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{stem}.mp4"
    n = 1
    while dest.exists():
        dest = dest_dir / f"{stem}_{n}.mp4"
        n += 1

    await asyncio.to_thread(shutil.copy2, src, dest)
    STORE.replace_clip(job, safe, {"saved_to": str(dest)})
    return {"saved": True, "path": str(dest), "folder": str(dest_dir)}



app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
