"""Natural-language layout control.

Turns a free-text instruction ("webcam bigger, top right, make it square")
into a validated LayoutSpec the renderers understand.

Two-stage parsing on purpose:

  1. A deterministic keyword pass that handles the phrasings people actually
     type. Costs nothing, works offline, and never burns LLM quota.
  2. An optional LLM pass for anything the keyword pass didn't resolve.

The keyword pass runs first and its results win, so a prompt like
"square, webcam top right" never needs a network call at all. The LLM is
only consulted when the prompt said something the keywords missed.
"""
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional
import json
import re

# aspect ratio -> (width, height). Values are the platform-native upload sizes.
ASPECT_PRESETS: Dict[str, tuple] = {
    "9:16": (1080, 1920),   # Shorts / Reels / TikTok
    "4:5": (1080, 1350),    # Instagram feed portrait
    "1:1": (1080, 1080),    # square
    "16:9": (1920, 1080),   # landscape
}

LAYOUTS = ("stacked", "facetrack", "center")
CORNERS = ("bottom-left", "bottom-right", "top-left", "top-right")


@dataclass
class LayoutSpec:
    """Everything the renderer needs to know about framing."""

    layout: str = "stacked"
    aspect_ratio: str = "9:16"
    webcam_corner: str = "bottom-left"
    cam_panel_fraction: float = 0.42
    face_zoom: float = 5.0
    num_clips: int = 5
    # Explicit [start, end] spans in seconds. When present, the pipeline cuts
    # exactly these and skips transcription and AI ranking entirely.
    time_ranges: List[List[float]] = field(default_factory=list)
    # Human-readable notes about what the parser understood, shown in the UI
    # so the user can see their prompt was actually applied.
    notes: List[str] = field(default_factory=list)

    @property
    def width(self) -> int:
        return ASPECT_PRESETS.get(self.aspect_ratio, ASPECT_PRESETS["9:16"])[0]

    @property
    def height(self) -> int:
        return ASPECT_PRESETS.get(self.aspect_ratio, ASPECT_PRESETS["9:16"])[1]

    def validate(self) -> "LayoutSpec":
        """Clamp every field into a renderable range. Never raises."""
        if self.layout not in LAYOUTS:
            self.layout = "stacked"
        if self.aspect_ratio not in ASPECT_PRESETS:
            self.aspect_ratio = "9:16"
        if self.webcam_corner not in CORNERS:
            self.webcam_corner = "bottom-left"
        # Below ~0.15 the webcam is unreadable; above ~0.75 there's no gameplay left.
        self.cam_panel_fraction = max(0.15, min(0.75, float(self.cam_panel_fraction)))
        # Below 2x the crop is inside the face; above 12x the webcam is a speck.
        self.face_zoom = max(2.0, min(12.0, float(self.face_zoom)))
        self.num_clips = max(1, min(10, int(self.num_clips)))

        # Keep only sane, ordered, non-duplicate spans.
        clean: List[List[float]] = []
        for r in self.time_ranges or []:
            try:
                start, end = float(r[0]), float(r[1])
            except (TypeError, ValueError, IndexError):
                continue
            if end <= start or start < 0:
                continue
            if [start, end] not in clean:
                clean.append([start, end])
        self.time_ranges = sorted(clean, key=lambda r: r[0])
        return self

    def to_dict(self) -> Dict:
        d = asdict(self)
        d["width"] = self.width
        d["height"] = self.height
        return d

    def warning(self) -> Optional[str]:
        """A caveat worth showing before the user commits to a long render."""
        if self.layout == "facetrack":
            return (
                "Heads up: this mode inspects every single frame, so it renders "
                "roughly 20× slower than the other two — minutes per clip on a "
                "high-resolution source. Use it for talking-head footage; for "
                "streams, 'webcam on top' looks better and finishes far sooner."
            )
        return None

    def describe(self) -> str:
        """One-line human summary, for the UI and the logs."""
        prefix = ""
        if self.time_ranges:
            n = len(self.time_ranges)
            prefix = f"{n} exact cut{'s' if n > 1 else ''} · "
        return prefix + self._describe_layout()

    def _describe_layout(self) -> str:
        if self.layout == "stacked":
            pct = round(self.cam_panel_fraction * 100)
            where = self.webcam_corner.replace("-", " ")
            return (
                f"{self.aspect_ratio} · webcam on top at {pct}% height "
                f"(overlay found in the {where}) · gameplay below"
            )
        if self.layout == "facetrack":
            return f"{self.aspect_ratio} · single frame, crop follows the face"
        return f"{self.aspect_ratio} · single frame, centre crop"


