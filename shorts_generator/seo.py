"""Per-clip YouTube metadata: the title, description and tags to upload with.

A rendered clip is only half the job. What decides whether a Short gets seen is
the packaging around it - a title that earns the tap, a description that tells
the algorithm what the clip is about, and tags that put it next to the videos
its audience already watches.

Those three are not equal, and the prompt below is built around the order.
Titles and descriptions are the strong ranking signals; the first three
hashtags in the description are what viewers actually see, rendered as links
above the title; tags are a weak supporting signal that a long list only
dilutes. And the title has one job before any of that: it has to NAME the
thing. A title lifted straight out of the transcript reads fine to someone who
watched the stream and is invisible to everyone else, because it contains no
word anyone would ever search or browse for.

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
# A dozen precise tags carry further than a wall of them: tags are a weaker
# signal than the title and description, and padding the list only dilutes it.
MAX_TAGS = 12
HOOK_LIMIT = 60
# Hashtags are the most visible metadata on the upload - the first three render
# as links above the title - and past a handful they read as spam.
MAX_HASHTAGS = 5

# Ask for the whole batch at once. Ten separate calls would take ten times as
# long and give the model no way to keep the titles from repeating each other.
SEO_PROMPT = """You are the packaging strategist for a YouTube Shorts channel that consistently breaks 1M views. You write the title, description and tags that decide whether a Short gets watched or scrolled past.

SOURCE VIDEO
{video_context}

You are given {n} clips cut from that video, already ranked best to worst by viral potential. For EACH clip, write upload-ready metadata.

THE TWO RULES THAT OUTRANK EVERYTHING BELOW:
1. ACCURATE. Every claim in the title and description must be provable from that clip's own transcript, which is given to you. Never promise a reveal the clip does not contain, never name a person, game or number that is not said, never imply a stake the clip does not reach. A title the clip fails to deliver gets swiped in two seconds, and short-form ranking punishes that harder than a boring title ever could.
2. VIRAL. Within what is true, pick the single most arresting framing. The strongest hook is almost always a real specific detail from the clip - the exact number, the exact word, the actual thing that happened - not a vague tease. Specific and true beats sensational and empty every time.

HOW SHORTS ARE ACTUALLY DISTRIBUTED - write for this, not for a search engine:
- The title is read in well under a second, on a phone, next to a video that is already playing. Front-loaded and concrete beats complete and tidy.
- Titles and descriptions outrank tags as ranking signals. The words that name the subject must appear in the title and in the first line of the description, not only in the tag box.
- The FIRST THREE hashtags in the description are shown as clickable links above the title. That makes them the most visible metadata on the whole upload, so they must be the terms this clip should be filed under.
- A hashtag in the TITLE buys nothing and spends characters you need for keywords. Put no hashtags in the title.
- 3-5 hashtags total. A longer list reads as spam, and past 15 every hashtag on the video is ignored outright.
- A few precise tags beat a wall of them. Padding the tag list dilutes it.

REACHING PAST THE AUDIENCE THE CHANNEL ALREADY HAS - this decides how wide a clip travels:
- WRITE FOR A STRANGER. Assume the viewer has never heard of this streamer, is not subscribed, and has never played the game. Insider framing - a nickname, a running joke, "he did it again", "the usual chaos" - is invisible to everyone who is not already watching, and caps the clip at the audience it started with. If the title only lands for a regular, rewrite it.
- PAIR THE NARROW TERM WITH A BROAD ONE. The exact name is what makes a clip findable; the category it belongs to is what makes it recommendable to people who would never search that name. Where the clip supports both, get the specific noun AND the kind of thing it is into the title or the first description line - the game and what genre of moment it is, the person and what they are known for.
- NO TWO CLIPS IN THIS SET MAY LEAD THE SAME WAY. You are writing all {n} at once, they came from one video, and they will be posted to one channel. If every title opens by naming the same game in the same shape, they compete with each other for a single narrow slice of the feed instead of covering several. Vary which structure each one leads with, and vary which true detail it fronts, so the set reaches several different audiences rather than the same one {n} times.
- NEVER WIDEN BY LYING. Broadening is a choice of truer, plainer words - never a bigger claim, never a vaguer one. Rule 1 outranks everything in this block.

