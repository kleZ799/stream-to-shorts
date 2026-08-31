"""Stacked webcam-over-gameplay layout for game-stream VODs.

The stock face-tracking crop in clipper.py slides a vertical window across the
frame to keep a face centred. That works for talking-head and podcast footage,
where the speaker fills the frame. It fails on a game stream: the webcam is a
small corner overlay, so the window either misses it entirely or locks onto a
character's face in the game.

This module builds the layout gaming clips actually use:

    +----------------+
    |    webcam      |  top   — cropped around the detected face
    +----------------+
    |                |
    |   gameplay     |  bottom — centre crop of the game area
    |                |
    +----------------+

The webcam is located per clip by sampling a handful of frames and running face
detection over the corner region, so it survives the overlay being moved or
resized between scenes. Rendering is a single ffmpeg pass — no per-frame Python.
"""
import os
import statistics
import subprocess
import tempfile
from typing import Dict, List, Optional, Tuple

from ..config import LOCAL_OUTPUT_DIR

# Fraction of the output height given to the webcam panel.
CAM_PANEL_FRACTION = 0.42

# How much of the webcam to show around the detected face, as a multiple of the
# face's width. ~5x gives a head-and-shoulders framing.
FACE_CONTEXT_MULTIPLE = 5.0

# Where the face sits vertically inside the cam panel (0 = top, 1 = bottom).
FACE_VERTICAL_ANCHOR = 0.42

# Trim the outer edge of the derived rect so the overlay's own border or
# letterboxing does not show up as a black sliver in the panel.
CAM_EDGE_INSET = 0.07

SAMPLE_COUNT = 6


def _probe_dimensions(source_path: str) -> Tuple[int, int]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", source_path],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    w, h = out.split("x")[:2]
    return int(w), int(h)


