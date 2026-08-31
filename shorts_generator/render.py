"""One entry point for every layout.

The repo grew two renderers with different shapes — the face-tracking crop in
local/clipper.py and the stacked webcam layout in local/gaming_layout.py — plus
a plain centre crop that had no home. This dispatches a LayoutSpec to whichever
one it asks for, so callers (CLI, web UI) never branch on layout themselves.
"""
import os
import subprocess
from typing import Dict, List, Optional

from .layout_spec import LayoutSpec


def _render_center_clip(source_path: str, start: float, end: float, out_path: str,
                        out_w: int, out_h: int) -> Dict:
    """Widest centre crop at the target ratio, in one ffmpeg pass.

    No face detection at all — for footage where the webcam is irrelevant or
    the user explicitly asked for gameplay only.
    """
    from .local.gaming_layout import _probe_dimensions

    src_w, src_h = _probe_dimensions(source_path)
    target = out_w / out_h

    if target < src_w / src_h:
        crop_h = src_h
        crop_w = int(crop_h * target)
    else:
        crop_w = src_w
        crop_h = int(crop_w / target)
    crop_w = min(crop_w - (crop_w % 2), src_w)
    crop_h = min(crop_h - (crop_h % 2), src_h)
    x = ((src_w - crop_w) // 2) & ~1
    y = ((src_h - crop_h) // 2) & ~1

    filt = (
        f"[0:v]crop={crop_w}:{crop_h}:{x}:{y},"
        f"scale={out_w}:{out_h}:flags=lanczos,setsar=1[v]"
    )
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-i", source_path, "-t", f"{end - start:.3f}",
        "-filter_complex", filt,
        "-map", "[v]", "-map", "0:a:0?",
        "-c:v", "libx264", "-preset", "medium", "-crf", "23", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart",
        out_path,
    ], check=True)
    return {"crop": {"x": x, "y": y, "w": crop_w, "h": crop_h}}


def _render_center_highlights(source_path: str, highlights: List[Dict], out_dir: str,
                              out_w: int, out_h: int, name_prefix: str = "short") -> List[Dict]:
    os.makedirs(out_dir, exist_ok=True)
    results: List[Dict] = []
    for i, h in enumerate(highlights, 1):
        out_path = os.path.join(out_dir, f"{name_prefix}_{i:02d}.mp4")
        print(f"[center] {i}/{len(highlights)}: {h.get('title', '(untitled)')}", flush=True)
        try:
            info = _render_center_clip(
                source_path, float(h["start_time"]), float(h["end_time"]),
                out_path, out_w, out_h,
            )
            results.append({**h, "clip_url": out_path, "layout": info})
        except Exception as e:
            print(f"[center] {i} failed: {e}", flush=True)
            results.append({**h, "clip_url": None, "error": str(e)})
    return results


def render_highlights(
    source_path: str,
    highlights: List[Dict],
    spec: LayoutSpec,
    out_dir: Optional[str] = None,
    name_prefix: str = "short",
) -> List[Dict]:
    """Render `highlights` from `source_path` according to `spec`.

    Returns the highlights with `clip_url` set (or `error` on failure) —
    the same shape every renderer in this repo already produces.
    """
    from .config import LOCAL_OUTPUT_DIR

    out_dir = out_dir or LOCAL_OUTPUT_DIR
    os.makedirs(out_dir, exist_ok=True)
    print(f"[render] {spec.layout} · {spec.describe()}", flush=True)

    if spec.layout == "stacked":
        from .local.gaming_layout import render_stacked_highlights
        return render_stacked_highlights(
            source_path, highlights, out_dir=out_dir,
            corner=spec.webcam_corner,
            out_w=spec.width, out_h=spec.height,
            cam_panel_fraction=spec.cam_panel_fraction,
            face_context_multiple=spec.face_zoom,
            name_prefix=name_prefix,
        )

    if spec.layout == "facetrack":
        from .local.clipper import crop_highlights_local
        return crop_highlights_local(
            source_path, highlights,
            aspect_ratio=spec.aspect_ratio,
            out_dir=out_dir,
            target_resolution=f"{spec.width}x{spec.height}",
            name_prefix=name_prefix,
        )

    return _render_center_highlights(
        source_path, highlights, out_dir, spec.width, spec.height, name_prefix=name_prefix
    )