# --- stage 1: deterministic keyword parsing -------------------------------

# Order matters: explicit ratios and unambiguous words are matched before the
# loose platform names. "shorts" means 9:16 in "make it for shorts" but means
# "clips" in "5 shorts" — the clip-count phrase is stripped before we look.
_ASPECT_WORDS = [
    (r"\b16[:\s/]?9\b|\blandscape\b|\bhorizontal\b|\bwidescreen\b", "16:9"),
    (r"\b4[:\s/]?5\b|\binstagram feed\b|\bfeed post\b", "4:5"),
    (r"\b1[:\s/]?1\b|\bsquare\b", "1:1"),
    (r"\b9[:\s/]?16\b|\bvertical\b|\bportrait\b|\breels?\b|\btiktok\b|\bshorts?\b", "9:16"),
]

_CORNER_WORDS = [
    (r"bottom[\s-]*left|lower[\s-]*left", "bottom-left"),
    (r"bottom[\s-]*right|lower[\s-]*right", "bottom-right"),
    (r"top[\s-]*left|upper[\s-]*left", "top-left"),
    (r"top[\s-]*right|upper[\s-]*right", "top-right"),
]


def _to_seconds(token: str) -> Optional[float]:
    """'1:30' -> 90, '01:02:03' -> 3723, '90s' -> 90, '90' -> 90."""
    token = re.sub(r"(?:seconds|secs|sec|s)\s*$", "", token.strip()).strip()
    if not token:
        return None
    if ":" in token:
        parts = token.split(":")
        if len(parts) > 3 or any(not p.strip().isdigit() for p in parts):
            return None
        nums = [int(p) for p in parts]
        while len(nums) < 3:
            nums.insert(0, 0)
        h, m, s = nums
        if m > 59 or s > 59:
            return None
        return h * 3600 + m * 60 + s
    try:
        return float(token)
    except ValueError:
        return None


# A timestamp is either clock form (1:30, 00:01:30) or an explicit seconds
# value (90s). Bare numbers are only accepted inside "from X to Y", because
# on their own they collide with everything from percentages to resolutions.
# The trailing \b matters: without it "928 square" parses as "928 s" and eats
# the leading letter of the next word.
_TS = r"(?:\d{1,2}:\d{2}(?::\d{2})?|\d{1,5}\s*(?:s|sec|secs|seconds)\b)"

_RANGE_PATTERNS = [
    # "from 1:30 to 2:45" / "between 90s and 150s" — explicit, so bare numbers are safe here
    rf"\b(?:from|between)\s+({_TS}|\d{{1,5}})\s*(?:to|-|–|—|and|until)\s*({_TS}|\d{{1,5}})",
    # "1:30-2:45", "1:30 to 2:45", "90s - 150s"
    rf"({_TS})\s*(?:to|-|–|—|until)\s*({_TS})",
]


