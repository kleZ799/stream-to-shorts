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
import traceback
from contextlib import closing
from typing import Optional

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


def _wait_until_up(port: int, engine: threading.Thread,
                   timeout: float = 60.0) -> bool:
    """Block until the server accepts connections, or give up.

    Watches the engine thread too: if it died on an import or a bind error
    there is nothing left to wait for, and spending the full timeout on a
    corpse just turns a clear error into a hang.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
            s.settimeout(0.4)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        if not engine.is_alive():
            return False
        time.sleep(0.2)
    return False


# Set by the engine thread when it dies, so main() can say *why* rather than
# reporting a bare timeout. A daemon thread's traceback goes to a stderr the
# windowed build does not have, so it has to be handed back deliberately.
_engine_error: Optional[BaseException] = None


def _serve(port: int) -> None:
    global _engine_error
    try:
        import uvicorn
        from webapp.server import app

        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    except BaseException as e:  # noqa: BLE001 - last chance to report anything
        _engine_error = e
        traceback.print_exc()


def main() -> int:
    _prepare_environment()

    port = _free_port()
    url = f"http://127.0.0.1:{port}"

    print(f"[{APP_NAME}] starting engine on {url}", flush=True)
    print(f"[{APP_NAME}] working directory: {os.getcwd()}", flush=True)

    engine = threading.Thread(target=_serve, args=(port,), daemon=True)
    engine.start()

    if not _wait_until_up(port, engine):
        if _engine_error is not None:
            print(f"The engine could not start: {_engine_error}", file=sys.stderr)
        else:
            print("The engine did not start in time. Check the log above.",
                  file=sys.stderr)
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
