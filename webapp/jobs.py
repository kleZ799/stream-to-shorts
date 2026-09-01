"""Background job runner for the web UI.

The pipeline takes minutes, not milliseconds — transcription alone can run
longer than any sensible HTTP timeout. So a request only ever *enqueues* work;
progress is streamed separately over SSE.

Jobs run one at a time on a single worker thread. That's deliberate: Whisper
and ffmpeg are both CPU-bound, and running two at once makes both slower than
running them in sequence.

Finished jobs are written to disk as a `job.json` beside their clips, and read
back at startup. Without that, the store was purely in-memory: close the app
and every clip you had ever made became unreachable through the UI even though
the mp4s were still sitting in the folder.
"""
import contextlib
import io
import json
import os
import queue
import re
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from shorts_generator.layout_spec import LayoutSpec

from shorts_generator import user_config

# Written into each job's folder so the run can be reconstructed later.
MANIFEST_NAME = "job.json"
CLIP_SUFFIXES = (".mp4", ".mkv", ".webm", ".mov")


def _title_from_filename(name: str) -> str:
    """A readable title for a clip we only know by its filename.

    "short_03.mp4" is what an older run left behind; "Clip 3" is what a person
    can actually pick out of a grid.
    """
    stem = os.path.splitext(name)[0]
    m = re.fullmatch(r"(?:short|edit)_(\d+)(?:_\d+)?", stem)
    if m:
        return f"Clip {int(m.group(1))}"
    return re.sub(r"[_-]+", " ", stem).strip().title() or stem


def _fmt_clock(seconds: float) -> str:
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

# Rough share of total runtime per stage, used to drive the progress bar.
# Transcription dominates on CPU, which is why it owns the widest band.
_STAGE_BANDS = {
    "queued":     (0.0, 0.0),
    "download":   (0.0, 0.15),
    "transcribe": (0.15, 0.55),
    "rank":       (0.55, 0.70),
    "render":     (0.70, 1.0),
    "done":       (1.0, 1.0),
}

_STAGE_LABELS = {
    "queued": "Queued",
    "download": "Fetching the video",
    "transcribe": "Transcribing audio",
    "rank": "Finding the best moments",
    "render": "Rendering clips",
    "done": "Done",
}

# Map a log line's prefix to the stage it belongs to.
_PREFIX_STAGE = [
    (r"^\[download", "download"),
    (r"^\[transcribe", "transcribe"),
    (r"^\[highlights|^\[llm|^\[rank", "rank"),
    (r"^\[stack|^\[clip/local|^\[center|^\[render", "render"),
]


@dataclass
class Job:
    id: str
    source: str
    spec: LayoutSpec
    download_format: str = "best"
    language: Optional[str] = None
    status: str = "queued"          # queued | running | done | error
    stage: str = "queued"
    progress: float = 0.0
    message: str = "Queued"
    error: Optional[str] = None
    source_path: Optional[str] = None
    clips: List[Dict] = field(default_factory=list)
    highlights: List[Dict] = field(default_factory=list)
    video_meta: Dict = field(default_factory=dict)
    log: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    # Where this job's clips actually live. Set for jobs read back from disk,
    # so moving the save location can't strand the ones already rendered.
    folder: Optional[str] = None
    restored: bool = False
    _version: int = 0

    @property
    def out_dir(self) -> str:
        if self.folder:
            return self.folder
        # Resolved live, so changing the save location takes effect immediately.
        return str(user_config.shorts_dir() / self.id)

    @property
    def source_title(self) -> str:
        return str(self.video_meta.get("title") or "")

    def snapshot(self) -> Dict:
        return {
            "id": self.id,
            "status": self.status,
            "stage": self.stage,
            "stage_label": _STAGE_LABELS.get(self.stage, self.stage),
            "progress": round(self.progress, 4),
            "message": self.message,
            "error": self.error,
            "spec": self.spec.to_dict(),
            "spec_summary": self.spec.describe(),
            "clips": self.clips,
            "source": self.source,
            "source_path": self.source_path,
            "source_title": self.source_title,
            "shorts_dir": self.out_dir,
            "created_at": self.created_at,
            "restored": self.restored,
            "log": self.log[-60:],
            "version": self._version,
        }

    def manifest(self) -> Dict:
        """Everything needed to rebuild this job in a later session."""
        return {
            "id": self.id,
            "created_at": self.created_at,
            "source": self.source,
            "source_path": self.source_path,
            "video_meta": self.video_meta,
            "spec": self.spec.to_dict(),
            "clips": self.clips,
        }