def _parse_time_ranges(p: str, spec: LayoutSpec) -> tuple:
    """Pull explicit clip spans out of the prompt.

    Returns (remaining_text, found) — the matched spans are stripped so the
    rest of the parser can't misread "9:16" style leftovers.
    """
    found = []
    for pattern in _RANGE_PATTERNS:
        for m in list(re.finditer(pattern, p)):
            a, b = _to_seconds(m.group(1)), _to_seconds(m.group(2))
            if a is None or b is None or b <= a:
                continue
            found.append([a, b])
            p = p[:m.start()] + " " + p[m.end():]
        if found:
            break

    if found:
        spec.time_ranges = found
        for a, b in found:
            spec.notes.append(
                f"exact cut → {_fmt_ts(a)} to {_fmt_ts(b)} ({b - a:.0f}s)"
            )
    return p, bool(found)


def _fmt_ts(seconds: float) -> str:
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _parse_keywords(prompt: str, spec: LayoutSpec) -> set:
    """Apply what we can read directly. Returns the set of fields we resolved."""
    p = prompt.lower()
    resolved = set()

    # Time ranges first: they're stripped from the text so a span like
    # "1:30-2:45" can never be mistaken for an aspect ratio later.
    p, had_ranges = _parse_time_ranges(p, spec)
    if had_ranges:
        resolved.add("time_ranges")

    # Clip count first, and strip the phrase so "5 shorts" can't also be read
    # as a request for 9:16.
    m = re.search(r"\b(\d{1,2})\s*(clips?|shorts?|videos?)\b", p)
    if m:
        spec.num_clips = int(m.group(1))
        spec.notes.append(f"clip count → {spec.num_clips}")
        resolved.add("num_clips")
        p = p[:m.start()] + " " + p[m.end():]

    for pattern, value in _ASPECT_WORDS:
        if re.search(pattern, p):
            spec.aspect_ratio = value
            spec.notes.append(f"aspect ratio → {value}")
            resolved.add("aspect_ratio")
            break

    # Layout intent. Checked most-specific first.
    if re.search(r"\bno (web)?cam\b|\bwithout (a )?(web)?cam\b|\bhide (the )?(web)?cam\b"
                 r"|\bgameplay only\b|\bjust (the )?gameplay\b|\bno face\b", p):
        spec.layout = "center"
        spec.notes.append("layout → gameplay only, no webcam panel")
        resolved.add("layout")
    elif re.search(r"\bfollow (my |the )?face\b|\bface[\s-]*track\w*\b|\btrack (my |the )?face\b"
                   r"|\bkeep (my |the )?face cent\w+\b|\btalking head\b", p):
        spec.layout = "facetrack"
        spec.notes.append("layout → single frame, crop follows the face")
        resolved.add("layout")
    elif re.search(r"\bstack\w*\b|\b(web)?cam (on |at |to )?(the )?top\b|\btop\b.*\b(web)?cam\b"
                   r"|\b(web)?cam above\b|\bsplit\b|\bpicture[\s-]*in[\s-]*picture\b|\bpip\b", p):
        spec.layout = "stacked"
        spec.notes.append("layout → webcam on top, gameplay below")
        resolved.add("layout")

    # Where the webcam overlay physically sits in the SOURCE footage.
    for pattern, value in _CORNER_WORDS:
        if re.search(pattern, p):
            # "webcam on top" is a layout instruction, not a corner. Only read a
            # corner when the phrasing is actually about locating the overlay.
            if re.search(r"(web)?cam|overlay|face", p):
                spec.webcam_corner = value
                spec.notes.append(f"looking for the webcam overlay in the {value.replace('-', ' ')}")
                resolved.add("webcam_corner")
            break

    # Panel size.
    if re.search(r"\b(bigger|larger|big|huge|more)\b.*\b(web)?cam\b|\b(web)?cam\b.*\b(bigger|larger|big|huge|more)\b", p):
        spec.cam_panel_fraction = 0.55
        spec.notes.append("webcam panel → larger (55% of height)")
        resolved.add("cam_panel_fraction")
    elif re.search(r"\b(smaller|small|tiny|less|shrink)\b.*\b(web)?cam\b|\b(web)?cam\b.*\b(smaller|small|tiny|less|shrink)\b", p):
        spec.cam_panel_fraction = 0.30
        spec.notes.append("webcam panel → smaller (30% of height)")
        resolved.add("cam_panel_fraction")

    # Explicit percentage, e.g. "webcam 60%".
    m = re.search(r"(\d{2})\s*%", p)
    if m:
        spec.cam_panel_fraction = int(m.group(1)) / 100.0
        spec.notes.append(f"webcam panel → {m.group(1)}% of height")
        resolved.add("cam_panel_fraction")

    # Face framing tightness.
    if re.search(r"\b(zoom|closer|tighter|tight|close up|closeup)\b", p):
        spec.face_zoom = 3.0
        spec.notes.append("webcam framing → tighter on the face")
        resolved.add("face_zoom")
    elif re.search(r"\b(wider|zoom out|pull back|more room|wide)\b", p):
        spec.face_zoom = 7.5
        spec.notes.append("webcam framing → wider")
        resolved.add("face_zoom")

    return resolved


