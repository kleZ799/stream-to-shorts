"""Background job runner for the web UI.

The pipeline takes minutes, not milliseconds — transcription alone can run
longer than any sensible HTTP timeout. So a request only ever *enqueues* work;
progress is streamed separately over SSE.

Jobs run one at a time on a single worker thread. That's deliberate: Whisper
and ffmpeg are both CPU-bound, and running two at once makes both slower than
running them in sequence.
"""
import contextlib
import io
import os
import queue
import re
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from shorts_generator.layout_spec import LayoutSpec

from shorts_generator import user_config


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
    log: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    _version: int = 0

    @property
    def out_dir(self) -> str:
        # Resolved live, so changing the save location takes effect immediately.
        return str(user_config.shorts_dir() / self.id)

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
            "source_path": self.source_path,
            "shorts_dir": self.out_dir,
            "log": self.log[-60:],
            "version": self._version,
        }


class JobStore:
    """Thread-safe registry. Single worker, FIFO queue."""

    def __init__(self) -> None:
        self._jobs: Dict[str, Job] = {}
        self._lock = threading.Lock()
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._worker = threading.Thread(target=self._run_forever, daemon=True)
        self._worker.start()

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
        return [j.snapshot() for j in jobs]

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
        from shorts_generator.local.downloader import download_youtube_local
        from shorts_generator.local.llm import call_local_llm
        from shorts_generator.local.transcriber import transcribe_local
        from shorts_generator.render import render_highlights

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
            result = get_highlights(transcript, num_clips=job.spec.num_clips,
                                    llm_fn=call_local_llm)
            all_highlights = result.get("highlights", [])
            if not all_highlights:
                raise RuntimeError("The ranker found no usable moments in this video.")

            top = sorted(all_highlights, key=lambda h: int(h.get("score", 0)),
                         reverse=True)[:job.spec.num_clips]

            self._update(job, stage="render", frac=0.0, message=_STAGE_LABELS["render"])
            shorts = render_highlights(source_path, top, job.spec, out_dir=job.out_dir)

        self._finalize(job, shorts, all_highlights)

    def _finalize(self, job: Job, shorts: List[Dict], all_highlights: List[Dict]) -> None:
        rendered = []
        for i, s in enumerate(shorts, 1):
            path = s.get("clip_url")
            rendered.append({
                "index": i,
                "title": s.get("title"),
                "score": s.get("score"),
                "start_time": s.get("start_time"),
                "end_time": s.get("end_time"),
                "duration": round(float(s.get("end_time", 0)) - float(s.get("start_time", 0)), 1),
                "hook_sentence": s.get("hook_sentence"),
                "virality_reason": s.get("virality_reason"),
                "error": s.get("error"),
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