class JobStore:
    """Thread-safe registry. Single worker, FIFO queue."""

    def __init__(self) -> None:
        self._jobs: Dict[str, Job] = {}
        self._lock = threading.Lock()
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._worker = threading.Thread(target=self._run_forever, daemon=True)
        self._worker.start()
        self.restore()

    # --- public API -------------------------------------------------------

    def create(self, source: str, spec: LayoutSpec, download_format: str = "best",
               language: Optional[str] = None) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], source=source, spec=spec,
                  download_format=download_format, language=language)
        with self._lock:
            self._jobs[job.id] = job
        os.makedirs(job.out_dir, exist_ok=True)
        self._queue.put(job.id)
        depth = self._queue.qsize()
        if depth > 1:
            self._update(job, message=f"Queued — {depth - 1} job(s) ahead")
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> List[Dict]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
        # A finished run with nothing left in it is just an empty folder — the
        # user deleted every clip. Don't show it as a row with no contents.
        return [j.snapshot() for j in jobs
                if j.clips or j.status in ("queued", "running", "error")]

    def clip(self, job: Job, filename: str) -> Optional[Dict]:
        with self._lock:
            for c in job.clips:
                if c.get("file") == filename:
                    return dict(c)
        return None

    def replace_clip(self, job: Job, filename: str, updates: Dict) -> Optional[Dict]:
        """Swap in an edited clip's details, keeping its place in the list."""
        found = None
        with self._lock:
            for c in job.clips:
                if c.get("file") == filename:
                    c.update(updates)
                    job._version += 1
                    found = dict(c)
                    break
        if found:
            self._persist(job)
        return found

    def remove_clip(self, job: Job, filename: str) -> bool:
        with self._lock:
            keep = [c for c in job.clips if c.get("file") != filename]
            if len(keep) == len(job.clips):
                return False
            job.clips = keep
            job._version += 1
        self._persist(job)
        return True

    def set_seo(self, job: Job, filename: str, seo: Dict) -> Optional[Dict]:
        """Attach freshly written upload metadata to one clip."""
        return self.replace_clip(job, filename, {"seo": seo})

    # --- persistence ------------------------------------------------------

    def _persist(self, job: Job) -> None:
        """Write the job's manifest beside its clips. Never raises.

        Losing a manifest costs the library a row; letting the write throw
        would cost the user a render they already waited for.
        """
        try:
            folder = Path(job.out_dir)
            folder.mkdir(parents=True, exist_ok=True)
            with self._lock:
                data = job.manifest()
            tmp = folder / (MANIFEST_NAME + ".tmp")
            tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
            os.replace(tmp, folder / MANIFEST_NAME)
        except Exception as e:
            print(f"[jobs] could not save the manifest for {job.id}: {e}", flush=True)

    def restore(self) -> int:
        """Adopt every run already sitting in the clips folder.

        Two shapes turn up there. A folder with a manifest rebuilds completely —
        titles, scores, spans, SEO. A folder holding nothing but mp4s (anything
        rendered before manifests existed) is adopted from the filenames alone,
        so those clips are at least playable, savable and deletable again
        instead of being invisible to the app that made them.
        """
        try:
            root = user_config.shorts_dir()
        except Exception as e:
            print(f"[jobs] could not read the clips folder: {e}", flush=True)
            return 0

        found = 0
        try:
            folders = sorted((d for d in root.iterdir() if d.is_dir()),
                             key=lambda d: d.stat().st_mtime, reverse=True)
        except OSError as e:
            print(f"[jobs] could not list {root}: {e}", flush=True)
            return 0

        for folder in folders:
            with self._lock:
                if folder.name in self._jobs:
                    continue
            try:
                job = self._job_from_folder(folder)
            except Exception as e:
                print(f"[jobs] skipping {folder.name}: {e}", flush=True)
                continue
            if job is None:
                continue
            with self._lock:
                self._jobs[job.id] = job
            found += 1

        if found:
            print(f"[jobs] restored {found} earlier run(s) from {root}", flush=True)
        return found

    def _job_from_folder(self, folder: Path) -> Optional[Job]:
        manifest: Dict = {}
        path = folder / MANIFEST_NAME
        if path.exists():
            try:
                manifest = json.loads(path.read_text(encoding="utf-8-sig"))
            except Exception as e:
                print(f"[jobs] {folder.name}: unreadable manifest ({e}) — "
                      f"reading the folder instead", flush=True)

        job_id = str(manifest.get("id") or folder.name)
        on_disk = {p.name for p in folder.iterdir()
                   if p.is_file() and p.suffix.lower() in CLIP_SUFFIXES}

        clips: List[Dict] = []
        for c in manifest.get("clips") or []:
            if not isinstance(c, dict):
                continue
            name = c.get("file")
            if not name or name not in on_disk:
                continue        # deleted from the folder by hand since the run
            on_disk.discard(name)
            clips.append({**c, "job_id": job_id,
                          "url": f"/api/jobs/{job_id}/clips/{name}"})

        # Anything left is a file the manifest didn't know about — an older
        # run, or a clip dropped in by hand. Take it at face value.
        for i, name in enumerate(sorted(on_disk), len(clips) + 1):
            clips.append({
                "index": i,
                "rank": i,
                "title": _title_from_filename(name),
                "score": None,
                "start_time": None,
                "end_time": None,
                "duration": None,
                "file": name,
                "url": f"/api/jobs/{job_id}/clips/{name}",
                "job_id": job_id,
                "seo": None,
            })

        if not clips:
            return None

        try:
            created = float(manifest.get("created_at") or folder.stat().st_mtime)
        except (TypeError, ValueError, OSError):
            created = time.time()

        source_path = manifest.get("source_path")
        job = Job(
            id=job_id,
            source=str(manifest.get("source") or ""),
            spec=LayoutSpec.from_dict(manifest.get("spec")),
            status="done",
            stage="done",
            progress=1.0,
            message=f"{len(clips)} clip(s) ready",
            source_path=source_path if source_path and os.path.exists(source_path) else None,
            clips=clips,
            video_meta=manifest.get("video_meta") or {},
            created_at=created,
            folder=str(folder),
            restored=True,
        )
        return job

    # --- internals --------------------------------------------------------

    def _update(self, job: Job, *, stage: Optional[str] = None,
                frac: Optional[float] = None, message: Optional[str] = None) -> None:
        """Advance a job's reported state. `frac` is progress *within* the stage."""
        with self._lock:
            if stage:
                job.stage = stage
            if message:
                job.message = message
            lo, hi = _STAGE_BANDS.get(job.stage, (0.0, 1.0))
            if frac is None:
                job.progress = max(job.progress, lo)
            else:
                job.progress = max(job.progress, lo + (hi - lo) * max(0.0, min(1.0, frac)))
            job._version += 1

    def _log(self, job: Job, line: str) -> None:
        line = line.rstrip()
        if not line:
            return
        with self._lock:
            job.log.append(line)
            job._version += 1

        for pattern, stage in _PREFIX_STAGE:
            if re.match(pattern, line):
                # "[stack] 2/5: ..." gives exact render progress.
                m = re.search(r"\b(\d+)\s*/\s*(\d+)\b", line)
                frac = None
                if m and stage == "render":
                    done, total = int(m.group(1)), int(m.group(2))
                    frac = (done - 1) / max(1, total)
                self._update(job, stage=stage, frac=frac,
                             message=_STAGE_LABELS.get(stage, stage))
                break

    def _run_forever(self) -> None:
        while True:
            job_id = self._queue.get()
            job = self.get(job_id)
            if job is None:
                continue
            try:
                self._execute(job)
            except Exception as e:
                with self._lock:
                    job.status = "error"
                    job.error = str(e)
                    job.message = f"Failed: {e}"
                    job.log.append(traceback.format_exc())
                    job._version += 1
            finally:
                self._queue.task_done()

    def _execute(self, job: Job) -> None:
        from shorts_generator.highlights import get_highlights
        from shorts_generator.local.downloader import (
            download_youtube_local, fetch_video_meta,
        )
        from shorts_generator.local.llm import call_local_llm, reset_fallback
        from shorts_generator.local.transcriber import transcribe_local
        from shorts_generator.render import render_highlights
        from shorts_generator.seo import attach_seo

        # A previous run may have fallen back to OpenAI. Start this one on the
        # provider the user actually chose — its quota may well have reset.
        reset_fallback()

        with self._lock:
            job.status = "running"
            job._version += 1
        self._update(job, stage="download", message=_STAGE_LABELS["download"])

        sink = _JobStdout(self, job)
        with contextlib.redirect_stdout(sink):
            source_path = download_youtube_local(
                job.source, fmt=job.download_format,
                out_dir=str(user_config.source_dir()),
            )
            with self._lock:
                job.source_path = source_path
                job._version += 1

            # What the video is *about*, straight from its own listing. The
            # transcript says what was said; this says who said it and where,
            # which is what keeps the titles and tags factual.
            meta = fetch_video_meta(job.source)
            if meta:
                with self._lock:
                    job.video_meta = meta
                    job._version += 1

            # Explicit spans: the user already told us what to cut, so there is
            # nothing to transcribe and nothing to rank. Straight to rendering.
            if job.spec.time_ranges:
                print(f"[render] {len(job.spec.time_ranges)} exact cut(s) requested — "
                      "skipping transcription and ranking", flush=True)
                top = [
                    {
                        "title": f"Clip {i} · {_fmt_clock(a)}–{_fmt_clock(b)}",
                        "start_time": a,
                        "end_time": b,
                        "score": None,
                        "hook_sentence": "",
                        "virality_reason": "Exact span you asked for",
                    }
                    for i, (a, b) in enumerate(job.spec.time_ranges, 1)
                ]
                all_highlights = list(top)
                attach_seo(top, transcript=None, video_meta=job.video_meta,
                           source=job.source, llm_fn=call_local_llm)
                self._update(job, stage="render", frac=0.0,
                             message=_STAGE_LABELS["render"])
                shorts = render_highlights(source_path, top, job.spec, out_dir=job.out_dir)
                self._finalize(job, shorts, all_highlights)
                return

            self._update(job, stage="transcribe", message=_STAGE_LABELS["transcribe"])
            transcript = transcribe_local(source_path, language=job.language)
            if not transcript["segments"]:
                raise RuntimeError(
                    "No speech found in this video — nothing to build clips from."
                )

            self._update(job, stage="rank", message=_STAGE_LABELS["rank"])
            # Beside the video, next to its .srt, so resuming works the same
            # way transcript reuse already does: point at the same source and
            # the work you already paid for is still there.
            checkpoint = Path(source_path).with_suffix(".highlights.json")
            result = get_highlights(transcript, num_clips=job.spec.num_clips,
                                    llm_fn=call_local_llm,
                                    checkpoint_path=checkpoint,
                                    clip_seconds=job.spec.clip_seconds,
                                    brief=job.spec.brief)
            all_highlights = result.get("highlights", [])
            if not all_highlights:
                raise RuntimeError("The ranker found no usable moments in this video.")

            # Best first, and it stays that way all the way to the grid: the
            # order the user sees is the order worth posting in.
            top = sorted(all_highlights, key=lambda h: int(h.get("score", 0)),
                         reverse=True)[:job.spec.num_clips]
            print(f"[rank] {len(top)} clip(s) chosen, best first: "
                  + ", ".join(str(h.get("score", "?")) for h in top), flush=True)

            self._update(job, frac=0.6, message="Writing titles, tags and hooks")
            attach_seo(top, transcript=transcript, video_meta=job.video_meta,
                       source=job.source, llm_fn=call_local_llm)

            self._update(job, stage="render", frac=0.0, message=_STAGE_LABELS["render"])
            shorts = render_highlights(source_path, top, job.spec, out_dir=job.out_dir)

        self._finalize(job, shorts, all_highlights)

    def _finalize(self, job: Job, shorts: List[Dict], all_highlights: List[Dict]) -> None:
        rendered = []
        for i, s in enumerate(shorts, 1):
            path = s.get("clip_url")
            rendered.append({
                "index": i,
                # `shorts` arrives best-first, so position is the ranking.
                "rank": i,
                "title": s.get("title"),
                "score": s.get("score"),
                "start_time": s.get("start_time"),
                "end_time": s.get("end_time"),
                "duration": round(float(s.get("end_time", 0)) - float(s.get("start_time", 0)), 1),
                "hook_sentence": s.get("hook_sentence"),
                "virality_reason": s.get("virality_reason"),
                "seo": s.get("seo"),
                "error": s.get("error"),
                "job_id": job.id,
                "file": os.path.basename(path) if path else None,
                "url": f"/api/jobs/{job.id}/clips/{os.path.basename(path)}" if path else None,
            })

        ok = [c for c in rendered if c["url"]]
        with self._lock:
            job.clips = rendered
            job.highlights = all_highlights
            job.status = "done" if ok else "error"
            job.stage = "done"
            job.progress = 1.0
            job.message = (f"{len(ok)} clip(s) ready" if ok
                           else "Every clip failed to render — see the log")
            if not ok:
                job.error = "no clips rendered"
            job._version += 1

        # Record the run next to its clips, so it is still here next launch.
        if ok:
            self._persist(job)


