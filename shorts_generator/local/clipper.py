"""Local clipping: ffmpeg subclip + OpenCV face-aware vertical crop.

Two stages per highlight:
  1. Cut the source video to [start, end] with ffmpeg (re-encoded, audio kept).
  2. Reframe the cut to the target aspect ratio. For 9:16 we slide a vertical
     window horizontally across the frame to keep faces centred (Haar
     cascade — same approach as the original repo, no external models).
"""
import os
import subprocess
import time
from typing import Dict, List, Optional, Tuple

from ..config import LOCAL_OUTPUT_DIR, LOCAL_OUTPUT_RESOLUTION


def _safe_remove(path: str, attempts: int = 5) -> None:
    """Delete a temp file, tolerating Windows' lazy handle release.

    Cleanup must never raise: on Windows a lingering handle turns a real
    encoding error into a confusing WinError 32 from the finally block.
    """
    for i in range(attempts):
        try:
            if os.path.exists(path):
                os.remove(path)
            return
        except OSError:
            if i == attempts - 1:
                print(f"[clip/local] warning: could not delete temp file {path}", flush=True)
                return
            time.sleep(0.3)


def _ratio(aspect_ratio: str) -> float:
    """Parse '9:16' → 9/16, '1:1' → 1.0."""
    try:
        w, h = aspect_ratio.split(":")
        return float(w) / float(h)
    except (ValueError, ZeroDivisionError):
        return 9.0 / 16.0


def _cut_subclip(source_path: str, start: float, end: float, out_path: str) -> str:
    """ffmpeg -ss start -to end → re-encoded mp4 with audio."""
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", source_path,
        "-ss", f"{start:.3f}",
        "-to", f"{end:.3f}",
        # Intermediate only — the reframe step re-encodes this, so favour speed
        # at near-transparent quality instead of spending time on compression.
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
        "-c:a", "aac", "-b:a", "128k",
        out_path,
    ]
    subprocess.run(cmd, check=True)
    return out_path


def _reframe_vertical(in_path: str, out_path: str, aspect_ratio: str,
                      target_resolution: Optional[str] = None) -> str:
    """Crop the cut clip to the target aspect ratio, tracking faces if possible."""
    try:
        import cv2  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "opencv-python is required for --mode local. Install it with:\n"
            "    pip install -r requirements-local.txt"
        ) from e

    target_ratio = _ratio(aspect_ratio)
    cap = cv2.VideoCapture(in_path)
    if not cap.isOpened():
        raise RuntimeError(f"could not open {in_path}")

    src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0

    # Compute the largest crop that fits inside the frame at the target ratio.
    if target_ratio < src_w / src_h:
        crop_h = src_h
        crop_w = int(crop_h * target_ratio)
    else:
        crop_w = src_w
        crop_h = int(crop_w / target_ratio)
    crop_w = max(2, crop_w - (crop_w % 2))
    crop_h = max(2, crop_h - (crop_h % 2))

    face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")

    silent_path = out_path + ".silent.mp4"
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(silent_path, fourcc, fps, (crop_w, crop_h))
    if not writer.isOpened():
        cap.release()
        raise RuntimeError(
            f"OpenCV could not open a writer for {crop_w}x{crop_h} @ {fps:.0f}fps"
        )

    # Haar detection at full 1440p costs more than the rest of the pipeline
    # combined; detect on a downscaled copy and scale the result back up.
    detect_scale = 640.0 / src_w if src_w > 640 else 1.0

    last_center: Optional[Tuple[int, int]] = None
    smoothing = 0.15  # how aggressively to chase a new face position
    try:
      while True:
          ret, frame = cap.read()
          if not ret:
              break

          gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
          if detect_scale < 1.0:
              gray = cv2.resize(gray, None, fx=detect_scale, fy=detect_scale)
          faces = face_cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(40, 40))
          if len(faces) > 0:
              # Pick the largest face — usually the speaker.
              x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
              cx = int((x + w // 2) / detect_scale)
              cy = int((y + h // 2) / detect_scale)
              if last_center is None:
                  last_center = (cx, cy)
              else:
                  lx, ly = last_center
                  last_center = (
                      int(lx + (cx - lx) * smoothing),
                      int(ly + (cy - ly) * smoothing),
                  )
          if last_center is None:
              last_center = (src_w // 2, src_h // 2)

          cx, cy = last_center
          x0 = max(0, min(src_w - crop_w, cx - crop_w // 2))
          y0 = max(0, min(src_h - crop_h, cy - crop_h // 2))
          # .copy() makes the slice contiguous — OpenCV's writer throws an
          # opaque C++ exception on the non-contiguous view at high resolution.
          cropped = frame[y0:y0 + crop_h, x0:x0 + crop_w].copy()
          writer.write(cropped)
    finally:
        # Windows keeps the file locked until both handles are closed, which
        # would otherwise make the temp-file cleanup fail and mask this error.
        cap.release()
        writer.release()

    # Mux audio from the cut clip back onto the silent reframed video.
    resolution = target_resolution if target_resolution is not None else LOCAL_OUTPUT_RESOLUTION
    scale_args: List[str] = []
    if resolution:
        w, h = resolution.split("x")
        # lanczos preserves detail on the upscale from the native crop size
        scale_args = ["-vf", f"scale={int(w)}:{int(h)}:flags=lanczos"]

    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", silent_path,
        "-i", in_path,
        *scale_args,
        # OpenCV writes mpeg4; re-encode to h264 so the upload is accepted
        # everywhere (Shorts / Reels / TikTok) without a lossy server-side pass.
        "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-map", "0:v:0", "-map", "1:a:0?",
        "-shortest",
        out_path,
    ]
    subprocess.run(cmd, check=True)
    _safe_remove(silent_path)
    return out_path


def crop_clip_local(
    source_path: str,
    start_time: float,
    end_time: float,
    aspect_ratio: str,
    out_path: str,
    target_resolution: Optional[str] = None,
) -> str:
    """Cut + reframe one highlight, returning the local mp4 path."""
    cut_path = out_path + ".cut.mp4"
    try:
        _cut_subclip(source_path, start_time, end_time, cut_path)
        _reframe_vertical(cut_path, out_path, aspect_ratio, target_resolution=target_resolution)
    finally:
        _safe_remove(cut_path)
    return out_path


def crop_highlights_local(
    source_path: str,
    highlights: List[Dict],
    aspect_ratio: str = "9:16",
    out_dir: Optional[str] = None,
    target_resolution: Optional[str] = None,
    name_prefix: str = "short",
) -> List[Dict]:
    out_dir = out_dir or LOCAL_OUTPUT_DIR
    os.makedirs(out_dir, exist_ok=True)
    results: List[Dict] = []
    for i, h in enumerate(highlights, 1):
        out_path = os.path.join(out_dir, f"{name_prefix}_{i:02d}.mp4")
        print(f"[clip/local] {i}/{len(highlights)}: {h.get('title', '(untitled)')}", flush=True)
        try:
            crop_clip_local(
                source_path,
                float(h["start_time"]),
                float(h["end_time"]),
                aspect_ratio,
                out_path,
                target_resolution=target_resolution,
            )
            results.append({**h, "clip_url": out_path})
        except Exception as e:
            print(f"[clip/local] {i} failed: {e}", flush=True)
            results.append({**h, "clip_url": None, "error": str(e)})
    return results
