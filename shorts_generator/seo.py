"""Per-clip YouTube metadata: the title, description and tags to upload with.

A rendered clip is only half the job. What decides whether a Short gets seen is
the packaging around it - a title that earns the tap, a description that tells
the algorithm what the clip is about, and tags that put it next to the videos
its audience already watches.

This asks the same LLM that ranked the highlights to write that packaging, in
one call for the whole batch. Two things it must be at once: accurate, because
a title the clip does not deliver on gets swiped away in two seconds and drags
the channel down with it; and viral, because an accurate title nobody taps is
worth nothing either. So the prompt grounds every field in the clip's own
transcript and then asks for the most arresting true framing of it.

It is deliberately best-effort: SEO text sits on top of a clip that already
exists, so every failure path here falls back to something usable rather than
sinking a run that has already paid for a download, a transcription and a
render.
"""
import json
import re
from typing import Callable, Dict, List, Optional

LLMFn = Callable[[str], str]

# YouTube's own limits, minus a little headroom so a stray character can't
# push a field over and get it rejected at upload time.
TITLE_LIMIT = 100
DESCRIPTION_LIMIT = 4800
TAGS_TOTAL_LIMIT = 460          # the real cap is 500 across all tags
TAG_LIMIT = 30                  # characters per tag
MAX_TAGS = 25
HOOK_LIMIT = 60

# Ask for the whole batch at once. Ten separate calls would take ten times as
# long and give the model no way to keep the titles from repeating each other.
SEO_PROMPT = """You are the packaging strategist for a YouTube Shorts channel that consistently breaks 1M views. You write the title, description and tags that decide whether a Short gets watched or scrolled past.

SOURCE VIDEO
{video_context}

You are given {n} clips cut from that video, already ranked best to worst by viral potential. For EACH clip, write upload-ready metadata.

THE TWO RULES THAT OUTRANK EVERYTHING BELOW:
1. ACCURATE. Every claim in the title and description must be provable from that clip's own transcript, which is given to you. Never promise a reveal the clip does not contain, never name a person, game or number that is not said, never imply a stake the clip does not reach. A title the clip fails to deliver gets swiped in two seconds, and short-form ranking punishes that harder than a boring title ever could.
2. VIRAL. Within what is true, pick the single most arresting framing. The strongest hook is almost always a real specific detail from the clip - the exact number, the exact word, the actual thing that happened - not a vague tease. Specific and true beats sensational and empty every time.

TITLE rules:
- Under {title_limit} characters, and it must land at a glance on a phone
- Front-load the hook: the first 3-4 words carry the curiosity or the payoff
- Prefer the clip's own strongest concrete detail over an abstract summary
- Curiosity gap, bold claim, or a number. Never "you won't believe" filler
- No ALL CAPS shouting, at most one emoji and only where it earns its place
- End with #shorts only when the title still reads naturally with it

DESCRIPTION rules:
- First line repeats the hook - it is the only line most viewers ever see
- Then 1-2 short lines of real context so the topic is unmistakable to the ranker
- Name the actual subject matter in plain words; this is what the algorithm reads
- Then a light call to action (follow / full video / comment prompt)
- Then 3-5 hashtags on their own line
- Under 700 characters. Plain text, no markdown

TAGS rules:
- 15-25 lowercase tags, most specific first, comma-separated
- Mix three kinds: the exact topic, the broader niche, and the format (shorts, clip, podcast clip, stream highlight)
- Include the real names of any people, games, brands or places actually said in the clip
- Nothing invented, no hashes, no duplicates, nothing over 30 characters

HOOK_TEXT rules:
- The on-screen caption to burn over the first 2 seconds
- Under 8 words, sentence case, no ending punctuation
- Built from what is actually about to happen in the clip, so the payoff lands
- It should make stopping the scroll feel involuntary

CLIPS
{clips_block}

Respond with ONLY valid JSON, no markdown fences:
{{"clips":[{{"index":int,"title":"string","description":"string","tags":["string"],"hashtags":["string"],"hook_text":"string","why_it_works":"string"}}]}}"""