def _clip_words(job: Job, clip: Dict) -> str:
    """What is actually said inside one clip, for the SEO writer to work from.

    Cheapest source first. A run that transcribed its source left an .srt
    behind, and slicing the clip's span out of it costs nothing. Only when
    there is no cache — an old folder whose source is long deleted — does this
    fall back to transcribing the clip file itself, which is quick because a
    Short is under a minute.
    """
    from shorts_generator.local.transcriber import _load_srt_cache, transcribe_local

    start, end = clip.get("start_time"), clip.get("end_time")
    if job.source_path and start is not None and end is not None:
        stem = Path(job.source_path).stem
        for candidate in (
            Path(job.source_path).with_suffix(".srt"),
            user_config.source_dir() / f"{stem}.srt",
        ):
            if not candidate.exists():
                continue
            try:
                cached = _load_srt_cache(candidate)
            except Exception:
                continue
            words = " ".join(
                str(s.get("text", "")).strip() for s in cached.get("segments", [])
                if float(s.get("end", 0)) >= float(start) and float(s.get("start", 0)) <= float(end)
            ).strip()
            if words:
                return words[:1600]

    path = Path(job.out_dir) / str(clip.get("file") or "")
    if not path.exists():
        return ""
    try:
        result = transcribe_local(str(path))
    except Exception as e:
        print(f"[seo] could not transcribe {path.name} ({e})", flush=True)
        return ""
    return " ".join(str(s.get("text", "")).strip()
                    for s in result.get("segments", [])).strip()[:1600]


