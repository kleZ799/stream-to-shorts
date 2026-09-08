"""Build StreamToShorts.exe with PyInstaller.

    pip install -r requirements-web.txt pyinstaller
    python build_exe.py

Output lands in dist/. Pass --onefile for a single .exe (slower to start,
since it unpacks to a temp dir each launch); the default one-folder build
starts faster and is what you'd zip for a release.

Optional: drop ffmpeg.exe and ffprobe.exe into a ./bin folder before
building and they get bundled, so users don't have to install ffmpeg
themselves. Without them the app still builds and tells the user what's
missing at startup.
"""
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
NAME = "StreamToShorts"


def _sep() -> str:
    # PyInstaller's --add-data separator is platform-specific.
    return ";" if os.name == "nt" else ":"


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the Stream to Shorts executable")
    ap.add_argument("--onefile", action="store_true",
                    help="Single .exe instead of a folder (slower first launch)")
    ap.add_argument("--clean", action="store_true", help="Wipe build/ and dist/ first")
    ap.add_argument("--cuda", dest="cuda", action="store_true", default=None,
                    help="Bundle the CUDA runtime (~2GB). Default for --onedir.")
    ap.add_argument("--no-cuda", dest="cuda", action="store_false",
                    help="Leave the CUDA runtime out. Default for --onefile.")
    args = ap.parse_args()

    # The two builds want opposite answers here, so the default depends on
    # which one is being made -- and saying it out loud beats letting them
    # drift apart by accident.
    #
    # A onedir build is unpacked already, so 2GB of CUDA costs disk and
    # nothing else. A onefile build re-extracts its entire payload to a temp
    # directory on EVERY launch, so the same 2GB is paid, as startup latency,
    # by every user on every run -- including the majority with no NVIDIA card
    # who cannot use it at all.
    use_cuda = (not args.onefile) if args.cuda is None else args.cuda

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller is not installed. Run:\n    pip install pyinstaller",
              file=sys.stderr)
        return 1

    if args.clean:
        for d in ("build", "dist"):
            shutil.rmtree(ROOT / d, ignore_errors=True)
        print(f"cleaned build/ and dist/")

    sep = _sep()
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--name", NAME,
        "--windowed",                      # no console window
        "--onefile" if args.onefile else "--onedir",

        # The UI files are read from disk at runtime, so they must ship.
        "--add-data", f"{ROOT / 'webapp' / 'static'}{sep}webapp/static",

        # The native window. pywebview picks its backend at runtime, so
        # PyInstaller sees none of it without being told.
        "--hidden-import", "webview",
        "--hidden-import", "webview.platforms.edgechromium",
        "--hidden-import", "webview.platforms.winforms",
        "--hidden-import", "clr",
        "--collect-all", "webview",

        # Imported lazily inside functions, so PyInstaller can't see them.
        "--hidden-import", "faster_whisper",
        "--hidden-import", "ctranslate2",
        "--hidden-import", "google.genai",
        "--hidden-import", "openai",
        "--hidden-import", "cv2",
        "--hidden-import", "yt_dlp",
        "--hidden-import", "uvicorn.logging",
        "--hidden-import", "uvicorn.loops.auto",
        "--hidden-import", "uvicorn.protocols.http.auto",
        "--hidden-import", "uvicorn.protocols.websockets.auto",
        "--hidden-import", "uvicorn.lifespan.on",

        # cv2's Haar cascades are data files the face detection loads by path.
        "--collect-data", "cv2",
        "--collect-data", "yt_dlp",

        # Torch is NOT what runs CUDA Whisper -- faster-whisper sits on
        # CTranslate2, which is collected above. Torch adds ~2GB and buys
        # nothing here, so it stays out.
        "--exclude-module", "torch",
        "--exclude-module", "matplotlib",
        "--exclude-module", "tkinter",
        "--exclude-module", "pytest",

        str(ROOT / "desktop.py"),
    ]

    bin_dir = ROOT / "bin"
    if bin_dir.is_dir() and any(bin_dir.iterdir()):
        cmd[-1:-1] = ["--add-data", f"{bin_dir}{sep}bin"]
        print(f"bundling binaries from {bin_dir}")
    else:
        print("no ./bin folder — ffmpeg will need to be on the user's PATH")

    if use_cuda:
        # CTranslate2 resolves cuBLAS and cuDNN through the DLL search path,
        # not by importing them, so PyInstaller never sees them unless told.
        # transcriber.py registers the directory at runtime.
        cmd[-1:-1] = ["--collect-binaries", "nvidia"]
        print("bundling the CUDA runtime — GPU transcription, ~2GB heavier")
    else:
        print("leaving the CUDA runtime out — transcription will run on the CPU")

    # Author and copyright, compiled into the exe's version resource. This is
    # what Properties -> Details shows, so a copy that has travelled away from
    # this repo still says who wrote it and under what licence.
    version_file = ROOT / "version_info.txt"
    if version_file.exists():
        cmd[-1:-1] = ["--version-file", str(version_file)]
    else:
        print("no version_info.txt — the exe will ship with no author metadata")

    icon = ROOT / "assets" / "icon.ico"
    if icon.exists():
        cmd[-1:-1] = ["--icon", str(icon)]

    print("running PyInstaller...\n")
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        return result.returncode

    out = ROOT / "dist" / (f"{NAME}.exe" if args.onefile else NAME)
    print(f"\nBuilt: {out}")
    if out.exists() and out.is_dir():
        size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
        print(f"Folder size: {size / 1e6:.0f} MB — zip this for a release.")
    elif out.exists():
        print(f"Size: {out.stat().st_size / 1e6:.0f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
