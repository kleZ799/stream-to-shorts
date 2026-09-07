"""Find the most viral-worthy highlights in a transcript.

Logic ported from ViralVadoo's transcript_analysis/highlight_generator.py:
  - content-type / density detection
  - chunking for long videos with overlap
  - virality-criteria prompt
  - score-based dedupe with overlap suppression

The LLM call is pluggable via the `llm_fn` argument so the same prompts can
drive either MuAPI (default, --mode api) or a direct local LLM client
(--mode local).

One thing here is not ported and is worth stating plainly, because it is the
difference between a clip that gets shown and one that does not: a highlight is
ranked on where it OPENS, not only on how good its best moment is. Short-form
distribution is decided in the first second - the early drop-off is read as a
verdict on the whole clip - so a moment that needs eight seconds of setup before
it pays off is worth less than a weaker moment that opens cold on its own hook.
The model scores both, and `score` is the blend the rest of the app sorts on.
"""
import json
import re
from pathlib import Path
from typing import Callable, Dict, List, Optional

from . import muapi


LLMFn = Callable[[str], str]


CONTENT_TYPE_PROMPT = """Analyze this video transcript sample and classify the content type.
Choose one: podcast, interview, tutorial, lecture, commentary, debate, vlog, other.
Also estimate content density: low (mostly filler/chit-chat), medium, or high (dense info/stories).
Respond with JSON only: {"content_type": "...", "density": "..."}"""


VIRALITY_CRITERIA = """
Virality signals to prioritize (ranked by impact):
1. HOOK MOMENTS — statements that create immediate curiosity ("The secret is...", "Nobody talks about...", "I was completely wrong about...")
2. EMOTIONAL PEAKS — genuine surprise, laughter, anger, vulnerability, excitement; raw unscripted reactions
3. OPINION BOMBS — strong, polarizing or counter-intuitive statements that trigger agree/disagree
4. REVELATION MOMENTS — surprising facts, stats, or confessions that reframe how the viewer thinks
5. CONFLICT/TENSION — disagreement, pushback, or a problem being confronted head-on
6. QUOTABLE ONE-LINERS — a sentence that works as a standalone quote card
7. STORY PEAKS — the climax or twist of an anecdote; the payoff moment
8. PRACTICAL VALUE — a concrete tip, hack, or insight the viewer can immediately apply
"""


STREAM_VIRALITY_CRITERIA = """
This transcript is a raw LIVE STREAM VOD of someone playing a story-heavy video
game. The audio is a single mixed track: the streamer's microphone AND the
game's own scripted narration/dialogue are transcribed together, with no labels.

Telling them apart:
- GAME NARRATION reads like written prose — literary, past tense, polished, no
  filler words, no self-correction, never addresses anyone directly.
- THE STREAMER sounds spoken — reactions, filler words, false starts, laughter,
  swearing, questions, addressing chat, commenting on what just happened.

HARD REQUIREMENT: every highlight MUST contain the streamer's own speech.
A clip of pure game narration is worthless — the audience follows the streamer,
and that footage belongs to the game, not the channel. A story beat is only
clippable when the streamer reacts to it, talks over it, or responds after it.

Virality signals to prioritize (ranked by impact):
1. REACTION TO A STORY BEAT — the game lands an emotional hit and the streamer
   audibly responds: shock, silence broken by a swear, laughter, genuine sadness
2. RAW REACTION MOMENTS — unscripted spikes of any kind: confusion, rage, delight
3. FAILS & DISASTERS — something goes wrong live and the streamer responds
4. HOT TAKES & RANTS — unfiltered opinions about the game, the story, anything
5. CHAT INTERACTION — answering a question, reacting to a donation or troll
6. PERSONAL TANGENTS — an off-topic story from the streamer's own life
7. QUOTABLE ONE-LINERS — a streamer line that works as a caption or meme
8. SINCERITY — an unguarded, genuinely felt moment of reflection

Hard rules for stream VODs:
- SKIP dead air, loading screens, technical difficulties, and stream housekeeping
- SKIP anything requiring 10 minutes of prior context — it must land for a stranger
- If a span is entirely game narration with no streamer speech, DO NOT return it
"""

