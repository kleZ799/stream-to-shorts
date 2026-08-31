"""Local YouTube download via yt-dlp.

Returns a local mp4 path so the rest of the local pipeline can read it
directly off disk.
"""
import os
import re
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from typing import Dict, List, Optional

from ..config import LOCAL_OUTPUT_DIR


def _import_ytdlp():
    try:
        import yt_dlp  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "yt-dlp is required for --mode local. Install it with:\n"
            "    pip install -r requirements-local.txt"
        ) from e
    return yt_dlp


def _format_for(fmt: str) -> str:
    """Map our '720' / '1080' / 'best' shorthand to a yt-dlp format selector.

    'best' takes whatever the source actually offers, which is what you want
    when the clips are meant to come out at the source's real quality.
    """
    fmt = (fmt or "").strip().lower()
    if fmt in ("best", "max", "source", "auto", ""):
        return "bestvideo+bestaudio/best"
    try:
        height = int(fmt)
    except ValueError:
        height = 720
    return (
        f"bestvideo[height<={height}]+bestaudio/"
        f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/"
        f"best[height<={height}]/best"
    )


def _extract_youtube_video_id(source: str) -> Optional[str]:
    """Best-effort extraction of a YouTube video id from a URL."""
    parsed = urlparse(source)
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]

    if host in ("youtu.be", "www.youtu.be"):
        video_id = parsed.path.lstrip("/").split("/", 1)[0]
        return video_id or None

    if "youtube.com" in host:
        if parsed.path.startswith("/watch"):
            qs = parse_qs(parsed.query)
            video_id = qs.get("v", [""])[0]
            return video_id or None
        match = re.search(r"/(?:shorts|embed|live)/([^/?#&]+)", parsed.path)
        if match:
            return match.group(1)

    return None


def _resolve_local_path(source: str) -> Optional[str]:
    """Return a local filesystem path if the input already points at one."""
    parsed = urlparse(source)
    if parsed.scheme == "file":
        raw_path = unquote(parsed.path)
        if parsed.netloc and parsed.netloc not in ("", "localhost"):
            raw_path = f"//{parsed.netloc}{raw_path}"
        candidate = Path(raw_path).expanduser()
        if candidate.exists() and candidate.is_file():
            return str(candidate.resolve())
        raise RuntimeError(f"Local file URL does not exist: {source}")

    if parsed.scheme in ("http", "https"):
        return None

    candidate = Path(source).expanduser()
    if candidate.exists() and candidate.is_file():
        return str(candidate.resolve())

    if any(sep in source for sep in (os.sep, "/")) or source.startswith("~") or source.startswith("."):
        raise RuntimeError(f"Local file path does not exist: {source}")

    return None


def _probe_height(path: str) -> int:
    """Video height of a local file, or 0 if ffprobe can't tell us."""
    import subprocess
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=height", "-of", "csv=p=0", path],
            capture_output=True, text=True, check=True,
        ).stdout.strip().splitlines()[0]
        return int(float(out))
    except Exception:
        return 0


def _existing_download(out_dir: str, video_id: str) -> Optional[str]:
    """Return a cached download path if we already have this YouTube id.

    Prefers the highest-resolution copy on disk, so asking for better quality
    after a low-quality run doesn't silently hand back the old file.
    """
    candidates = []
    for ext in (".mp4", ".mkv", ".webm"):
        for stem in (f"source_{video_id}", f"source_{video_id}_*"):
            import glob
            candidates.extend(glob.glob(os.path.join(out_dir, stem + ext)))

    best, best_h = None, -1
    for path in sorted(set(candidates)):
        h = _probe_height(path)
        if h > best_h:
            best, best_h = path, h
    return best


def _cache_is_good_enough(path: str, fmt: str) -> bool:
    """Does this cached file already meet the requested quality?"""
    have = _probe_height(path)
    if have <= 0:
        return True          # can't tell — don't re-download on a guess
    fmt = (fmt or "").strip().lower()
    if fmt in ("best", "max", "source", "auto", ""):
        # Anything 1440p or above is treated as already-best; below that it is
        # worth checking whether the source offers more.
        return have >= 1440
    try:
        return have >= int(fmt) * 0.95
    except ValueError:
        return True