TITLE rules:
- Aim for 40-70 characters. Hard limit {title_limit}. It has to land at a glance on a phone
- NAME THE SUBJECT. The game, person, show or topic actually in the clip must appear in the title - it is the one term a human would ever search or browse for, and a title without it is invisible outside the feed. Take the name from the source video's metadata above when the clip itself does not say it.
- Front-load: the first 3-4 words carry the curiosity or the payoff, everything after them is context
- Use ONE of these structures, whichever the clip genuinely supports:
    - Curiosity gap: says enough to raise a question, withholds the answer
    - Contradiction: the outcome fights the expectation the setup created
    - Stakes: names the concrete thing that is about to go wrong or right
    - Reaction framing: names who reacted and how hard
    - Specific detail: the exact number, object or word lifted from the clip
- A transcript line pasted in as a title is NOT a title. It carries no subject, no context and no question. Rewrite it into one of the structures above.
- No ALL CAPS shouting, at most one emoji and only where it earns its place
- No hashtags, no clickbait the clip does not pay off, no "you won't believe" filler

DESCRIPTION rules:
- Line 1: the hook as a full sentence - it is the only line most viewers ever see
- Lines 2-3: what actually happens, in plain words, naming the game / person / topic and the kind of moment it is. This is the text the ranker reads to decide who to show the clip to, so spend it on real nouns, not adjectives
- Then one short call to action. A specific question about THIS clip beats "comment below"
- Then one line of 3-5 hashtags, #shorts first, then the most specific ones for this clip. Lowercase, no spaces inside a hashtag
- Under 500 characters. Plain text, no markdown

TAGS rules:
- 6-12 lowercase tags, most specific first, comma-separated
- Cover three things and then stop: the exact subject (the game, person or place actually in the clip), the niche it sits in, and the format (shorts, stream highlights, gaming clips)
- Include the real names of any people, games, brands or places said in the clip or named in the source metadata
- Nothing invented, no hashes, no duplicates, nothing over 30 characters, no padding

HOOK_TEXT rules:
- The on-screen caption burned over the first 2 seconds, read while the clip is already playing
- Under 8 words, sentence case, no ending punctuation
- It must raise something the next three seconds answer, so scrolling away costs the viewer the answer
- Never a summary of the clip, and never the same sentence as the title

WHY_IT_WORKS rules:
- One sentence for the channel owner: which structure you used and what it is betting on

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
            f"Opens on this line: {h.get('first_line') or h.get('hook_sentence', '')}\n"
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
    """Normalize hashtags, and make sure #shorts leads.

    YouTube renders the first three hashtags in a description as clickable
    links above the title, so their order is not cosmetic - it is the most
    valuable metadata slot on the whole upload. #shorts goes first because it
    is what tells the platform the video belongs in the Shorts feed at all.
    """
    items = raw if isinstance(raw, list) else []
    out, seen = [], set()
    for tag in items:
        tag = "#" + re.sub(r"[^A-Za-z0-9_]", "", str(tag))
        if len(tag) < 3 or tag.lower() in seen:
            continue
        seen.add(tag.lower())
        out.append(tag)
        if len(out) >= MAX_HASHTAGS:
            break

    if "#shorts" not in seen:
        out = ["#shorts"] + out[:MAX_HASHTAGS - 1]
    else:
        out.sort(key=lambda t: t.lower() != "#shorts")
    return out


def _strip_title_hashtags(title: str) -> str:
    """Take the hashtags back out of a title.

    They earn nothing there - the ones that get shown come from the description
    - and they spend characters the title needs for the words someone might
    actually search for.
    """
    title = re.sub(r"#\w+", " ", title)
    return re.sub(r"\s+", " ", title).strip(" -|,")


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
    topic = list(dict.fromkeys(words))[:6]
    source_title = (video_meta or {}).get("title", "")
    # The source listing is the only place the subject's actual name is written
    # down, and naming the subject is most of what makes a clip findable - so
    # even the offline fallback should carry it into the tags.
    source_tags = [str(t) for t in (video_meta or {}).get("tags", [])[:4]]

    return {
        "title": title[:TITLE_LIMIT],
        "description": "\n".join([
            title,
            "",
            f"Clipped from: {source_title}" if source_title else "Full video on the channel.",
            "Follow for more clips like this.",
            "",
            " ".join(hashtags),
        ]),
        "tags": _clean_tags(source_tags + topic
                            + ["shorts", "stream highlights", "gaming clips"], []),
        "hashtags": hashtags,
        "hook_text": (h.get("hook_sentence") or h.get("title") or "")[:HOOK_LIMIT],
        "why_it_works": h.get("virality_reason", ""),
        "generated": False,
    }


def _coerce_entry(item: Dict, h: Dict, video_meta: Optional[Dict]) -> Dict:
    """Force one model entry into shape, filling any gap from the fallback."""
    base = _fallback_for(h, video_meta)

    title = _strip_title_hashtags(str(item.get("title") or ""))[:TITLE_LIMIT]
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