def regenerate_seo(store: "JobStore", job: Job, force: bool = False) -> int:
    """Write upload metadata for a finished job's clips. Returns how many.

    This is what gives clips made before any of this existed a title, a
    description and tags — including ones whose only remaining trace is the
    mp4 itself.
    """
    from shorts_generator.local.llm import call_local_llm
    from shorts_generator.seo import generate_seo

    with store._lock:
        targets = [dict(c) for c in job.clips
                   if c.get("file") and (force or not c.get("seo"))]
    if not targets:
        return 0

    print(f"[seo] preparing metadata for {len(targets)} clip(s)", flush=True)
    highlights = []
    for c in targets:
        start = c.get("start_time")
        end = c.get("end_time")
        highlights.append({
            "title": c.get("title") or "",
            "start_time": float(start) if start is not None else 0.0,
            "end_time": float(end) if end is not None else float(c.get("duration") or 0.0),
            "score": c.get("score"),
            "hook_sentence": c.get("hook_sentence") or "",
            "virality_reason": c.get("virality_reason") or "",
            "transcript_text": _clip_words(job, c),
        })

    errors: List[str] = []
    written = generate_seo(highlights, video_meta=job.video_meta,
                           source=job.source, llm_fn=call_local_llm,
                           errors=errors)

    # Only keep what a model actually wrote, or fill a clip that had nothing at
    # all. Writing a fallback over metadata that was generated properly would
    # quietly downgrade a good title into a filename — worst on the "Rewrite"
    # button, whose whole job is to improve what is already there.
    kept = 0
    for clip, seo in zip(targets, written):
        if seo.get("generated") or not clip.get("seo"):
            store.set_seo(job, clip["file"], seo)
        if seo.get("generated"):
            kept += 1

    if not kept:
        # Returning a count of fallbacks read as success all the way to the UI,
        # which then reported titles were "ready" when nothing had changed.
        raise RuntimeError(
            errors[0] if errors else
            "The metadata writer returned nothing usable for these clips."
        )
    return kept


class _JobStdout(io.TextIOBase):
    """Captures pipeline prints into the job log, line by line."""

    def __init__(self, store: "JobStore", job: Job) -> None:
        self._store = store
        self._job = job
        self._buf = ""

    def write(self, s: str) -> int:
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._store._log(self._job, line)
        return len(s)

    def flush(self) -> None:
        if self._buf.strip():
            self._store._log(self._job, self._buf)
            self._buf = ""


STORE = JobStore()