COLD_OPEN_RULES = """
HOW A CLIP MUST START - this decides whether it gets shown to anyone at all:

A Short is judged in its first second. Around half of everyone who leaves is
gone inside three seconds, and the platform reads that early drop as "low value"
and stops distributing the clip, no matter how good second 20 is. So the opening
line is not the run-up to the clip. It IS the clip's audition.

- START ON THE HOOK, NOT THE RUN-UP. start_time goes on the first word of the
  most arresting line in the moment. Cut the throat-clear, the "so", the "okay
  so basically", the menu, the walking, the silence before the reaction.
- NO SETUP FIRST. If the interesting thing happens 8 seconds into a span, the
  clip starts at second 8 - not at second 0 with the context first. Either the
  context is implied by the moment, or the moment is not clippable.
- AT MOST ~1 SECOND OF RUNWAY, and only when the payoff is a sound rather than
  a sentence (a laugh, a scream, a gasp), where the instant before it lands is
  what makes the sound read.
- THE FIRST LINE MUST WORK ALONE. Read only the opening sentence, as a stranger
  who has never seen this video and knows nothing about it. If it does not
  create a question, a shock, or a laugh by itself, the clip starts in the wrong
  place - move start_time until it does.
- END ON THE PUNCH. end_time lands just after the payoff, never trailing into
  dead air, a topic change, or "anyway". A clip that ends flat loses the replay.
- A MOMENT THAT NEEDS A PREAMBLE IS NOT A HIGHLIGHT. If it cannot open cold,
  skip it and spend the slot on one that can.
"""


# Which criteria block gets injected into the highlight prompt.
# Swap to VIRALITY_CRITERIA for edited/long-form content.
ACTIVE_VIRALITY_CRITERIA = STREAM_VIRALITY_CRITERIA


HIGHLIGHT_SYSTEM_PROMPT = """You are an elite short-form video editor who has studied thousands of viral clips on TikTok, Instagram Reels, and YouTube Shorts. You know exactly what makes viewers stop scrolling, watch to the end, and share.

{virality_criteria}
{cold_open_rules}
Content type: {content_type} | Density: {density}
{user_brief}
Your task: identify the most viral-worthy highlights from the transcript.

Rules:
- {duration_rule}
- Never cut mid-sentence or mid-thought — each clip must feel complete and self-contained
- Clips must not overlap significantly with each other
- {num_clips_instruction}
- "first_line" is the exact transcript sentence the clip opens on, copied
  verbatim. Write it out before you settle on start_time — if the line you are
  about to copy is filler, setup, or a neutral observation, then the clip starts
  in the wrong place and you must move start_time to a line that hooks.
- "hook_sentence" is that same opening line
- Score each clip TWICE, 0-100, independently:
    "score" — viral potential of the moment as a whole
    "hook_score" — how hard "first_line" ALONE stops a scroll, judged as if you
      cannot see the rest of the clip. Setup, filler or a flat observation
      scores under 40 here however good the payoff is. Be harsh: this is the
      number that decides whether anybody ever reaches the payoff.
- Explain in one sentence why this clip is viral ("virality_reason")

Respond ONLY with valid JSON (no markdown, no explanation):
{{"highlights":[{{"title":"string","start_time":float,"end_time":float,"score":int,"hook_score":int,"first_line":"string","hook_sentence":"string","virality_reason":"string"}}]}}"""