def _clip_transcript(transcript: Optional[Dict], start: float, end: float,
                     limit: int = 1600) -> str:
    """The spoken words inside a clip's span.

    This is what keeps the metadata honest - the model writes about the words
    in front of it rather than about the video in general.
    """
    if not transcript:
        return ""
    parts = []
    for seg in transcript.get("segments", []):
        try:
            s, e = float(seg["start"]), float(seg["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if e < start or s > end:
            continue
        text = str(seg.get("text", "")).strip()
        if text:
            parts.append(text)
    return " ".join(parts)[:limit]


def describe_video(meta: Optional[Dict], source: str = "") -> str:
    """Context block about the source video, straight from its own listing."""
    meta = meta or {}
    lines = []
    if meta.get("title"):
        lines.append(f"Title: {meta['title']}")
    if meta.get("uploader"):
        lines.append(f"Channel: {meta['uploader']}")
    if meta.get("categories"):
        lines.append(f"Category: {', '.join(str(c) for c in meta['categories'][:3])}")
    if meta.get("tags"):
        lines.append("The full video's own tags: "
                     + ", ".join(str(t) for t in meta["tags"][:20]))
    if meta.get("description"):
        lines.append(f"Description: {str(meta['description'])[:600]}")
    link = meta.get("webpage_url") or source
    if link and str(link).startswith("http"):
        lines.append(f"Link: {link}")
    return "\n".join(lines) or (
        "(No metadata for the source - judge the topic from the clip transcripts alone, "
        "and do not guess at anything outside them.)"
    )


def _build_clips_block(highlights: List[Dict], transcript: Optional[Dict]) -> str:
    blocks = []
    total = len(highlights)
    for i, h in enumerate(highlights, 1):
        start = float(h.get("start_time", 0) or 0)
        end = float(h.get("end_time", 0) or 0)
        # A clip restored from disk carries its own transcript instead of a
        # span into the source's — either way the model sees real words.
        said = str(h.get("transcript_text") or "").strip()[:1600]
        said = said or _clip_transcript(transcript, start, end) or (
            "(no transcript for this span - keep the metadata generic "
            "rather than inventing detail)"
        )
        blocks.append(
            f"--- CLIP {i} (rank {i} of {total}, "
            f"viral score {h.get('score', 'n/a')}, {max(0.0, end - start):.0f}s)\n"
            f"Working title: {h.get('title', '')}\n"
            f"Hook line: {h.get('hook_sentence', '')}\n"
            f"Why it was picked: {h.get('virality_reason', '')}\n"
            f"WHAT IS ACTUALLY SAID: {said}"
        )
    return "\n\n".join(blocks)


def _parse_json_loose(raw: str) -> Dict:
    text = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1:
            return json.loads(text[start:end + 1])
        raise


def _clean_tags(raw: object, fallback: List[str]) -> List[str]:
    """Normalize a tag list and keep it inside YouTube's 500-character budget."""
    items: List[str] = []
    if isinstance(raw, str):
        items = raw.split(",")
    elif isinstance(raw, list):
        items = [str(t) for t in raw]

    seen, out, total = set(), [], 0
    for tag in (items or fallback):
        tag = re.sub(r"[#,\n]", " ", str(tag)).strip().lower()
        tag = re.sub(r"\s+", " ", tag)[:TAG_LIMIT].strip()
        if not tag or tag in seen:
            continue
        # +1 for the separating comma, which counts against the limit too.
        if total + len(tag) + 1 > TAGS_TOTAL_LIMIT or len(out) >= MAX_TAGS:
            break
        seen.add(tag)
        out.append(tag)
        total += len(tag) + 1
    return out


def _clean_hashtags(raw: object) -> List[str]:
    items = raw if isinstance(raw, list) else []
    out, seen = [], set()
    for tag in items:
        tag = "#" + re.sub(r"[^A-Za-z0-9_]", "", str(tag))
        if len(tag) < 3 or tag.lower() in seen:
            continue
        seen.add(tag.lower())
        out.append(tag)
        if len(out) >= 5:
            break
    return out


def _fallback_for(h: Dict, video_meta: Optional[Dict]) -> Dict:
    """Metadata derived from the highlight alone, when the LLM can't be reached.

    Weaker than the model's version, but every word of it comes from the clip's
    own hook line, so it stays accurate - and a user can copy and upload it,
    which beats an empty box next to a finished clip.
    """
    title = (h.get("hook_sentence") or h.get("title") or "Watch this").strip()
    title = re.sub(r"\s+", " ", title).rstrip(" .,-")
    if len(title) > TITLE_LIMIT - 8:
        title = title[:TITLE_LIMIT - 11].rstrip() + "..."
    hashtags = ["#shorts", "#clips", "#viral"]

    words = re.findall(r"[a-z]{4,}", f"{title} {h.get('virality_reason', '')}".lower())
    topic = list(dict.fromkeys(words))[:8]
    source_title = (video_meta or {}).get("title", "")

    return {
        "title": f"{title} #shorts"[:TITLE_LIMIT],
        "description": "\n".join([
            title,
            "",
            f"Clipped from: {source_title}" if source_title else "Full video on the channel.",
            "Follow for more clips like this.",
            "",
            " ".join(hashtags),
        ]),
        "tags": _clean_tags(topic + ["shorts", "viral clips", "podcast clip",
                                     "stream highlights", "funny moments"], []),
        "hashtags": hashtags,
        "hook_text": (h.get("hook_sentence") or h.get("title") or "")[:HOOK_LIMIT],
        "why_it_works": h.get("virality_reason", ""),
        "generated": False,
    }


def _coerce_entry(item: Dict, h: Dict, video_meta: Optional[Dict]) -> Dict:
    """Force one model entry into shape, filling any gap from the fallback."""
    base = _fallback_for(h, video_meta)

    title = re.sub(r"\s+", " ", str(item.get("title") or "")).strip()[:TITLE_LIMIT]
    if not title:
        title = base["title"]

    description = str(item.get("description") or "").strip()[:DESCRIPTION_LIMIT]
    hashtags = _clean_hashtags(item.get("hashtags")) or base["hashtags"]
    if not description:
        description = base["description"]
    elif not any(t.lower() in description.lower() for t in hashtags):
        description = f"{description}\n\n{' '.join(hashtags)}"[:DESCRIPTION_LIMIT]

    tags = _clean_tags(item.get("tags"), base["tags"])
    hook = re.sub(r"\s+", " ", str(item.get("hook_text") or "")).strip()[:HOOK_LIMIT]

    return {
        "title": title,
        "description": description,
        "tags": tags or base["tags"],
        "hashtags": hashtags,
        "hook_text": hook or base["hook_text"],
        "why_it_works": str(item.get("why_it_works") or h.get("virality_reason") or "").strip(),
        "generated": True,
    }


def generate_seo(
    highlights: List[Dict],
    transcript: Optional[Dict] = None,
    video_meta: Optional[Dict] = None,
    source: str = "",
    llm_fn: Optional[LLMFn] = None,
    errors: Optional[List[str]] = None,
) -> List[Dict]:
    """Upload metadata for each highlight, in the order given (best first).

    Falling back to filename-derived titles is the right behaviour mid-render —
    a run must not die because the metadata step failed. But the caller has to
    be able to tell the difference, so anything that went wrong is appended to
    `errors` rather than only printed. Each returned entry also carries
    `generated`, saying whether a model actually wrote it.
    """
    if not highlights:
        return []

    if llm_fn is None:
        return [_fallback_for(h, video_meta) for h in highlights]

    prompt = SEO_PROMPT.format(
        n=len(highlights),
        title_limit=TITLE_LIMIT,
        video_context=describe_video(video_meta, source),
        clips_block=_build_clips_block(highlights, transcript),
    )

    by_index: Dict[int, Dict] = {}
    try:
        parsed = _parse_json_loose(llm_fn(prompt))
        for item in parsed.get("clips") or []:
            if not isinstance(item, dict):
                continue
            try:
                idx = int(item.get("index"))
            except (TypeError, ValueError):
                idx = len(by_index) + 1
            by_index[idx] = item
        print(f"[seo] wrote metadata for {len(by_index)}/{len(highlights)} clip(s)",
              flush=True)
    except Exception as e:
        print(f"[seo] could not generate metadata ({e}); falling back to hook-line titles",
              flush=True)
        if errors is not None:
            errors.append(str(e))

    out = []
    for i, h in enumerate(highlights, 1):
        item = by_index.get(i)
        out.append(_coerce_entry(item, h, video_meta) if isinstance(item, dict)
                   else _fallback_for(h, video_meta))
    return out


def attach_seo(highlights: List[Dict], **kwargs) -> List[Dict]:
    """Set a `seo` key on each highlight in place, and return them.

    Renderers copy every key they are handed onto their results, so metadata
    attached here rides along to the UI without anything in between needing to
    know about it.
    """
    for h, seo in zip(highlights, generate_seo(highlights, **kwargs)):
        h["seo"] = seo
    return highlights