def _detect_face(source_path: str, timestamp: float, src_w: int, src_h: int,
                 corner: str = "bottom-left") -> Optional[Tuple[int, int, int]]:
    """Return (center_x, center_y, face_w) for the largest face in the corner."""
    import cv2

    cascade = cv2.CascadeClassifier(
        cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    )
    with tempfile.TemporaryDirectory() as d:
        frame_path = os.path.join(d, "f.png")
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{timestamp:.3f}",
             "-i", source_path, "-frames:v", "1", frame_path],
            check=True,
        )
        img = cv2.imread(frame_path)
    if img is None:
        return None

    h, w = img.shape[:2]
    y_off = h // 2 if corner.startswith("bottom") else 0
    x_off = 0 if corner.endswith("left") else w // 2
    roi = img[y_off:y_off + h // 2, x_off:x_off + w // 2]

    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    faces = cascade.detectMultiScale(gray, scaleFactor=1.05, minNeighbors=6, minSize=(60, 60))
    if len(faces) == 0:
        return None

    x, y, fw, fh = max(faces, key=lambda f: f[2] * f[3])
    return (x + x_off + fw // 2, y + y_off + fh // 2, fw)


def locate_webcam(source_path: str, start: float, end: float,
                  corner: str = "bottom-left",
                  face_context_multiple: float = FACE_CONTEXT_MULTIPLE) -> Optional[Dict[str, int]]:
    """Sample frames across [start, end] and derive a stable webcam crop rect.

    Medians rather than means, so one bad detection (a character's face, a
    frame where the streamer looked away) does not drag the framing off.
    """
    src_w, src_h = _probe_dimensions(source_path)

    step = (end - start) / (SAMPLE_COUNT + 1)
    hits: List[Tuple[int, int, int]] = []
    for i in range(1, SAMPLE_COUNT + 1):
        hit = _detect_face(source_path, start + step * i, src_w, src_h, corner)
        if hit:
            hits.append(hit)

    if not hits:
        return None

    cx = int(statistics.median(h[0] for h in hits))
    cy = int(statistics.median(h[1] for h in hits))
    face_w = int(statistics.median(h[2] for h in hits))

    crop_w = int(face_w * face_context_multiple * (1 - CAM_EDGE_INSET))
    crop_h = int(crop_w * 3 / 4)

    x = cx - crop_w // 2
    y = cy - int(crop_h * FACE_VERTICAL_ANCHOR)

    # Keep the rect inside the frame without changing its size.
    x = max(0, min(src_w - crop_w, x))
    y = max(0, min(src_h - crop_h, y))
    crop_w = min(crop_w, src_w)
    crop_h = min(crop_h, src_h)

    return {
        "x": x - (x % 2), "y": y - (y % 2),
        "w": crop_w - (crop_w % 2), "h": crop_h - (crop_h % 2),
        "detections": len(hits),
    }


def render_stacked_clip(
    source_path: str,
    start: float,
    end: float,
    out_path: str,
    out_w: int = 1080,
    out_h: int = 1920,
    corner: str = "bottom-left",
    cam_panel_fraction: float = CAM_PANEL_FRACTION,
    face_context_multiple: float = FACE_CONTEXT_MULTIPLE,
) -> Dict:
    """Cut [start, end] and render it as webcam-over-gameplay in one ffmpeg pass."""
    src_w, src_h = _probe_dimensions(source_path)
    cam = locate_webcam(source_path, start, end, corner=corner,
                        face_context_multiple=face_context_multiple)

    cam_h = int(out_h * cam_panel_fraction)
    cam_h -= cam_h % 2
    game_h = out_h - cam_h

    # Gameplay: the widest centre crop matching the bottom panel's aspect,
    # nudged away from the corner the webcam occupies.
    game_crop_h = src_h
    game_crop_w = int(game_crop_h * (out_w / game_h))
    game_crop_w = min(game_crop_w - (game_crop_w % 2), src_w)
    game_x = (src_w - game_crop_w) // 2
    if cam and corner.endswith("left"):
        game_x = max(game_x, min(cam["x"] + cam["w"], src_w - game_crop_w))
    game_x -= game_x % 2

    if cam:
        filt = (
            f"[0:v]crop={cam['w']}:{cam['h']}:{cam['x']}:{cam['y']},"
            f"scale={out_w}:{cam_h}:flags=lanczos,setsar=1[cam];"
            f"[0:v]crop={game_crop_w}:{game_crop_h}:{game_x}:0,"
            f"scale={out_w}:{game_h}:flags=lanczos,setsar=1[game];"
            f"[cam][game]vstack=inputs=2[v]"
        )
    else:
        # No webcam found — fall back to a full-height centre crop.
        fb_w = min(int(src_h * (out_w / out_h)), src_w)
        fb_w -= fb_w % 2
        filt = (
            f"[0:v]crop={fb_w}:{src_h}:{(src_w - fb_w) // 2}:0,"
            f"scale={out_w}:{out_h}:flags=lanczos,setsar=1[v]"
        )

    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-i", source_path, "-t", f"{end - start:.3f}",
        "-filter_complex", filt,
        "-map", "[v]", "-map", "0:a:0?",
        "-c:v", "libx264", "-preset", "medium", "-crf", "23", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart",
        out_path,
    ]
    subprocess.run(cmd, check=True)
    return {"cam": cam, "game_x": game_x, "cam_panel_h": cam_h}


def render_stacked_highlights(
    source_path: str,
    highlights: List[Dict],
    out_dir: Optional[str] = None,
    corner: str = "bottom-left",
    out_w: int = 1080,
    out_h: int = 1920,
    cam_panel_fraction: float = CAM_PANEL_FRACTION,
    face_context_multiple: float = FACE_CONTEXT_MULTIPLE,
    name_prefix: str = "short",
) -> List[Dict]:
    out_dir = out_dir or LOCAL_OUTPUT_DIR
    os.makedirs(out_dir, exist_ok=True)
    results: List[Dict] = []
    for i, h in enumerate(highlights, 1):
        out_path = os.path.join(out_dir, f"{name_prefix}_{i:02d}.mp4")
        print(f"[stack] {i}/{len(highlights)}: {h.get('title', '(untitled)')}", flush=True)
        try:
            info = render_stacked_clip(
                source_path, float(h["start_time"]), float(h["end_time"]), out_path,
                corner=corner, out_w=out_w, out_h=out_h,
                cam_panel_fraction=cam_panel_fraction,
                face_context_multiple=face_context_multiple,
            )
            cam = info["cam"]
            print(
                f"[stack] {i} webcam "
                + (f"{cam['w']}x{cam['h']} at ({cam['x']},{cam['y']}) "
                   f"from {cam['detections']}/{SAMPLE_COUNT} samples"
                   if cam else "NOT FOUND — used centre crop"),
                flush=True,
            )
            results.append({**h, "clip_url": out_path, "layout": info})
        except Exception as e:
            print(f"[stack] {i} failed: {e}", flush=True)
            results.append({**h, "clip_url": None, "error": str(e)})
    return results
