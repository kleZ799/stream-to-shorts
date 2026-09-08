# How Stream → Shorts works

> **📚 Study document.** This is a learning and reference companion to the
> codebase — written to be read end-to-end so the whole system can be explained
> from memory. It is *not* setup instructions (see the [README](README.md)) and
> it is *not* a specification: the code is the authority, and this describes the
> code as it stood when written. If the two ever disagree, the code is right and
> this file needs updating.

A complete walkthrough of this codebase: what it does, how it does it, what
technology it uses, and *why* each piece is shaped the way it is.

The [README](README.md) is the pitch. This is the map. Everything here was read
off the current source, so where the README and the code disagree, this file
follows the code and says so ([§17](#17-rough-edges-and-stale-docs)).

Written to be read end-to-end. If you can explain [§1](#1-the-one-paragraph-answer),
[§4](#4-the-pipeline-stage-by-stage), [§6](#6-the-ranking-engine-the-brain) and
[§11](#11-the-backend-job-runner--web-layer), you can explain this project to
anyone.

---

## Contents

**Orientation**
1. [The one-paragraph answer](#1-the-one-paragraph-answer)
2. [The problem this actually solves](#2-the-problem-this-actually-solves)
3. [Tech stack, and why each choice](#3-tech-stack-and-why-each-choice)

**The engine**
4. [The pipeline, stage by stage](#4-the-pipeline-stage-by-stage)
5. [Getting the video](#5-getting-the-video)
6. [The ranking engine — the brain](#6-the-ranking-engine--the-brain)
7. [The three renderers](#7-the-three-renderers)
8. [Natural-language layout parsing](#8-natural-language-layout-parsing)
9. [SEO: the packaging step](#9-seo-the-packaging-step)
10. [Transcription](#10-transcription)

**The application**
11. [The backend: job runner + web layer](#11-the-backend-job-runner--web-layer)
12. [The frontend](#12-the-frontend)
13. [Configuration and precedence](#13-configuration-and-precedence)
14. [Quota accounting and provider fallback](#14-quota-accounting-and-provider-fallback)
15. [Caching: five independent layers](#15-caching-five-independent-layers)
16. [Packaging into a Windows .exe](#16-packaging-into-a-windows-exe)

**Reference**
17. [Rough edges and stale docs](#17-rough-edges-and-stale-docs)
18. [HTTP API reference](#18-http-api-reference)
19. [Interview cheat-sheet](#19-interview-cheat-sheet)

---

## 1. The one-paragraph answer

**Stream → Shorts takes a long video — a stream VOD, a podcast, any mp4 — and
produces a set of ranked vertical clips ready to upload as YouTube Shorts,
Reels or TikToks, each with a title, description and tags written for it.**

It downloads the source with **yt-dlp**, transcribes it locally with
**faster-whisper**, sends the transcript to an **LLM (Gemini or OpenAI)** with a
prompt tuned to find moments that travel, dedupes and scores the results, then
renders the winners with **ffmpeg + OpenCV** into 9:16 video. It ships as a
**FastAPI** backend behind a **vanilla-JS** single-page UI, wrapped in a native
**pywebview** window and packaged by **PyInstaller** into a single Windows .exe
that needs nothing installed.

The whole thing runs on the user's own machine. The only data that leaves is the
transcript text sent to the ranking model — and even that is skipped when the
user names an exact timespan.

### The four stages

```
  get the file   →   transcribe   →   rank moments   →   render clips
    yt-dlp          faster-whisper     Gemini/OpenAI      ffmpeg + OpenCV
   (network)          (slow, CPU)     (fast, costs $)      (medium, free)
```

They are kept strictly separate because **they fail for different reasons and
cost different amounts.** That single observation explains most of the
architecture: the caching, the checkpointing, the provider fallback, the fact
that re-cutting a clip doesn't re-run the pipeline. A failure in one stage must
never throw away what the others already paid for.

---

## 2. The problem this actually solves

Worth knowing, because it explains why this isn't just a wrapper around an API.

The author streams story games and posts Shorts. A 35-minute session has maybe
five clippable moments, and finding them means scrubbing the whole VOD twice.
Off-the-shelf clippers exist — and they fail on stream footage for two specific
reasons:

**1. They crop to the wrong thing.** Auto-croppers slide a vertical window across
the frame hunting for a face. On a stream, the biggest face on screen is usually
a *game character*, so the clip ends up centred on a cutscene with the streamer's
commentary playing from off-frame.

**2. They can't tell the streamer from the game.** A story game's audio is
narration, dialogue and score — all mixed onto the same track as the mic, with no
speaker labels. Generic highlight detection happily returns 45 seconds of
beautifully-written game narration with zero streamer in it. That footage belongs
to the studio, not the channel.

Two fixes, and they are the two most interesting parts of the codebase:

- **`local/gaming_layout.py`** — a webcam-over-gameplay renderer that locates the
  webcam overlay by searching *only the corner it lives in*, so a character's
  face can't win. ([§7](#7-the-three-renderers))
- **`STREAM_VIRALITY_CRITERIA`** — a ranking prompt that teaches the model to
  separate streamer speech from game narration by register, and refuses any clip
  without the streamer in it. ([§6](#6-the-ranking-engine--the-brain))

---

## 3. Tech stack, and why each choice

| Layer | Technology | Why this one |
|---|---|---|
| Language | Python 3.10+ | The whole ML/video ecosystem lives here |
| Web framework | **FastAPI** | Async, typed request bodies via Pydantic, native `StreamingResponse` for SSE, zero-config static serving |
| ASGI server | **uvicorn** | FastAPI's standard runtime; embeddable in a thread, which the desktop app depends on |
| Request validation | **Pydantic** `BaseModel` | Declarative request schemas; malformed JSON is rejected before any handler runs |
| Video download | **yt-dlp** | Actively maintained, handles YouTube's format churn, supports flat metadata-only extraction for channel listing |
| Transcription | **faster-whisper** (CTranslate2) | 4× faster than reference Whisper, runs int8 on CPU, no cloud round trip, no upload |
| LLM | **Google Gemini** (`google-genai`) / **OpenAI** | Gemini's free tier makes the app free to run; OpenAI is the paid fallback |
| Video processing | **ffmpeg** (subprocess) | Single-pass filter graphs do crop/scale/stack without touching frames in Python |
| Computer vision | **OpenCV** (Haar cascades) | Face detection with no model download and no GPU requirement |
| Frontend | **Vanilla JS + CSS**, no framework | Zero build step, ~3.5k lines total, ships as three static files inside the exe |
| Live updates | **Server-Sent Events** | One-directional server→client is exactly the shape of progress reporting; simpler than WebSockets |
| Desktop shell | **pywebview** (Edge WebView2) | A native window with no address bar, using a browser engine Windows already has |
| Packaging | **PyInstaller** | Single-file .exe with ffmpeg bundled — the user installs nothing |
| Config | **python-dotenv** + a JSON settings file | `.env` for developers, `settings.json` for people running the .exe |

### The three non-obvious choices

**Why SSE and not WebSockets.** Progress reporting is purely server→client. SSE
gives that over plain HTTP, auto-reconnects in the browser, and needs no protocol
upgrade or extra dependency. The client is one line: `new EventSource(url)`.

**Why no frontend framework.** The UI has to ship inside a PyInstaller bundle. A
build step means a toolchain in the release pipeline and a `node_modules` in the
repo. Three static files that PyInstaller copies verbatim eliminates that
entirely — and the UI is a single page with maybe eight interactive regions,
which is well inside what vanilla DOM handles cleanly.

**Why subprocess ffmpeg instead of a Python binding.** The renderers build filter
graphs (`crop → scale → vstack`) that ffmpeg executes in one pass with zero
frames crossing into Python. That is the difference between clips rendering in
seconds and rendering in minutes. The one renderer that *does* touch frames in
Python (`facetrack`) is ~20× slower, which the UI warns about explicitly.

---

## 4. The pipeline, stage by stage

### 4.0 First: two front doors that behave differently

This is the thing most likely to confuse you when reading the code:
**the CLI and the app do not run the same pipeline.**

| | `python main.py …` | `desktop.py` / `python -m webapp` |
|---|---|---|
| Orchestrator | `shorts_generator/pipeline.py` | `webapp/jobs.py :: JobStore._execute` |
| Default mode | `api` (MuAPI, cloud) | local, always |
| Layout | face-tracking crop only, hardcoded | any of three, via `LayoutSpec` |
| Uses `render.py` | no | yes |
| Uses `layout_spec.py` | no | yes |
| Writes SEO metadata | no | yes |
| Resumable chunk checkpoints | no | yes |
| Source-quality ladder | no | yes |
| Exact-span fast path | no | yes |
| Output | `output/short_NN.mp4` | `shorts/<job-id>/short_NN.mp4` |

`pipeline.py` is the original upstream shape, kept working. It is the only caller
of `api` mode, where MuAPI does download, transcription, ranking and cropping
server-side. **If you are reading the code to understand the product, read
`webapp/jobs.py::_execute`.** Everything below describes that path.

### 4.1 Module map

```
main.py                     CLI entry — argparse → generate_shorts()
desktop.py                  Desktop entry — uvicorn thread + native window
build_exe.py                PyInstaller wrapper

shorts_generator/
├── pipeline.py             CLI orchestrator; picks api vs local mode
├── config.py               .env loading, env vars, key requirements
├── user_config.py          per-user settings.json, output roots
├── usage.py                daily API-request ledger + Pacific day boundary
├── highlights.py           THE BRAIN — prompts, chunking, scoring, dedupe
├── layout_spec.py          natural language → LayoutSpec; quality ladder
├── render.py               one entry point that dispatches a LayoutSpec
├── seo.py                  per-clip title / description / tags / hashtags
│
├── downloader.py           api mode: MuAPI /youtube-download
├── transcriber.py          api mode: MuAPI /openai-whisper
├── clipper.py              api mode: MuAPI /autocrop
├── muapi.py                thin submit-and-poll client
│
└── local/
    ├── downloader.py       yt-dlp, download cache, channel listing, metadata
    ├── transcriber.py      faster-whisper + .srt cache
    ├── llm.py              Gemini / OpenAI dispatch, retries, quota fallback
    ├── clipper.py          renderer: per-frame face-tracking vertical crop
    └── gaming_layout.py    renderer: webcam-over-gameplay vstack

webapp/
├── __main__.py             python -m webapp
├── server.py               FastAPI routes (~30 endpoints)
├── jobs.py                 job queue, worker thread, progress, persistence
└── static/                 index.html / app.js / style.css — the whole UI
```

### 4.2 Two design rules hold this together

**One transcript shape.** Every transcriber, local or remote, returns exactly:

```python
{"duration": 2130.0, "segments": [{"start": 12.4, "end": 15.1, "text": "..."}]}
```

`highlights.py` cannot tell which one ran. That is the point — the ranking logic
stays a single copy that can't drift between modes.

**The LLM is an argument, not an import.**

```python
get_highlights(transcript, num_clips=5, llm_fn=call_local_llm)
```

`llm_fn` is *the function that calls a model*. Swapping Gemini for OpenAI for
MuAPI touches one line at the call site and nothing inside the ranker. This is
plain dependency injection, and it's why the same prompts drive three backends.

### 4.3 The run, top to bottom

`webapp/jobs.py::JobStore._execute` in ~80 lines:

```
reset_fallback()                      ← undo any previous provider switch
      │
      ▼
download_youtube_local()              ← stage: download   (bar 0.00 → 0.15)
fetch_video_meta()                    ← title/channel/tags, best-effort
      │
      ├─── spec.time_ranges set? ────► render directly, skip everything below
      │
      ▼
transcribe_local()                    ← stage: transcribe (bar 0.15 → 0.55)
      │
      ▼
get_highlights(checkpoint_path=…)     ← stage: rank       (bar 0.55 → 0.70)
sort by score, take top N
      │
      ▼
attach_seo()                          ← titles, descriptions, tags
      │
      ▼
render_highlights(spec)               ← stage: render     (bar 0.70 → 1.00)
      │
      ▼
_finalize() → _persist()              ← job.json written beside the clips
```

**Step 0 — `reset_fallback()`.** A previous run may have fallen back from Gemini
to OpenAI when the daily quota ran out. That choice is process-sticky, so each
new job explicitly re-checks the provider the user actually chose — its quota may
have reset since.

**The exact-span fast path.** If the layout prompt contained something like
`cut 14:45 to 15:30`, the user has already told us what to cut. There is nothing
to transcribe and nothing to rank, so the job goes straight to SEO + render.
Seconds instead of minutes, zero API quota, and no transcript ever leaves the
machine.

---

## 5. Getting the video

`local/downloader.py` handles three input shapes:

- **A local path or `file://` URL** → returned as-is. Nothing downloads.
- **A YouTube video URL** → yt-dlp, saved as `source_<videoid>.mp4`.
- **A channel or playlist URL** → rejected here. The UI resolves it first via
  `POST /api/resolve`, which calls `list_channel_videos()` — yt-dlp's
  `extract_flat` mode, so it's a *metadata-only* call that pulls no media — and
  shows 12 recent videos as a pickable grid.

`is_channel_or_playlist()` distinguishes them by URL shape: a `list=` query
param, or a path starting `/@`, `/channel/`, `/c/`, `/user/`, `/playlist`, or
ending `/videos`, `/streams`, `/shorts`.

### The quality-aware download cache

This is subtler than a normal cache, because the wrong hit is expensive.

`_existing_download()` finds every `source_<id>*` file on disk and ffprobes each
for height, keeping the tallest. `_cache_is_good_enough()` then decides whether
that copy satisfies the *current* request:

| Requested | Rule |
|---|---|
| `best` | Anything ≥ 1440p counts. Below that, check whether the source offers more |
| A number (`1080`) | The cached copy must be within 95% of that height |
| ffprobe can't read it | Assume it's fine — don't re-download on a guess |

A better re-fetch is written with a **quality tag in the filename**
(`source_abc_1080.mp4`) rather than overwriting the old file. That is not
tidiness: **the transcript cache is keyed to the video's filename**, so
overwriting would silently invalidate a transcription that took twenty minutes.

### Video metadata for the SEO writer

`fetch_video_meta()` pulls title, uploader, description and tags without
downloading anything. The transcript says *what was said*; this says *what the
video is about* — the channel, the topic, the words the uploader already ranks
for. It's best-effort: a failure costs a little context, never the run.

---

## 6. The ranking engine — the brain

`shorts_generator/highlights.py` is the most opinionated file in the repo, and
the one that decides output quality.

### 6.1 Content-type detection

One cheap LLM call over the first 25 segments classifies the video (`podcast` /
`interview` / `commentary` / …) and its density (`low` / `medium` / `high`). On
*any* failure it returns `{"content_type": "other", "density": "medium"}` — a
classification is context for the main prompt, never a dependency.

### 6.2 The prompt is assembled from four blocks

```
HIGHLIGHT_SYSTEM_PROMPT
  ├── {virality_criteria}   ← ACTIVE_VIRALITY_CRITERIA
  ├── {cold_open_rules}     ← COLD_OPEN_RULES
  ├── {user_brief}          ← the user's own words, if any
  └── {duration_rule}       ← house default, or what the user asked for
```

**`ACTIVE_VIRALITY_CRITERIA` is the single biggest lever in the codebase.** It
points at `STREAM_VIRALITY_CRITERIA`, written for the mixed-audio-track problem
from [§2](#2-the-problem-this-actually-solves). It teaches the model to separate
the two voices by **register**:

> **Game narration** reads like written prose — literary, past tense, polished,
> no filler words, no self-correction, never addresses anyone.
>
> **The streamer** sounds spoken — reactions, false starts, laughter, swearing,
> questions, talking to chat.

Then the hard rule: **every highlight must contain the streamer's own speech.** A
story beat only counts when the streamer reacts to it, talks over it, or responds
after it.

Its ranked priorities: reaction to a story beat → raw unscripted reactions →
fails and disasters → hot takes → chat interaction → personal tangents →
quotable one-liners → sincerity. Dead air, loading screens and stream
housekeeping are explicitly skipped.

Swap the constant to `VIRALITY_CRITERIA` for podcast or talking-head footage,
which ranks on generic signals: hooks, emotional peaks, opinion bombs,
revelations, conflict, quotables, story peaks, practical value.

**`COLD_OPEN_RULES`** encodes the distribution reality the whole app sorts on. A
Short is judged in its first second; the platform reads early drop-off as a
verdict on the entire clip. So:

- `start_time` goes on the first word of the most arresting line, not the run-up
- at most ~1 second of runway, and only when the payoff is a *sound* (a laugh, a
  scream) where the instant before it lands is what makes it read
- the first line must work alone, read by a stranger
- end on the punch, never trailing into dead air
- **a moment that needs a preamble is not a highlight** — skip it and spend the
  slot on one that can open cold

**`brief_block()`** injects the user's own prompt verbatim, marked as outranking
the generic criteria where they disagree. Someone who types *"only the funny rage
moments"* has told the ranker something the house list cannot know. It explicitly
instructs the model to ignore framing instructions (webcam position, aspect
ratio, clip count) — those belong to the renderer.

### 6.3 Double scoring, and the blend

The model scores each clip **twice, independently**:

- `score` — viral potential of the moment as a whole
- `hook_score` — how hard the opening line *alone* stops a scroll, judged as if
  you cannot see the rest of the clip

`_sanitize_highlights()` blends them into the number everything else sorts on:

```python
HOOK_SCORE_WEIGHT = 0.4
score = 0.4 * hook_score + 0.6 * viral_score
```

The original is kept as `viral_score`, and `hook_score` survives alongside. A
model that ignores `hook_score` isn't punished — an absent value defaults to the
viral score, meaning *"no opinion"*, not zero.

The reasoning: a brilliant moment behind a flat opening line is not a good Short,
because nobody stays long enough to reach it. The opening line gets a real vote.

### 6.4 Sanitising what comes back

Never trust model output. `_sanitize_highlights()` drops non-dicts, negative
starts, `end <= start`, and spans over the ceiling; clamps the rest to the
video's real duration; and coerces every field to its expected type with a safe
default.

The ceiling is `MAX_CLIP_SECONDS` (90) — but `_clip_ceiling()` raises it to
`clip_seconds[1] * 1.2` when the user explicitly asked for long clips. Otherwise
someone requesting 90–120s clips would have every single one silently thrown away
by a limit they never saw.

### 6.5 Chunking long videos

Videos at or over `LONG_VIDEO_THRESHOLD` (1800s / 30 min) are split into
`CHUNK_SIZE_SECONDS` (1200s / 20 min) windows with `CHUNK_OVERLAP_SECONDS` (60s)
of overlap, advancing by `CHUNK_SIZE - OVERLAP` each step. Overlap exists so a
moment straddling a boundary isn't cut in half by the chunking itself.

The critical detail is **rebasing**:

```python
chunk["segments"] = [{**seg, "start": seg["start"] - start,
                             "end":   seg["end"]   - start} for seg in chunk_segs]
chunk["duration"] = end - start
chunk["_offset"]  = start          # added back after ranking
```

Without this, chunks past the first hand the model *absolute* timestamps that are
then clamped against a *relative* duration bound — and every highlight after the
first 20 minutes is silently discarded. **Long videos returned zero highlights
before this fix.** It's the kind of bug that produces no error, just an empty
result.

`seg_end` is the same idea one level down. The window deliberately carries 60s of
segments past its own end so a moment straddling the boundary stays readable —
and the declared duration has to cover that tail, because it is the bound the
clamp measures against. Declaring `end - start` while carrying segments to
`end + 60` meant every highlight found in that last minute was clamped to zero
length and dropped, silently, on three of every four chunks.

### 6.6 Resumable checkpoints

Each chunk costs an API request. A nine-chunk video that died on chunk three used
to throw away the two it had already paid for.

So every finished chunk is written to `<source>.highlights.json` **immediately —
per chunk, not at the end**, because the whole point is surviving the failure
that happens on the *next* one.

Reuse is gated by a fingerprint:

```
v{PROMPT_VERSION}|{duration}|{chunk_count}|{num_clips}|{clip_length}
```

- `PROMPT_VERSION` is in there because chunks ranked by an older prompt are
  answers to a question the app no longer asks. Resuming onto them would hide a
  prompt change from every video that had already been through once.
- Clip length is in there because asking for 30s clips after a run that found 60s
  ones is a *different question*, and reusing those answers would silently ignore
  what was asked for.

### 6.7 Dedupe

```python
overlap > 0.5 * candidate_duration  →  drop
```

Sorted by score descending, so when two candidates overlap the higher-scoring one
is already kept and the weaker one is dropped. Overlap is measured against the
*candidate's own* duration, so a short clip buried inside a long one is correctly
recognised as redundant.

### 6.8 Retry on malformed output

`call_highlight_api()` makes up to `MAX_HIGHLIGHT_API_ATTEMPTS` (3) attempts.
After a failure it appends an explicit *"return ONLY valid JSON, no markdown
fences"* instruction and retries. `_parse_json_loose()` also strips markdown
fences and, failing that, slices from the first `{` to the last `}`.

It asks the model for roughly **2× the requested clip count** so dedupe has
headroom, capped so the model doesn't have to emit a huge JSON payload — which
times out smaller models mid-object.

---

## 7. The three renderers

`render.py::render_highlights()` is the only entry point. Callers never branch on
layout themselves — that's the whole reason the module exists. The repo had grown
two renderers with different shapes plus a centre crop with no home.

### 7.1 Choosing the output size

If `spec.match_source_quality` is on (the default), `pick_output_size()` measures
the crop that will *actually be taken* and climbs a per-ratio ladder as far as
those real pixels justify:

```python
QUALITY_LADDER["9:16"] = [(1080,1920), (1440,2560), (2160,3840)]
```

A rung is allowed when the crop supplies **at least ~85% of its width**. Below
that the renderer would be inventing pixels — which adds no detail and only
inflates the file.

For the stacked layout the maths accounts for the gameplay panel being only ~58%
of output height while still sourced from the *full* frame height, so it has
proportionally more pixels to give.

Net effect: a 1440p stream renders at 1440×2560 instead of being flattened to the
1080p platform minimum.

### 7.2 `stacked` — the webcam-over-gameplay layout

`local/gaming_layout.py`. This is the reason the repo exists.

```
┌──────────────────────┐
│       WEBCAM         │  42%  (CAM_PANEL_FRACTION)
│   auto-located,      │
│  head-and-shoulders  │
├──────────────────────┤
│                      │
│      GAMEPLAY        │  58%  centre crop, nudged away
│                      │       from the webcam corner
└──────────────────────┘
       1080 × 1920
```

The webcam is **not a hardcoded rectangle.** Each clip gets its overlay located
from scratch:

1. **Sample 6 frames** (`SAMPLE_COUNT`) spread evenly across the clip's span.
2. For each, extract one frame via ffmpeg and run a **Haar frontal-face cascade —
   only inside the quadrant the overlay lives in** (`corner`, default
   `bottom-left`). This is the fix for problem #1 in [§2](#2-the-problem-this-actually-solves):
   a character's face elsewhere in the frame physically cannot win.
3. Take the **median** of the hits for centre-x, centre-y and face width. Median,
   not mean — one bad detection (a frame where the streamer looked away) can't
   drag the framing off.
4. Build the crop at `face_w × FACE_CONTEXT_MULTIPLE (5.0) × (1 - CAM_EDGE_INSET)`,
   4:3 shaped, face anchored at `FACE_VERTICAL_ANCHOR` (0.42) down the panel. The
   edge inset trims the overlay's own border so it doesn't show as a black sliver.
5. Clamp the rect inside the frame **without changing its size**, and round every
   coordinate to even numbers — h264 chroma subsampling requires it.

Then the whole thing renders in **one ffmpeg pass**:

```
[0:v]crop=cam…,scale=1080:806,setsar=1[cam];
[0:v]crop=game…,scale=1080:1114,setsar=1[game];
[cam][game]vstack=inputs=2[v]
```

No per-frame Python. Clips render in seconds, not minutes.

If no face turns up in any sample, it falls back to a full-height centre crop and
**says so in the log** rather than silently shipping something wrong.

### 7.3 `facetrack` — the talking-head crop

`local/clipper.py`. The upstream approach, kept for podcast footage where the
speaker fills the frame.

Two stages per clip: ffmpeg cuts the span (`-preset ultrafast -crf 18` — it's an
intermediate that gets re-encoded anyway, so favour speed at near-transparent
quality), then OpenCV walks **every frame**, detects the largest face, and slides
a crop window toward it with `smoothing = 0.15` so the framing eases rather than
snaps. Finally ffmpeg muxes the original audio back onto the silent OpenCV output
and re-encodes to h264 (OpenCV writes mpeg4, which uploads don't universally
accept).

Three fixes in here, each of which produced an opaque failure:

- **Downscaled detection.** Haar runs on a copy scaled to 640px wide, then
  coordinates scale back up. At 1440p, full-resolution detection cost more than
  the entire rest of the pipeline combined.
- **`.copy()` on the crop.** OpenCV's writer throws an unreadable C++ exception on
  a non-contiguous NumPy view at high resolution.
- **`finally: cap.release(); writer.release()`** plus retry-with-backoff temp
  deletion. Windows holds the file lock until both handles close — otherwise a
  real encoding error surfaces as a confusing `WinError 32` from the cleanup path,
  masking the actual problem.

This mode is **~20× slower** than the other two, and `LayoutSpec.warning()` says
so in the UI before the user commits to a long render.

### 7.4 `center` — plain centre crop

`render.py::_render_center_clip`. Widest centre crop at the target ratio, one
ffmpeg pass, no face detection at all. For *"gameplay only, no webcam"*.

---

## 8. Natural-language layout parsing

`layout_spec.py` turns free text into a validated `LayoutSpec`. Type
*"webcam at the top, square, 5 clips"* and the frame preview updates as you type.

### 8.1 Two stages, in this order

**Stage 1 — deterministic keywords.** Runs in ~70ms, costs nothing, works
offline. This is what the live preview calls on every keystroke (debounced 450ms).

**Stage 2 — the LLM.** Consulted **only when the keyword pass resolved absolutely
nothing.** Anything the keywords did resolve came from the user's literal words,
so a second opinion adds latency and quota without adding accuracy. And if the
LLM call fails, the parser notes it and keeps the defaults — a layout prompt is a
convenience, never a hard dependency.

### 8.2 Parse order is load-bearing

Each step **strips the text it matched**, so later steps can't misread the
leftovers:

| # | Step | Why here |
|---|---|---|
| 1 | **Clip length** (`"between 20 and 40 seconds"`) | Reads identically to a timespan. Getting this order wrong turned a request for 20–40s clips into a single exact cut from 0:20 to 0:40, with ranking skipped entirely. The word `cut` is the app's explicit-span verb, so its presence hands the phrase to the range parser instead |
| 2 | **Time ranges** (`"14:45 to 15:30"`) | Stripped so `1:30-2:45` can never be mistaken for an aspect ratio later |
| 3 | **Clip count** (`"5 clips"`) | Stripped so `5 shorts` can't *also* be read as a request for 9:16 |
| 4 | **Aspect ratio** | Explicit ratios and unambiguous words before loose platform names |
| 5 | **Layout** | Most specific first: "no webcam"→`center`, "follow my face"→`facetrack`, "webcam on top"/"pip"→`stacked` |
| 6 | **Webcam corner** | Only read when the phrasing is about *locating an overlay* — "webcam on top" is a layout instruction, not a corner |
| 7 | **Panel size** | bigger→0.55, smaller→0.30, or explicit `"60%"` |
| 8 | **Face zoom** | "closer"→3.0, "wider"→7.5 |

Timestamp parsing accepts `1:30`, `00:01:30`, `90s`, and bare numbers *only*
inside `from X to Y` (bare numbers collide with percentages and resolutions
otherwise). The regex's trailing `\b` matters: without it, `"928 square"` parses
as `"928 s"` and eats the leading letter of the next word.

### 8.3 What comes out

```python
LayoutSpec(
    layout="stacked",              # stacked | facetrack | center
    aspect_ratio="9:16",           # 9:16 | 4:5 | 1:1 | 16:9
    webcam_corner="bottom-left",
    cam_panel_fraction=0.42,       # clamped 0.15–0.75
    face_zoom=5.0,                 # clamped 2.0–12.0
    num_clips=10,                  # clamped 1–10
    match_source_quality=True,
    time_ranges=[],                # [[start, end], …] → skips transcribe + rank
    clip_seconds=None,             # [min, max] → becomes a prompt instruction
    brief="",                      # the whole prompt, handed to the ranker
    notes=[],                      # human-readable "what I understood"
)
```

Two subtleties worth knowing:

**`clip_seconds` is a request to the model, not a trim.** Cutting a clip to
length afterwards would slice mid-sentence, so the length has to be part of what
the ranker is *asked to find*.

**`brief` keeps the whole prompt, unparsed.** Framing words in it are harmless
noise to the ranker, and trying to subtract them would cost exactly the nuance
(*"only the rage moments"*, *"hooks that ask a question"*) that makes the brief
worth having.

`validate()` clamps every field into a renderable range and **never raises** —
bad input degrades to a working render rather than an error. `from_dict()`
rebuilds a spec from a saved manifest, so re-cutting a clip months later still
renders in the layout it was first made with.

`notes` is why the UI can show *"clip count → 5"*, *"aspect ratio → 1:1"* under
the preview: the parser reports what it understood, so the user can see their
words actually landed.

---

## 9. SEO: the packaging step

A rendered clip is only half the job. What decides whether a Short gets seen is
the packaging: a title that earns the tap, a description that tells the algorithm
what the clip is about, and tags that file it next to the videos its audience
already watches.

`seo.py` asks the same model that ranked the highlights to write that packaging —
in **one call for the whole batch**, not one per clip. Ten separate calls would
take ten times as long and give the model no way to stop the titles repeating
each other.

### The two rules that outrank everything

1. **Accurate.** Every claim must be provable from that clip's own transcript,
   which is given to the model. A title the clip fails to deliver gets swiped in
   two seconds, and short-form ranking punishes that harder than a boring title
   ever could.
2. **Viral.** Within what is true, pick the most arresting framing. A real
   specific detail — the exact number, the actual thing that happened — beats a
   vague tease every time.

### What the prompt encodes about Shorts distribution

- Titles and descriptions **outrank tags** as ranking signals, so the words that
  name the subject must appear in the title and the first line of the
  description, not only in the tag box.
- The **first three hashtags** render as clickable links above the title, making
  them the most visible metadata on the whole upload.
- A hashtag **in the title** buys nothing and spends characters you need for
  keywords.
- **The title must name the subject** — the game, person or topic. A title lifted
  straight from the transcript reads fine to someone who watched the stream and
  is invisible to everyone else, because it contains no word anyone would search
  or browse for.

Limits are enforced in code, each set slightly *under* YouTube's real cap so a
stray character can't get the upload rejected: `TITLE_LIMIT` 100,
`DESCRIPTION_LIMIT` 4800, `MAX_TAGS` 12, `MAX_HASHTAGS` 5, `TAGS_TOTAL_LIMIT` 460
(real cap 500).

### Best-effort, but honestly reported

This step sits on top of a clip that **already exists**, so every failure path
falls back to a hook-line title rather than sinking a run that already paid for a
download, a transcription and a render.

But the caller must be able to tell the difference. So each entry carries a
`generated` flag, and failures are appended to an `errors` list rather than only
printed. `regenerate_seo()` uses that flag carefully:

```python
if seo.get("generated") or not clip.get("seo"):
    store.set_seo(job, clip["file"], seo)
```

Only overwrite existing metadata when a model actually wrote the replacement.
Without that check, the **"Rewrite" button — whose entire job is to *improve*
what's there — would quietly downgrade a good title into a filename.**

And if *nothing* was generated, `regenerate_seo()` raises. Returning a count of
fallbacks read as success all the way to the UI, which then reported titles were
"ready" when nothing had changed.

### The title is also the filename

`short_01.mp4` tells you nothing in a folder of thirty. Since ranking and SEO
both run *before* rendering, the title exists by the time the mp4 does — so
`_finalize()` renames each clip to it, and rewriting the titles renames the files
again. `safe_stem()` makes that survive a filesystem and a URL path segment:
emoji and the characters Windows forbids are dropped, combining marks are kept
(losing them turned Hindi's `क्या` into `क य`), reserved device names and
over-long stems fall back, and collisions take a `_2` suffix.

### Finding the words for old clips

`_clip_words()` works cheapest-first: slice the clip's span out of the cached
`.srt` if one exists (free), otherwise transcribe the clip file itself (quick — a
Short is under a minute). This is what gives clips made before any of this
existed a real title, including ones whose only remaining trace is the mp4.

---

## 10. Transcription

`local/transcriber.py`, faster-whisper.

**Device selection** is auto by default: `cuda` + `float16` when a usable GPU is
found, otherwise `cpu` + `int8`. Getting this right took three separate fixes,
and each failure was silent — worth studying as a case of *asking the wrong
component a reasonable-sounding question*:

1. **Ask CTranslate2, not torch.** The original probe called
   `torch.cuda.is_available()`. But faster-whisper does not run on torch — it
   runs on **CTranslate2**, and torch is not installed here at all (it is an
   explicit `--exclude-module` in the build, because it adds ~2GB and is not
   used). So the import raised `ImportError`, the handler swallowed it, and
   every machine took the CPU path. The correct probe is
   `ctranslate2.get_cuda_device_count() > 0`.
2. **Register the DLL directories.** `nvidia-cublas-cu12` and
   `nvidia-cudnn-cu12` install their DLLs under `site-packages/nvidia/*/bin`.
   Python 3.8+ removed the current directory and `PATH` from the DLL search
   order, and CTranslate2 — unlike torch — never calls `os.add_dll_directory`
   for them. So *installing the wheels changes nothing*: the device counts, the
   model constructs, and the first `encode()` dies on `cublas64_12.dll is not
   found`.
3. **Ship them.** PyInstaller cannot see a dependency that is resolved by DLL
   search path rather than by import, so the build needs
   `--collect-binaries nvidia`.

The lesson worth keeping: **a device that enumerates, and a model that
constructs on it, are both worthless as proof.** Only draining the segment
generator exercises the maths library. So the fallback wraps the *entire*
transcription, not the constructor:

```python
try:
    segments, info = _run(device, compute_type)      # drains the generator
except Exception:
    if device != "cuda":
        raise
    device, compute_type = "cpu", "int8"
    segments, info = _run(device, compute_type)      # redo the whole thing
```

Measured on an RTX 5060 Laptop (8GB), 900s of audio:

| model | device | time | realtime factor |
|---|---|---|---|
| base | cuda | 25.3s | 35.6x |
| base | cpu | 46.1s | 19.5x |
| small | **cuda** | **20.8s** | **43.3x** |
| small | cpu | 104.2s | 8.6x |

Note `small` on CUDA beats `base` on CUDA. On a GPU the *more accurate* model is
also the faster one, which matters because model size is the main defence
against the hallucination described next.

**Language pinning.** The spoken language defaults to `en` rather than
auto-detect. Whisper re-decides the language on unclear audio, and on a game
stream — music beds, effects, non-speech — it drifts and then generates fluent,
confident text in the language it landed on. A real 3h47m English VOD came back
with **703 of 1097 cues in Korean**, none of it spoken. One of those hallucinated
cues propagated into a clip's `hook_sentence`, then into its SEO title (via the
fallback in [§9](#9-seo-the-packaging-step)), then into the filename on disk.
`"auto"` is still available and is the only value that restores auto-detection.

**Settings that matter:**

- `beam_size=5`
- `condition_on_previous_text=False` — stops Whisper looping a hallucinated
  phrase forward through a long VOD. Note this limits *propagation* of a
  hallucination, not its *occurrence*; language pinning addresses the latter.
- **VAD off by default.** Voice-activity detection is too aggressive on mixed
  speech/music content — a stream with a game score under the mic loses real
  speech to it. Enable with `LOCAL_WHISPER_VAD_FILTER=true`.

**The cache** is an `.srt` written next to the video, validated by modification
time (`cache_mtime >= source_mtime`). SRT rather than JSON because it's a format
the user can open, edit, and hand to a subtitle tool.

An empty or zero-duration cache is treated as **invalid**, deleted, and
re-transcribed — that shape comes from a run that died partway, and honouring it
would mean an empty transcript forever.

**This is the slowest step in the pipeline and it is paid exactly once per
video.** Every re-rank and re-render afterwards is free. That fact drives the
workflow in [§15](#15-caching-five-independent-layers).

---

## 11. The backend: job runner + web layer

### 11.1 Why a queue exists at all

Transcription alone outlives any sensible HTTP timeout. A three-hour VOD is not a
two-second wait.

So `POST /api/jobs` **only ever enqueues and returns an id**. The browser follows
progress separately over server-sent events. The request never blocks on the
pipeline.

### 11.2 One worker, on purpose

```python
self._worker = threading.Thread(target=self._run_forever, daemon=True)
```

Jobs run **one at a time on a single worker thread**. That is deliberate, not a
limitation: Whisper and ffmpeg are both CPU-bound, and running two at once makes
both slower than running them in sequence. A queued job reports its depth
(*"Queued — 2 job(s) ahead"*) rather than pretending to run.

The pattern is a classic producer/consumer: FastAPI handlers produce job ids into
a `queue.Queue`, the worker consumes them forever. All shared state is guarded by
a single `threading.Lock`.

### 11.3 Progress is derived, not guessed

This is the nicest trick in the backend.

The pipeline already narrates itself to stdout. `_JobStdout` — a tiny
`io.TextIOBase` subclass — captures that stream line by line inside a
`contextlib.redirect_stdout`, and `_log()` maps each line's prefix onto a stage:

```python
[download…]                        → download
[transcribe…]                      → transcribe
[highlights…] [llm…] [rank…]       → rank
[stack…] [clip/local…] [center…]   → render
```

Each stage owns a band of the progress bar, sized by how long it actually takes:

```python
download   0.00 → 0.15
transcribe 0.15 → 0.55      # dominates on CPU, so it gets the widest band
rank       0.55 → 0.70
render     0.70 → 1.00
```

And when a line carries an `N/M` counter — `[stack] 2/5: …` — the bar moves to
the right position *inside* the render band:

```python
frac = (done - 1) / max(1, total)
job.progress = lo + (hi - lo) * frac
```

**The pipeline has no idea a UI exists.** It just prints what it's doing, and a
render reporting `3/5` moves the bar correctly without any coupling.

Progress is monotonic (`max(job.progress, …)`), so a late-arriving line can never
make the bar jump backwards.

### 11.4 Streaming it to the browser

```python
@app.get("/api/jobs/{job_id}/stream")
async def stream_job(job_id: str) -> StreamingResponse:
    async def gen():
        last = -1
        while True:
            snap = job.snapshot()
            if snap["version"] != last:      # only send on real change
                last = snap["version"]
                yield f"data: {json.dumps(snap)}\n\n"
            elif idle % 30 == 0:
                yield ": ping\n\n"           # keep-alive every ~15s
            if snap["status"] in ("done", "error"):
                break
            await asyncio.sleep(0.5)
    return StreamingResponse(gen(), media_type="text/event-stream")
```

Every mutation bumps `job._version`. The generator polls twice a second but
**only emits when that version actually changed**, so an idle job costs one
comment line every 15 seconds instead of two full snapshots per second. The
stream closes itself when the job finishes.

`X-Accel-Buffering: no` and `Cache-Control: no-cache` stop proxies from buffering
the stream into uselessness.

### 11.5 Not blocking the event loop

FastAPI handlers are `async`, so any blocking call would stall every other
request. Anything slow goes through a thread:

```python
spec = await asyncio.to_thread(parse_layout_prompt, req.prompt, None, req.use_llm)
written = await asyncio.to_thread(regenerate_seo, STORE, job, force)
result = await asyncio.to_thread(_render)
```

### 11.6 Persistence — surviving a restart

Every finished job writes a `job.json` manifest beside its clips, written to a
temp file then `os.replace`d so a crash mid-write can't corrupt it (atomic
rename). Manifest writes **never raise**: losing a manifest costs the library a
row, but letting the write throw would cost the user a render they already waited
for.

`restore()` runs at startup and on every `/api/library` hit, adopting two shapes
of folder:

- **With a manifest** → rebuilt completely: titles, scores, spans, SEO. Clips
  listed but no longer on disk are dropped (someone deleted them by hand).
- **Bare mp4s** → adopted from filenames alone, with `_title_from_filename()`
  turning `short_03.mp4` into `Clip 3`. These are clips from before manifests
  existed; without this they'd be invisible to the app that made them.

Before this existed the store was purely in-memory — close the app and every clip
you'd ever made became unreachable through the UI, even though the mp4s were
still sitting in the folder.

### 11.7 Clip editing — re-cut from source

`POST /api/jobs/{id}/clips/{file}/trim` re-runs the **renderer over the original
download** with new timestamps. It does not trim the rendered file.

That distinction *is* the feature: re-rendering means the span can **grow** as
well as shrink, and the layout stays exactly what the user asked for the first
time. Trimming the output could only ever remove.

The new render gets a fresh `edit_NN_<timestamp>` name, replaces the old entry
**in place** (keeping its position in the list), and the superseded file is
deleted.

### 11.8 Security posture

The app binds `127.0.0.1` and has **no authentication**, which is correct for a
local desktop app and stated plainly in the warning `python -m webapp` prints
when you bind elsewhere.

The defences that do exist are about path handling, since filenames come from
the client:

```python
def _clip_path(job, filename: str) -> Path:
    safe = os.path.basename(filename)          # strip any directory part
    root = Path(job.out_dir).resolve()
    path = (root / safe).resolve()
    try:
        path.relative_to(root)                 # prove it didn't escape
    except ValueError:
        raise HTTPException(403, "That file is outside the job folder.")
    return path
```

`os.path.basename` first, then resolve, then prove containment with
`relative_to`. Uploads are similarly constrained: extension checked against a
whitelist, filename sanitised to `[A-Za-z0-9._-]`, collisions given a numeric
suffix rather than overwriting.

And one deliberate non-parameter:

```python
# The URL is hardcoded on purpose — this opens an external page, so it must
# not be steerable by anything the page sends.
url = "https://www.youtube.com/upload"
```

### 11.9 Disk cleanup

`/api/cleanup` is deliberately narrow: **downloaded source videos and
half-finished `.part` files only.** Rendered clips live elsewhere and are the
whole point of the app. Transcripts stay too — a `.srt` is a few hundred KB and
saves re-transcribing hours of audio.

It refuses to run while a job is in flight (409), since the source that job is
reading is exactly what would be deleted.

---

## 12. The frontend

`webapp/static/` — three files, ~3,500 lines, **no framework, no build step.**

| File | Lines | What |
|---|---|---|
| `index.html` | 444 | Full page markup + an inline SVG sprite sheet |
| `app.js` | 1,514 | All behaviour, plain DOM, no dependencies |
| `style.css` | 1,514 | Design system in CSS custom properties |

### 12.1 The design decision

The UI is built on **YouTube's own layout** — masthead, left guide rail, card
grid, a player with a right-hand control rail — because the audience is people
who post to YouTube. They already know how to use it. From the CSS header:

> *Surfaces, spacing and motion follow youtube.com's dark theme so the app feels
> like a place the user has already been; the neon accents and depth are ours.*

The design system is CSS custom properties on `:root`: YouTube's dark surfaces
(`--bg: #0f0f0f`, `--raised: #181818`, `--hover: #272727`), an accent set
(`--red: #ff0033`, `--blue: #3ea6ff`), a radius scale, and **one easing curve for
everything that moves** (`cubic-bezier(.2,.8,.25,1)`) with two durations. One
easing function across the whole app is what makes motion feel like a single
system rather than a pile of separate animations.

### 12.2 Page structure

```
masthead        search box (paste a link) · file picker · Create · folder · settings
guide rail      Create · Your clips · Settings · Clips folder · Upload to YouTube
main
 ├── setup panel        first-run only: provider + API key
 ├── chip bar           clickable example phrases
 ├── 1 Source           drag-drop zone / URL / channel grid
 ├── 2 What to make     the layout prompt + aspect toggle
 ├── 3 Render           format picker + Generate button
 ├── side: Live preview the 9:16 frame, redrawn as you type
 ├── progress panel     stage pills, % bar, live pipeline log
 └── results panel      search + date chips, then the clip grid
player overlay   video + control rail + trim panel + SEO panel
mini player      persists while you scroll
drawer           settings: provider, model, budget meters, save location, cleanup
```

### 12.3 The live preview

The single most important interaction. As you type, the preview redraws **the
real frame shape and the real webcam panel height** — so you can see your words
land before spending a second of render time.

```js
$("prompt").addEventListener("input", () => {
  syncChips();
  clearTimeout(specTimer);
  specTimer = setTimeout(refreshPreview, 450);   // debounce
});

async function refreshPreview() {
  try {
    const d = await api("/api/layout/preview", json("POST", {…}));
    drawPreview(d.spec, d.summary, d.notes, d.warning);
  } catch (_) { /* the preview is cosmetic — never block on it */ }
}
```

Three things to notice:

- **Debounced 450ms**, so typing doesn't fire a request per keystroke.
- **The parse happens server-side** — `POST /api/layout/preview` runs the *same*
  `parse_layout_prompt()` the real job will run. There is no duplicated parsing
  logic in JS that could drift from the Python.
- **The catch is empty on purpose.** The preview is cosmetic; a failed preview
  must never block the user from rendering.

`drawPreview()` sizes a div to the spec's real aspect ratio, sets the webcam
panel to `height × cam_panel_fraction`, prints the resolution, lists the parser's
`notes` so the user sees what was understood, and shows the exact-cut box when
`time_ranges` came back.

### 12.4 The chips

The example chips (`"webcam at the top"`, `"cut 14:45 to 15:30"`) aren't buttons
that set hidden state — they **insert their phrase into the prompt text**, and
`syncChips()` lights up any chip whose phrase is currently present. Clicking an
active chip removes it, with a regex that avoids leaving a stray comma behind.

The prompt box stays the single source of truth. The chips are just shortcuts for
phrases the parser already understands.

### 12.5 Following a job

```js
es = new EventSource(`/api/jobs/${job.id}/stream`);
es.onmessage = (ev) => onUpdate(JSON.parse(ev.data));
es.onerror = () => {
  // The stream drops when the job ends; fall back to one direct read.
  es.close();
  fetch(`/api/jobs/${job.id}`).then(r => r.json()).then(onUpdate).catch(() => {});
};
```

`onUpdate()` writes the stage label, the percentage, a `scaleX()` transform on
the bar (transform, not width — it's GPU-composited and doesn't trigger layout),
the stage pills, and the live log.

Two details that show care:

**The log auto-scrolls only if you were already at the bottom.**

```js
const stuck = el.scrollTop + el.clientHeight >= el.scrollHeight - 30;
el.textContent = s.log.join("\n");
if (stuck) el.scrollTop = el.scrollHeight;
```

Scroll up to read something and it stops yanking you back down.

**`finish()` is idempotent.** The stream's last message and the error-path
fallback read can both arrive, so a `finished` guard makes sure the completion
path runs once.

**Existing clips stay on screen while a new run works** — the results grid isn't
cleared on submit.

### 12.6 Searching the library

A library built up over weeks needs a way in that isn't scrolling. The search box
matches everything a clip carries — title, hook, tags, filename, and the title of
the video it was cut from — because you rarely remember which of those holds the
phrase you are searching by. Terms are ANDed, so a second word narrows.

The date chips filter by *run* rather than by clip, since every clip in a run was
rendered at once, and `Today` means the calendar day rather than the last 24
hours: a clip made last night is not one you made today.

The subtle part is indexing. Cards used to carry a counter that walked every run
in order, which assumed the grid always showed everything — filtering under that
assumption would have made cards open the wrong clip. Each clip now carries its
own position in `clips`, reassigned by `reindex()` whenever that array changes,
and arrow-key navigation walks the *visible* list so it matches the screen.

### 12.7 The player and trim editor

The player is a `<video>` with a custom control rail (Play, Sound, Trim, Boost,
Save, Download, Show file, Delete, Mini) and keyboard shortcuts —
<kbd>Space</kbd>, <kbd>M</kbd>, <kbd>T</kbd>, <kbd>B</kbd>, <kbd>F</kbd>.

The **trim panel** is a two-handle range control built on pointer events, plus
`−1s`/`+1s` nudge buttons and text fields that accept clock format (`1:30`).
Handles drag, the fill between them updates live, and the length readout tracks.
Applying it POSTs to the trim endpoint and swaps the clip in place when the
re-cut returns.

The **mini player** persists as you scroll away, and an `IntersectionObserver`
drives the guide rail's active-section highlight.

The **SEO panel** ("Boost") shows the generated title, description, hashtags and
tags with copy buttons, a "Rewrite" action (`?force=true`), and copy-everything.

### 12.8 Settings drawer

Reads `/api/settings` and `/api/usage` to render:

- Provider switcher (Gemini / OpenAI) — offering a switch rather than asking for
  a key that's already on disk
- Model picker, with each Gemini model's **free-tier daily allowance shown next
  to it**. The allowance differs enormously (20/day on one, 1000 on another), and
  picking wrong is the difference between a working afternoon and a paid API. The
  choice is made with the number in view.
- **Budget meters** — requests used today vs the daily cap, and time until reset
- Save-location picker and the disk cleanup scanner

If a setting is pinned by an environment variable, the API reports
`provider_pinned` / `model_pinned` and the UI says so — the difference between a
switch that looks broken and one that explains itself.

### 12.9 Small things that make it feel finished

- **`esc()` on every interpolation** — all user and model text is HTML-escaped
  before it reaches `innerHTML`.
- **A toast** for transient feedback, a **confirm dialog** for destructive
  actions ("The file is removed from your PC. This cannot be undone.").
- **A top loading bar** mirrors job progress even when the progress panel is
  scrolled out of view.
- **Drag-and-drop** anywhere on the drop zone, plus a file picker, plus URL
  paste, plus the channel grid — four ways in.
- **Usage refreshes after every run** — including failed ones, which is exactly
  when knowing what's left matters most.

---

## 13. Configuration and precedence

Three layers, and the order between them is deliberate:

```
1. Real environment variables         ← always wins
2. .env files                         ← loaded with override=False
3. %APPDATA%\StreamToShorts\settings.json
```

**Why env wins.** Someone with a working `.env` setup keeps behaving exactly as
before when the settings UI is added. Existing setups don't break.

**Why `.env` is searched in three places** (`config.py::_env_file_candidates`):
a bare `load_dotenv()` searches the *current directory*, and the packaged build
`chdir`s to the user's Videos folder before any of this is imported. Someone who
put their key in the repo's `.env` and then ran the `.exe` got told the key "is
not set" — true only of the directory the app happened to be standing in. So it
also looks beside the executable and in the config directory.

**Why `user_config.load()` records *why* it failed.** This file holds the user's
API key. Reading it as `{}` because of a bad byte or a locked handle turns a
fixable problem into *"GEMINI_API_KEY is not set"* twenty minutes into a run,
pointing the user at a `.env` that was never involved. So the reason is
remembered, and `_where_we_looked()` puts it in the error that finally reaches
them:

> *A GEMINI_API_KEY saved in C:\…\settings.json was NOT used, because that file
> is not valid JSON (…). Fix or delete that file, or set GEMINI_API_KEY in the
> environment.*

A **missing** file is ordinary — a first run has none — and stays silent. A file
we can *see* but cannot *use* is a bug and says so, once per distinct reason.

**Why `save()` can refuse.** If a config file exists but won't parse, merging
onto a silently-empty dict would drop the API key it holds. It raises instead of
destroying the thing it was asked to update. It also `chmod 600`s the file where
that's meaningful.

**Live re-reading.** `current_provider()` and `current_model()` go through
`user_config` on every call, so switching provider in the UI takes effect on the
next request without a restart.

### Where things land

| | Path |
|---|---|
| Settings + usage ledger | `%APPDATA%\StreamToShorts\` (`~/.config` Linux, `~/Library/Application Support` macOS) |
| Source videos, `.srt`, `.highlights.json` | `<OUTPUT_ROOT>/output/` |
| Rendered clips + `job.json` | `<OUTPUT_ROOT>/shorts/<job-id>/` |
| `OUTPUT_ROOT` default | cwd — which is `~/Videos/StreamToShorts` in the packaged build |

`set_output_root()` **proves it can write** to a new location — creates the
folder, writes a probe file, deletes it — before saving the setting. A save
location that turns out to be read-only fails at the moment you choose it, not
twenty minutes into a render.

---

## 14. Quota accounting and provider fallback

`usage.py` exists because of one specific failure: Gemini's free tier caps
requests **per day**, and the only way the app learned it had hit the cap was a
429 three chunks into a run that had already paid for a download and a
transcription.

### The ledger

`usage.json` sits next to `settings.json` and tracks requests **per provider per
model**.

Per-model matters: Google's daily cap is per project *per model*, so a model that
has never been called still has its full allowance — which is exactly why
switching models is a way out of a spent quota. Marking the *provider* dead would
lock out models with a far bigger allowance that were never touched.

Known free-tier limits are hardcoded (`gemini-2.5-flash-lite` 1000/day,
`gemini-2.5-flash` 250, `gemini-2.5-pro` 100, `gemini-3.6-flash` 20) and used
only to **annotate** the model list. The *list* of available models is asked of
the API, never hardcoded — a hand-written list here already went stale and
offered a withdrawn model that 404'd mid-run.

Yesterday's tally isn't an error, it's spent history: a ledger whose `day`
doesn't match today reads as blank.

### The count is advisory; the 429 is truth

```python
def is_exhausted(provider, model):
    """Deliberately ignores the local count."""
```

The count assumes the free tier and assumes this machine is the only thing
spending the key. Refusing to call an API because a *guess* said so would strand
anyone on a paid plan after twenty requests. **Only a 429 that names the daily
quota stops a run**, via `mark_exhausted()`.

### Telling per-minute from per-day

Both arrive as a 429. The per-minute one clears in a minute and is worth sleeping
through; the per-day one doesn't clear until midnight Pacific, so sleeping on it
just wastes five minutes before failing anyway.

Google names the quota in the payload — `GenerateRequestsPerDayPerProject` —
which `_is_daily_quota()` matches on. It's the only reliable way to tell them
apart.

### The Pacific day boundary, spelled out by hand

Google's free-tier day rolls over at midnight **US Pacific** — not local, not
UTC. `zoneinfo` would need the `tzdata` package on Windows, which is one more
thing to bundle into the exe and one more way for a build to break. The US
Pacific DST rule is stable and short, so it's written out directly: second Sunday
of March to first Sunday of November, `-7` inside, `-8` outside.

### Retry and fallback — `local/llm.py`

By the time a Gemini call happens, the run has already paid for a download and a
transcription, so a blip must not sink it. **The usual rule is inverted:**

> Give up immediately only on errors retrying can never fix — bad key,
> `PERMISSION_DENIED`, `INVALID_ARGUMENT`, `404`, a spent daily allowance — and
> **retry everything else.**

Five attempts. If the error carries a `retry in Xs` hint, that exact delay is
honoured (the rate limiter told us precisely how long to wait); otherwise
exponential backoff capped at 60s.

When the daily quota *is* gone and an OpenAI key is configured, the run **switches
providers mid-flight** rather than losing the work. The switch is process-sticky
— no point asking the spent provider again on every remaining chunk — and
`reset_fallback()` clears it at the start of each new job.

Two Gemini-specific details:

- `max_output_tokens = 32768` because Gemini 3.x spends part of its output budget
  on **internal reasoning** before emitting any JSON; 8192 truncated long-chunk
  responses mid-object.
- `check_gemini_model()` probes a model with a 1-token request before saving it,
  because **ListModels is not proof of anything** — it happily returns models
  that answer *"no longer available to new users"* when actually called, and that
  404 used to surface nine chunks into a run. One tiny request at the moment of
  choosing is far cheaper than discovering it later.
- An empty response reports its `finish_reason` rather than failing as
  "invalid JSON", so you learn *why* it came back empty.

---

## 15. Caching: five independent layers

Each guards a different expense:

| Cache | Where | Keyed by | Guards |
|---|---|---|---|
| Download | `output/source_<id>*.mp4` | video id + probed height | Re-downloading gigabytes |
| Transcript | `output/<stem>.srt` | filename + mtime | The slowest step in the pipeline |
| Chunk rankings | `output/<stem>.highlights.json` | prompt version, duration, chunk count, clip count, clip length | API quota already spent |
| Job manifest | `shorts/<id>/job.json` | — | Clips becoming invisible after a restart |
| Gemini model list | in-process, 600s | — | A network round trip on every settings load |

### The workflow this enables

Ranking and rendering are separate on purpose, because they fail for different
reasons and cost different amounts.

1. **First pass** — transcribe, rank, render. Slow, paid once per VOD.
2. **Read the picks.** They're plain JSON with timestamps. Nudge a start time
   back three seconds, drop the one that didn't land, retitle the good ones.
3. **Re-render from the edited list** — with **no LLM calls at all.** On a free
   tier this matters: re-ranking a long VOD burns quota, and once you've
   hand-picked five timestamps, re-ranking is pure waste.

One trick worth stealing: **transcribe from the 720p download, render from a
1440p one.** Whisper doesn't care about resolution and CPU transcription is the
bottleneck, so one session yields both cheap transcription and a sharp render.
The root-level `render_1440.py` / `render_from_saved.py` scripts are exactly
this workflow, scripted.

---

## 16. Packaging into a Windows .exe

`build_exe.py` wraps PyInstaller. `--onedir` (default) starts faster;
`--onefile` is a single self-contained exe that unpacks itself each launch.

**Bundled:** `webapp/static`, and `./bin` (ffmpeg + ffprobe) when present — which
is what makes the published build need nothing installed. Hidden imports cover
everything PyInstaller's static analysis can't see:
`webview.platforms.edgechromium`, `faster_whisper`, `ctranslate2`, `cv2`,
`google.genai`, `yt_dlp`, and the uvicorn loop/protocol/lifespan modules.
`torch`, `matplotlib`, `tkinter` and `pytest` are excluded to keep size down
(229 MB with ffmpeg, 153 MB without).

`desktop.py` solves four problems specific to being a windowed app.

**1. No stdout.** Launched from Explorer there's no console, so PyInstaller
leaves `sys.stdout`/`sys.stderr` as `None`. `print()` tolerates that; libraries
reaching for a stream do not — uvicorn's log config calls `sys.stdout.isatty()`
and reports the resulting `AttributeError` as *"Unable to configure formatter
'default'"*, killing the engine thread before it could bind a port. The symptom
was an icon that spun and then did nothing.

`_attach_streams()` points both at `app.log`. **Runs with redirected output
already have real streams, which is exactly why this never showed up in
testing** — so packaged-app bugs must be reproduced *unredirected*.

**2. Nowhere to write.** A packaged app must not write clips next to the `.exe`
(often Program Files, often read-only), so a frozen build `chdir`s to
`~/Videos/StreamToShorts` and creates its subfolders up front.

**3. Double-clicking.** A slow start looks exactly like a dead one, so the
natural reaction is to click the icon again — and four copies each unpacking
themselves and loading the same models is how a slow start becomes a *failed*
one.

```python
LOCK_PORT = 50507
s.bind(("127.0.0.1", LOCK_PORT))   # SO_REUSEADDR deliberately NOT set
```

Binding a fixed port and holding it for the process lifetime is the mutex.
Windows refuses the second bind, and that refusal *is* the lock.

**4. Cold-start time.** A first launch pays for importing faster-whisper,
ctranslate2, cv2 and the Google client off disk while the virus scanner reads
them too. `STARTUP_TIMEOUT_SECONDS` is 300 — but `_wait_until_up()` also watches
the engine thread, so a genuine failure reports in seconds rather than spending
the full timeout on a corpse.

Anything worth printing on the way out also gets a `MessageBoxW`, since stderr
goes nowhere a user will ever look. And the engine thread stashes its exception
in `_engine_error` so `main()` can say *why* rather than reporting a bare
timeout — a daemon thread's traceback goes to a stderr the windowed build
doesn't have.

If pywebview is missing or the native window fails, it falls back to the default
browser rather than failing: a working browser window beats no app at all.

**Distribution note:** the `.exe` ships via GitHub Release. Pushing source
changes nothing for people downloading the app, and the onedir and onefile builds
are separate artifacts — one build run refreshes only one of them.

---

## 17. Rough edges and known limits

Everything in this section is true of the code as it stands. An earlier draft of
this document listed four defects it found while being written; those have since
been fixed, and what they were is recorded in [§17.1](#171-fixed-since-this-document-was-written)
because the reasoning is worth keeping.

### The CLI is not the app

`python main.py --mode local` always renders the **face-tracking** layout, never
the stacked one. It writes no SEO metadata, keeps no chunk checkpoints, names
its output `short_NN.mp4`, and ignores `LayoutSpec` entirely.

This is a design split rather than a bug — `pipeline.py` is the original
upstream shape, kept working — but it surprises everyone who reads the README's
feature list and then opens `main.py`. The features described everywhere outside
the Quickstart are *app* features.

### `redirect_stdout` is process-global

`_execute()` wraps the whole pipeline in `contextlib.redirect_stdout(sink)`.
Since only one job runs at a time this is correct, but anything else printing on
another thread during a job — an unrelated request handler, say — lands in that
job's log. Harmless today; the first thing to fix before adding a second worker.

### Face detection is Haar, and OpenCV 5 removed it

Both renderers depend on `cv2.CascadeClassifier`, which 5.x dropped — hence the
`opencv-python>=4.8.0,<5` pin. Haar also means detection is **frontal-face
only**: a streamer in profile, in heavy shadow, or wearing a large headset can
defeat it. That is precisely why the stacked renderer samples six frames and
takes a median rather than trusting any single detection, and why it falls back
to a centre crop out loud instead of silently shipping a bad framing.

### Clip filenames are titles, so they change

Naming a clip after its title ([§9](#9-seo-the-packaging-step)) means the file on
disk is renamed whenever the title is rewritten. That is the intent, but it has
consequences worth knowing: a clip you linked to elsewhere, or opened in an
editor, moves out from under that reference. The job manifest is rewritten in the
same step, so the app itself never loses track.

---

### 17.1 Fixed since this document was written

Kept because the failure modes are instructive.

**Transcripts were cached against the working directory, not the video.** The
cache path came from `LOCAL_OUTPUT_DIR`, which is relative and so resolved
against the cwd, while the video was written under `OUTPUT_ROOT`. Identical by
default; divergent the moment anyone moved their save location — at which point
every transcript was written somewhere the lookup never checked, and each video
silently re-transcribed on every run. The cache now follows the video, still
reads the old locations so nothing is orphaned, and falls back when the video's
folder is read-only.

**Every highlight in a chunk's overlap tail was thrown away.** Each chunk carries
60s of context past its own end so a moment straddling the boundary stays
readable, but it declared its duration as `end - start` regardless. That duration
is the clamp bound sanitisation measures against, so anything the model found in
that tail was clamped to zero length and dropped — silently, because a span
clamped to `start == end` simply disappears. On a 60-minute VOD that was three of
four chunks losing their last minute. Fixed by making the declared duration cover
the tail the chunk actually carries; `PROMPT_VERSION` went to 3 so existing
checkpoints are not resumed onto.

**Clip serving used a string-prefix containment check.** `get_clip` compared
resolved paths with `startswith`, which treats a sibling folder whose name merely
begins with this one's as being inside it. It could not actually be exploited —
`os.path.basename` had already stripped any directory part — but the editor's
`_clip_path` proves containment properly with `relative_to`, and both paths now
use it.

**Two constants named `MAX_CLIP_SECONDS`.** One caps what the ranker may return
(90s), the other the longest span the trim editor will re-cut (300s). The second
is now `MAX_TRIM_SECONDS`.

**Two docs had drifted from the code.** `.env.example` still defaulted
`LLM_PROVIDER` to `openai` when the code says `gemini`, and omitted
`LOCAL_OUTPUT_RESOLUTION`. The README still described clips starting 2–5s
*before* the moment, which `COLD_OPEN_RULES` had reversed.

---

## 18. HTTP API reference

All routes bind `127.0.0.1`. There is no authentication — binding `0.0.0.0`
exposes an unauthenticated service where every job spends the host's API quota
and CPU, and `webapp/__main__.py` prints a warning when you do.

### Setup and settings

| Route | Purpose |
|---|---|
| `GET /api/options` | Aspect ratios, layouts and corners for the UI controls |
| `GET /api/settings` | Whether a key exists, which provider, where it came from, ffmpeg presence, available Gemini models with free-tier limits. **Never returns the key itself** |
| `POST /api/settings` | Save a key / switch provider / pick a model / set a self-imposed daily cap. Gemini models are probe-tested before storing |
| `GET /api/usage` | Today's spend per provider per model, seconds until reset, whether an OpenAI fallback is ready |
| `GET`/`POST /api/locations` | Read or change the save location |
| `GET`/`POST /api/cleanup` | Scan for, then delete, reclaimable source files |
| `POST /api/reveal` | Show a path in the OS file manager |
| `POST /api/open-upload` | Open YouTube's upload page (URL hardcoded, not client-steerable) |

### Creating work

| Route | Purpose |
|---|---|
| `POST /api/resolve` | Classify a pasted link: single video, or a channel to pick from |
| `POST /api/upload` | Accept a dropped video file, return a path to run from |
| `POST /api/layout/preview` | Parse a layout prompt without running anything — powers the live preview |
| `POST /api/jobs` | Enqueue a job. Returns immediately with an id |

### Following work

| Route | Purpose |
|---|---|
| `GET /api/jobs` | All jobs, newest first |
| `GET /api/jobs/{id}` | One job snapshot |
| `GET /api/jobs/{id}/stream` | **SSE** — one event per state change, `: ping` every ~15s, closes on `done`/`error` |
| `GET /api/library` | Every clip still on disk, rescanning the folder first |

### Acting on clips

| Route | Purpose |
|---|---|
| `GET /api/jobs/{id}/clips/{file}` | Stream the mp4 |
| `POST …/clips/{file}/trim` | Re-cut from source at new timestamps, optionally muted |
| `POST …/clips/{file}/save` | Copy out of the working folder into the save location |
| `DELETE …/clips/{file}` | Delete the clip and its file |
| `POST /api/jobs/{id}/seo` | Write or rewrite upload metadata (`?force=true` to overwrite) |
| `POST /api/jobs/{id}/reveal` | Show a clip in the file manager |

---

## 19. Interview cheat-sheet

Short answers to the questions you'll actually be asked.

**"What does it do?"**
Turns a long video into ranked vertical Shorts with upload-ready titles,
descriptions and tags. Downloads with yt-dlp, transcribes locally with
faster-whisper, ranks moments with an LLM, renders with ffmpeg. Runs entirely on
the user's machine and ships as a single Windows .exe.

**"Walk me through the architecture."**
Four decoupled stages — download, transcribe, rank, render — behind a FastAPI
backend with a single-worker job queue. HTTP requests only enqueue; progress
streams back over SSE. A vanilla-JS SPA drives it, wrapped in a pywebview native
window, packaged by PyInstaller.

**"Why a job queue instead of just doing the work in the request?"**
Transcription outlives any sensible HTTP timeout. `POST /api/jobs` enqueues and
returns an id in milliseconds; the browser follows over SSE. One worker, because
Whisper and ffmpeg are both CPU-bound and racing them makes both slower.

**"Why SSE and not WebSockets?"**
Progress is purely server→client. SSE gives that over plain HTTP with browser
auto-reconnect and no protocol upgrade. The client is one line.

**"How does the progress bar know what's happening?"**
It's derived, not guessed. The pipeline narrates itself to stdout; the runner
captures that stream line by line, maps prefixes like `[transcribe]` onto stages,
and gives each stage a band of the bar sized by how long it takes. An `N/M`
counter in a render line moves the bar to the right spot inside the render band
— with zero coupling between pipeline and UI.

**"What was the hardest bug?"**
Chunk timestamp rebasing. Videos over 30 minutes returned *zero* highlights,
with no error. Each chunk was being handed absolute timestamps but clamped
against a relative duration bound, so everything past the first 20 minutes was
silently discarded. The fix: rebase each chunk to zero, keep the offset, add it
back after ranking.

**"How did you make it work on stream footage when generic tools don't?"**
Two things. The renderer searches for the webcam **only in the corner it lives
in**, so a game character's face can't win the crop — and takes the median of six
samples so one bad detection can't skew it. And the ranking prompt teaches the
model to separate streamer speech from game narration by register, then hard-
requires the streamer's own voice in every clip.

**"How do you handle LLM failures?"**
Inverted retry logic: give up immediately only on errors retrying can never fix
(bad key, 404, spent daily quota) and retry everything else five times, honouring
the server's own `retry in Xs` hint. On a spent daily quota, switch to OpenAI
mid-run rather than lose a download and a transcription. Every finished chunk is
checkpointed to disk, so a run that dies resumes where the quota ran out.

**"How do you keep it cheap?"**
Five caches — download, transcript, chunk rankings, job manifest, model list —
each guarding a different expense. Keyword-first prompt parsing that only
consults the LLM when it understood nothing. A one-batch SEO call instead of one
per clip. An exact-span path that skips transcription and ranking entirely. And a
usage ledger so the daily budget is visible *before* it runs out.

**"Why no frontend framework?"**
The UI ships inside a PyInstaller bundle. A build step means a toolchain in the
release pipeline. Three static files copied verbatim eliminates that, and the UI
is one page with about eight interactive regions — well inside what vanilla DOM
handles cleanly.

**"How do you know it works?"**
The two worst bugs in this codebase were both silent — they produced no error,
just fewer results — so the honest answer is that the fix isn't done until the
old behaviour has been reproduced. Both were verified by asserting the broken
output first and the corrected output second, and the library filters were
driven through real typing and clicks in the browser rather than by setting
state directly.

**"What would you do next?"**
Replace the Haar cascade with a modern detector so profile views and headsets
don't defeat it. Add a regression test suite around `_sanitize_highlights`,
`chunk_transcript` and `layout_spec` parse ordering — all three encode hard-won
ordering constraints that a refactor could silently break, and "silently" is the
recurring theme of every real bug found here. And fix the process-global
`redirect_stdout` before ever adding a second worker.

---

## Quick orientation for a new reader

Reading in this order gets you productive fastest:

1. **`webapp/jobs.py::_execute`** — the real pipeline, top to bottom, ~80 lines.
2. **`shorts_generator/highlights.py`** — the prompts and the scoring. The file
   that decides output quality.
3. **`shorts_generator/local/gaming_layout.py`** — the layout that makes stream
   footage survive a vertical crop.
4. **`shorts_generator/layout_spec.py`** — how plain English becomes a render
   spec, and the ordering constraints that keep it honest.

The most valuable knob to turn first is `ACTIVE_VIRALITY_CRITERIA` in
`highlights.py`. Everything else is tuning; that one changes what the app
considers worth clipping at all.