# Bump whenever the ranking prompt -- or the shape of the transcript we hand
# it -- changes meaning. v3 widened each chunk's declared duration to cover its
# overlap tail, so chunks ranked under v2 were asked a narrower question.
PROMPT_VERSION = 3
HOOK_SCORE_WEIGHT = 0.4       # how much the opening line counts toward the rank
MAX_CLIP_SECONDS = 90         # reject anything the model returns above this
CHUNK_SIZE_SECONDS = 1200       # 20-min chunks for long videos
LONG_VIDEO_THRESHOLD = 1800     # chunk videos longer than 30 min
CHUNK_OVERLAP_SECONDS = 60
GPT_CALL_TIMEOUT_SECONDS = 300  # cap LLM polls at 5 min — a wedged call should fail fast
MAX_HIGHLIGHT_API_ATTEMPTS = 3


def call_muapi_llm(prompt: str) -> str:
    """Default LLM backend: MuAPI gpt-5-mini."""
    result = muapi.run(
        "gpt-5-mini",
        {"prompt": prompt},
        label="gpt-5-mini",
        timeout=GPT_CALL_TIMEOUT_SECONDS,
    )

    outputs = result.get("outputs")
    if isinstance(outputs, list) and outputs and isinstance(outputs[0], str) and outputs[0].strip():
        return outputs[0]

    for key in ("output", "text", "response", "result", "content"):
        v = result.get(key)
        if isinstance(v, str) and v.strip():
            return v
        if isinstance(v, dict):
            inner = v.get("text") or v.get("content")
            if isinstance(inner, str) and inner.strip():
                return inner
        if isinstance(v, list) and v and isinstance(v[0], str):
            return v[0]

    raise RuntimeError(f"Could not extract gpt-5-mini text from response: {result}")


def _parse_json_loose(raw: str) -> Dict:
    """gpt-5-4 sometimes wraps JSON in markdown fences — strip and parse."""
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1:
            return json.loads(text[start:end + 1])
        raise