# --- stage 2: LLM fallback ------------------------------------------------

_LLM_PROMPT = """You convert a video-editing instruction into JSON config.

Fields (omit any the instruction does not mention):
- "layout": "stacked" (webcam panel on top, gameplay below) | "facetrack" (one frame, crop follows the speaker's face) | "center" (one frame, plain centre crop, no webcam)
- "aspect_ratio": "9:16" | "4:5" | "1:1" | "16:9"
- "webcam_corner": where the webcam overlay sits in the ORIGINAL footage: "bottom-left" | "bottom-right" | "top-left" | "top-right"
- "cam_panel_fraction": 0.15-0.75, how much output height the webcam panel gets
- "face_zoom": 2.0-12.0, crop width as a multiple of face width (lower = tighter)
- "num_clips": 1-10

Respond with ONLY a JSON object. No markdown, no explanation.

Instruction: {prompt}"""


def _parse_with_llm(prompt: str, spec: LayoutSpec, already: set) -> None:
    """Ask the configured LLM about whatever the keyword pass didn't catch."""
    from .local.llm import call_local_llm

    raw = call_local_llm(_LLM_PROMPT.format(prompt=prompt))
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON object in LLM reply: {raw[:200]}")
    data = json.loads(text[start:end + 1])

    # Keyword results win — they came from the user's literal words.
    for key in ("layout", "aspect_ratio", "webcam_corner", "cam_panel_fraction",
                "face_zoom", "num_clips"):
        if key in already or key not in data or data[key] is None:
            continue
        setattr(spec, key, data[key])
        spec.notes.append(f"{key.replace('_', ' ')} → {data[key]} (interpreted)")


def parse_layout_prompt(
    prompt: Optional[str],
    base: Optional[LayoutSpec] = None,
    use_llm: bool = True,
) -> LayoutSpec:
    """Build a LayoutSpec from free text. Always returns something renderable.

    Args:
        prompt: free-text instruction. Empty/None returns the defaults.
        base: spec to start from, so UI controls can seed the prompt parse.
        use_llm: consult the LLM for anything keywords missed.
    """
    spec = base or LayoutSpec()
    spec.notes = []

    if not prompt or not prompt.strip():
        spec.notes.append("no layout prompt — using defaults")
        return spec.validate()

    resolved = _parse_keywords(prompt, spec)

    # Only pay for an LLM call when the keyword pass understood nothing at all.
    # Anything it did resolve came from the user's literal words, so a second
    # opinion adds latency (and quota) without adding accuracy — and this runs
    # on every keystroke behind the live preview.
    if use_llm and not resolved:
        try:
            _parse_with_llm(prompt, spec, resolved)
        except Exception as e:
            # A layout prompt is a convenience, never a hard dependency —
            # falling back to defaults beats failing the whole render.
            spec.notes.append(f"could not interpret the rest of the prompt ({e}); kept defaults")

    if not spec.notes:
        spec.notes.append("nothing recognised in the prompt — using defaults")

    return spec.validate()
