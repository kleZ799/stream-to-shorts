"""Desktop entry point — Stream to Shorts as an app window, not a website.

Starts the local server on a free port in a background thread, then opens it
in a native window (Edge WebView2 on Windows, WebKit elsewhere) so there is no
address bar, no browser tab, and nothing for the user to "visit".

If a native webview isn't available it falls back to the default browser
rather than failing, because a working browser window beats no app at all.

Run from source:   python desktop.py
Packaged:          StreamToShorts.exe
"""
import os
import socket
import sys
import threading
import time
from contextlib import closing

APP_NAME = "Stream to Shorts"


def _resource_root() -> str:
    """Directory holding bundled data — differs under PyInstaller."""
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def _prepare_environment() -> None:
    """Make the app behave the same whether run from source or a bundle."""
    root = _resource_root()

    # Bundled ffmpeg/ffprobe, if the build shipped them, take priority.
    bundled_bin = os.path.join(root, "bin")
    if os.path.isdir(bundled_bin):
        os.environ["PATH"] = bundled_bin + os.pathsep + os.environ.get("PATH", "")

    # A packaged app must not write clips next to the .exe (often Program
    # Files, often read-only). Work in the user's Videos folder instead.
    if getattr(sys, "frozen", False):
        home = os.path.expanduser("~")
        base = os.path.join(home, "Videos", "StreamToShorts")
        for sub in ("", "output", "webapp_output", "webapp_uploads"):
            os.makedirs(os.path.join(base, sub), exist_ok=True)
        os.chdir(base)
        os.environ.setdefault("LOCAL_OUTPUT_DIR", "output")


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_until_up(port: int, timeout: float = 60.0) -> bool:
    """Block until the server accepts connections, or give up."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
            s.settimeout(0.4)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.2)
    return False


def _serve(port: int) -> None:
    import uvicorn
    from webapp.server import app

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


def main() -> int:
    _prepare_environment()

    port = _free_port()
    url = f"http://127.0.0.1:{port}"

    print(f"[{APP_NAME}] starting engine on {url}", flush=True)
    print(f"[{APP_NAME}] working directory: {os.getcwd()}", flush=True)

    threading.Thread(target=_serve, args=(port,), daemon=True).start()

    if not _wait_until_up(port):
        print("The engine did not start in time. Check the log above.", file=sys.stderr)
        return 1

    try:
        import webview  # type: ignore
    except ImportError:
        webview = None

    if webview is not None:
        try:
            webview.create_window(APP_NAME, url, width=1280, height=900,
                                  min_size=(900, 640))
            # http_server=False: we already run our own server.
            webview.start()
            return 0
        except Exception as e:
            print(f"Native window unavailable ({e}); opening your browser instead.",
                  flush=True)

    import webbrowser
    print(f"\n  {APP_NAME} is running at {url}\n  Close this window to quit.\n", flush=True)
    webbrowser.open(url)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