def is_channel_or_playlist(url: str) -> bool:
    """True if the URL points at a collection rather than a single video."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.netloc or "").lower().replace("www.", "")
    if "youtube.com" not in host and host != "youtu.be":
        return False
    if _extract_youtube_video_id(url):
        return False
    path = parsed.path.rstrip("/")
    if "list=" in (parsed.query or ""):
        return True
    return (
        path.startswith("/@")
        or path.startswith("/channel/")
        or path.startswith("/c/")
        or path.startswith("/user/")
        or path.startswith("/playlist")
        or path.endswith(("/videos", "/streams", "/shorts"))
    )


def list_channel_videos(url: str, limit: int = 12) -> List[Dict]:
    """List recent videos on a channel or playlist without downloading them.

    Uses yt-dlp's flat extraction, so this is a metadata call — it does not
    pull any media. Returns newest-first.
    """
    yt_dlp = _import_ytdlp()

    # Channel roots don't list videos directly; /videos and /streams do.
    parsed = urlparse(url)
    probe_url = url
    path = parsed.path.rstrip("/")
    if (path.startswith("/@") or path.startswith("/channel/")
            or path.startswith("/c/") or path.startswith("/user/")):
        if not path.endswith(("/videos", "/streams", "/shorts")):
            probe_url = f"{url.rstrip('/')}/videos"

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
        "playlistend": limit,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(probe_url, download=False)

    entries = info.get("entries") or []
    videos: List[Dict] = []
    for e in entries[:limit]:
        if not e or e.get("_type") == "playlist":
            continue
        vid = e.get("id")
        if not vid:
            continue
        videos.append({
            "id": vid,
            "title": e.get("title") or "(untitled)",
            "url": e.get("url") if str(e.get("url", "")).startswith("http")
                   else f"https://www.youtube.com/watch?v={vid}",
            "duration": e.get("duration"),
            "thumbnail": (e.get("thumbnails") or [{}])[-1].get("url"),
        })
    return videos


def download_youtube_local(video_url: str, fmt: str = "720", out_dir: Optional[str] = None) -> str:
    """Download a remote URL or return a local file path unchanged."""
    local_path = _resolve_local_path(video_url)
    if local_path:
        print(f"[download/local] using local file: {local_path}", flush=True)
        return local_path

    yt_dlp = _import_ytdlp()
    out_dir = out_dir or LOCAL_OUTPUT_DIR
    os.makedirs(out_dir, exist_ok=True)

    video_id = _extract_youtube_video_id(video_url)
    if video_id:
        cached = _existing_download(out_dir, video_id)
        if cached and _cache_is_good_enough(cached, fmt):
            print(f"[download/local] reusing cached download: {cached} "
                  f"({_probe_height(cached)}p)", flush=True)
            return cached
        if cached:
            print(f"[download/local] cached copy is only {_probe_height(cached)}p — "
                  f"fetching a better one for '{fmt}'", flush=True)

    # Tag the filename with the requested quality so a higher-quality re-fetch
    # sits alongside the old copy instead of overwriting it (and invalidating
    # the transcript cached against it).
    tag = "" if fmt in ("720",) else f"_{(fmt or 'best').strip().lower()}"
    print(f"[download/local] {video_url} @ {fmt} → {out_dir}/", flush=True)
    ydl_opts = {
        "format": _format_for(fmt),
        "outtmpl": os.path.join(out_dir, f"source_%(id)s{tag}.%(ext)s"),
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(video_url, download=True)
        path = ydl.prepare_filename(info)
        # merge_output_format may rename the extension after merge
        if not os.path.exists(path):
            stem, _ = os.path.splitext(path)
            for ext in (".mp4", ".mkv", ".webm"):
                if os.path.exists(stem + ext):
                    path = stem + ext
                    break

    print(f"[download/local] ready: {path}", flush=True)
    return path