def _coerce_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_int(value: object, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


DEFAULT_DURATION_RULE = (
    "Duration: TARGET 18-35 seconds. Completion rate is the signal that buys "
    "distribution, and the bar is stricter the longer the clip runs — a 20s "
    "clip watched to the end beats a 45s clip watched halfway, every time. "
    "Drop to 10-17s for a single perfect line. Go past 40s only when the payoff "
    "genuinely needs the room, and NEVER exceed 60 seconds"
)


def brief_block(brief: str) -> str:
    """The user's own description of the short they want, if they gave one.

    Placed above the task and marked as outranking the generic criteria: the
    house virality list is a good default, but someone who says "only the
    funny fails" has told us something the list cannot know.
    """
    brief = (brief or "").strip()
    if not brief:
        return ""
    return (
        "\nWHAT THE USER ASKED FOR (outranks the generic criteria above where "
        f"they disagree):\n\"{brief}\"\n"
        "Honour any editorial direction in it — the angle, the mood, the kind "
        "of moment, what the hook should do. Ignore any framing or layout "
        "instructions (webcam position, aspect ratio, clip count); those are "
        "handled elsewhere and are not your concern.\n"
    )


def duration_rule(clip_seconds: Optional[List[float]]) -> str:
    """The length instruction, either the house default or what was asked for."""
    if not clip_seconds or len(clip_seconds) != 2:
        return DEFAULT_DURATION_RULE
    lo, hi = int(clip_seconds[0]), int(clip_seconds[1])
    return (
        f"Duration: every clip MUST run between {lo} and {hi} seconds. This is "
        f"a hard requirement the user asked for by name, not a preference. "
        f"Prefer a moment that is naturally this long over trimming a longer "
        f"one, and never end mid-sentence to hit the number"
    )


def _clip_ceiling(clip_seconds: Optional[List[float]]) -> float:
    """Longest clip to accept back from the model.

    A user who asks for 90-120s clips must not have every one of them thrown
    away by a limit they never saw, so an explicit request raises the ceiling.
    """
    if clip_seconds and len(clip_seconds) == 2:
        return max(float(MAX_CLIP_SECONDS), float(clip_seconds[1]) * 1.2)
    return float(MAX_CLIP_SECONDS)


def _sanitize_highlights(raw_highlights: object, duration: float,
                         clip_seconds: Optional[List[float]] = None) -> List[Dict]:
    """Normalize model output into the expected shape; skip invalid entries."""
    if not isinstance(raw_highlights, list):
        return []
    ceiling = _clip_ceiling(clip_seconds)

    max_end = duration if duration > 0 else float("inf")
    cleaned: List[Dict] = []
    for item in raw_highlights:
        if not isinstance(item, dict):
            continue

        start = _coerce_float(item.get("start_time"), default=-1.0)
        end = _coerce_float(item.get("end_time"), default=-1.0)
        if start < 0 or end <= start:
            continue

        if (end - start) > ceiling:
            continue

        if max_end != float("inf"):
            start = min(start, max_end)
            end = min(end, max_end)
            if end <= start:
                continue

        viral = max(0, min(100, _coerce_int(item.get("score"), default=0)))
        # A model that ignores the field should not be punished for it, so an
        # absent hook_score means "no opinion" rather than zero.
        hook = max(0, min(100, _coerce_int(item.get("hook_score"), default=viral)))

        cleaned.append(
            {
                "title": str(item.get("title") or "Untitled Highlight").strip(),
                "start_time": start,
                "end_time": end,
                # What every caller sorts and cuts on. A brilliant moment behind
                # a flat opening line is not a good Short, because nobody stays
                # long enough to reach it — so the opening line gets a real vote.
                "score": int(round(HOOK_SCORE_WEIGHT * hook
                                   + (1 - HOOK_SCORE_WEIGHT) * viral)),
                "viral_score": viral,
                "hook_score": hook,
                "first_line": str(item.get("first_line") or "").strip(),
                "hook_sentence": str(item.get("hook_sentence")
                                     or item.get("first_line") or "").strip(),
                "virality_reason": str(item.get("virality_reason") or "").strip(),
            }
        )

    return cleaned


def detect_content_type(transcript: Dict, llm_fn: LLMFn = call_muapi_llm) -> Dict[str, str]:
    segments = transcript.get("segments", [])
    sample = " ".join(s["text"] for s in segments[:25])[:3000]
    prompt = f"{CONTENT_TYPE_PROMPT}\n\nTranscript sample:\n{sample}"
    try:
        raw = llm_fn(prompt)
        return _parse_json_loose(raw)
    except Exception:
        return {"content_type": "other", "density": "medium"}


def build_transcript_text(transcript: Dict) -> str:
    segments = transcript.get("segments", [])
    return "\n".join(f"[{s['start']:.1f}s] {s['text'].strip()}" for s in segments)


def chunk_transcript(transcript: Dict) -> List[Dict]:
    segments = transcript.get("segments", [])
    duration = transcript.get("duration", segments[-1]["end"] if segments else 0)
    chunks = []
    start = 0
    while start < duration:
        end = min(start + CHUNK_SIZE_SECONDS, duration)
        # The window carries a tail of extra context past its own end, so a
        # moment straddling the boundary is still readable in full. That tail
        # has to count toward the chunk's declared duration as well: it is the
        # clamp bound _sanitize_highlights measures against, and leaving it at
        # `end - start` threw away every highlight the model found in the last
        # 60 seconds of each chunk -- silently, because clamping a span to
        # start == end just drops it.
        seg_end = min(end + CHUNK_OVERLAP_SECONDS, duration)
        chunk_segs = [
            s for s in segments
            if s["start"] >= start and s["end"] <= seg_end
        ]
        if chunk_segs:
            # Rebase segment times to the chunk so they match the relative
            # duration we pass as the clamp bound; get_highlights adds _offset
            # back afterwards. Without this, chunks after the first hand the
            # model absolute timestamps that then get clamped away entirely.
            chunk = dict(transcript)
            chunk["segments"] = [
                {**seg, "start": seg["start"] - start, "end": seg["end"] - start}
                for seg in chunk_segs
            ]
            chunk["duration"] = seg_end - start
            chunk["_offset"] = start
            chunks.append(chunk)
        start += CHUNK_SIZE_SECONDS - CHUNK_OVERLAP_SECONDS
    return chunks


def call_highlight_api(
    transcript_text: str,
    content_info: Dict,
    duration: float,
    num_clips: int,
    is_chunk: bool = False,
    llm_fn: LLMFn = call_muapi_llm,
    clip_seconds: Optional[List[float]] = None,
    brief: str = "",
) -> Dict:
    # Ask for ~2× the user's target so dedupe has headroom, but cap so the model
    # doesn't have to generate a huge JSON payload (which times out gpt-5-mini).
    target = max(num_clips * 2, 5)
    natural_max = max(2 if is_chunk else 3, int(duration / 90))
    min_clips = min(target, natural_max, 8)
    system = HIGHLIGHT_SYSTEM_PROMPT.format(
        virality_criteria=ACTIVE_VIRALITY_CRITERIA,
        cold_open_rules=COLD_OPEN_RULES,
        content_type=content_info.get("content_type", "other"),
        density=content_info.get("density", "medium"),
        num_clips_instruction=f"Generate at least {min_clips} highlights",
        duration_rule=duration_rule(clip_seconds),
        user_brief=brief_block(brief),
    )
    base_prompt = f"{system}\n\nTranscript:\n{transcript_text}"
    prompt = base_prompt
    last_error = "unknown"

    for attempt in range(1, MAX_HIGHLIGHT_API_ATTEMPTS + 1):
        raw = llm_fn(prompt)
        try:
            parsed = _parse_json_loose(raw)
            highlights = _sanitize_highlights(parsed.get("highlights"), duration=duration,
                                              clip_seconds=clip_seconds)
            if highlights:
                return {"highlights": highlights}
            last_error = "no valid highlights in response"
        except Exception as e:
            last_error = str(e)

        if attempt < MAX_HIGHLIGHT_API_ATTEMPTS:
            print(
                f"[highlights] invalid model output on attempt {attempt}/{MAX_HIGHLIGHT_API_ATTEMPTS}; retrying",
                flush=True,
            )
            prompt = (
                base_prompt
                + "\n\nIMPORTANT: Return ONLY valid JSON with a top-level 'highlights' array."
                + " Each item must include: title, start_time, end_time, score, hook_sentence, virality_reason."
                + " No markdown fences, no commentary."
            )

    raise RuntimeError(
        f"Highlight generator produced invalid output after {MAX_HIGHLIGHT_API_ATTEMPTS} attempts: {last_error}"
    )


def dedupe_highlights(highlights: List[Dict]) -> List[Dict]:
    """Drop a highlight if it overlaps >50% with a higher-scoring one already kept."""
    highlights = sorted(highlights, key=lambda x: int(x.get("score", 0)), reverse=True)
    kept: List[Dict] = []
    for h in highlights:
        h_start = float(h["start_time"])
        h_end = float(h["end_time"])
        h_dur = h_end - h_start
        overlapping = False
        for k in kept:
            latest_start = max(h_start, float(k["start_time"]))
            earliest_end = min(h_end, float(k["end_time"]))
            overlap = earliest_end - latest_start
            if overlap > 0 and overlap > 0.5 * h_dur:
                overlapping = True
                break
        if not overlapping:
            kept.append(h)
    return kept


def _checkpoint_fingerprint(duration: float, chunk_count: int, num_clips: int,
                            clip_seconds: Optional[List[float]] = None) -> str:
    """Identifies the run a saved checkpoint belongs to.

    Includes the requested clip length: asking for 30s clips after a run that
    found 60s ones is a different question, and reusing those answers would
    silently ignore what was asked for. PROMPT_VERSION does the same job across
    releases - chunks ranked by an older prompt are answers to a question we no
    longer ask, and resuming onto them would hide the change from every video
    that has already been through the app once.
    """
    length = "-".join(str(int(x)) for x in clip_seconds) if clip_seconds else "default"
    return f"v{PROMPT_VERSION}|{duration:.0f}|{chunk_count}|{num_clips}|{length}"


def _load_checkpoint(path: Optional[Path], fingerprint: str) -> Dict[str, List[Dict]]:
    """Chunks already ranked on an earlier attempt, keyed by chunk index."""
    if not path or not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    if not isinstance(data, dict) or data.get("fingerprint") != fingerprint:
        # A different video, or the same one asked a different question.
        return {}
    chunks = data.get("chunks")
    return chunks if isinstance(chunks, dict) else {}


def _save_checkpoint(path: Optional[Path], fingerprint: str,
                     chunks: Dict[str, List[Dict]]) -> None:
    if not path:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"fingerprint": fingerprint, "chunks": chunks}, indent=2),
            encoding="utf-8",
        )
    except OSError:
        # Losing the ability to resume is not a reason to fail the run.
        pass


def get_highlights(
    transcript: Dict,
    num_clips: int = 3,
    llm_fn: Optional[LLMFn] = None,
    checkpoint_path: Optional[Path] = None,
    clip_seconds: Optional[List[float]] = None,
    brief: str = "",
) -> Dict:
    """Main entry point — returns {highlights: [...]} sorted by score.

    `llm_fn` swaps the underlying LLM. Defaults to MuAPI gpt-5-mini; local
    mode passes in a local LLM-backed callable.

    `checkpoint_path` makes a long video resumable. Each chunk costs an API
    request, and a nine-chunk video that dies on chunk three used to throw
    away the two it had already paid for -- so every finished chunk is written
    out, and a later attempt picks up where the quota ran out.
    """
    llm_fn = llm_fn or call_muapi_llm
    duration = transcript.get("duration", 0)
    content_info = detect_content_type(transcript, llm_fn=llm_fn)
    print(f"[highlights] content={content_info.get('content_type')} density={content_info.get('density')} duration={duration:.0f}s", flush=True)

    if duration >= LONG_VIDEO_THRESHOLD:
        chunks = chunk_transcript(transcript)
        print(f"[highlights] long video — splitting into {len(chunks)} chunks", flush=True)

        fingerprint = _checkpoint_fingerprint(duration, len(chunks), num_clips, clip_seconds)
        done = _load_checkpoint(checkpoint_path, fingerprint)
        if done:
            print(f"[highlights] resuming — {len(done)}/{len(chunks)} chunk(s) "
                  f"already ranked earlier", flush=True)

        all_highlights: List[Dict] = []
        for i, chunk in enumerate(chunks):
            offset = chunk.get("_offset", 0)
            key = str(i)
            if key in done:
                all_highlights.extend(done[key])
                continue

            text = build_transcript_text(chunk)
            print(f"[highlights] chunk {i + 1}/{len(chunks)} (offset {offset:.0f}s)", flush=True)
            result = call_highlight_api(text, content_info, chunk["duration"], num_clips=num_clips, is_chunk=True, llm_fn=llm_fn, clip_seconds=clip_seconds, brief=brief)
            ranked = []
            for h in result.get("highlights", []):
                h["start_time"] = float(h["start_time"]) + offset
                h["end_time"] = float(h["end_time"]) + offset
                ranked.append(h)
            all_highlights.extend(ranked)

            # Written per chunk, not at the end: the whole point is to survive
            # the failure that happens on the *next* one.
            done[key] = ranked
            _save_checkpoint(checkpoint_path, fingerprint, done)

        highlights = dedupe_highlights(all_highlights)
    else:
        text = build_transcript_text(transcript)
        result = call_highlight_api(text, content_info, duration, num_clips=num_clips, llm_fn=llm_fn, clip_seconds=clip_seconds, brief=brief)
        highlights = dedupe_highlights(result.get("highlights", []))

    return {"highlights": highlights}
