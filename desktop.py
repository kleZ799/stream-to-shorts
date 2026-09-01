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


def _alert(message: str, *, error: bool = True) -> None:
    """Tell the user something, even with no console to tell them through.

    The packaged build is windowed, so stderr goes nowhere a user will ever
    look. Anything worth printing on the way out is worth a dialog box.
    """
    print(message, file=sys.stderr if error else sys.stdout, flush=True)
    if sys.platform != "win32":
        return
    try:
        import ctypes

        MB_ICONERROR, MB_ICONINFORMATION = 0x10, 0x40
        ctypes.windll.user32.MessageBoxW(
            None, message, APP_NAME, MB_ICONERROR if error else MB_ICONINFORMATION
        )
    except Exception:
        pass    # a missing dialog must not become the reason we can't exit


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# A fixed port nobody else is likely to want, bound for the lifetime of the
# process purely as a mutex. Windows refuses the second bind, which is the
# whole trick.
LOCK_PORT = 50507


def _claim_single_instance() -> Optional[socket.socket]:
    """Return a held socket, or None if this app is already running.

    A slow start looks exactly like a dead one, so the natural reaction is to
    click the icon again. Four copies each unpacking themselves and loading
    the same models is how a slow start becomes a failed one -- so only the
    first copy gets to run. SO_REUSEADDR is deliberately not set: the default
    refusal is the lock.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
        s.listen(1)
    except OSError:
        s.close()
        return None
    return s


# A cold start pays for imports of faster-whisper, ctranslate2, cv2 and the
# Google client, read off disk while the virus scanner reads them too. Sixty
# seconds was enough warm and not enough cold, which showed up as a taskbar
# click that spun and then did nothing at all. The engine thread is watched
# separately, so a genuine failure still reports in seconds -- this ceiling
# only has to outlast a slow machine having a bad morning.
STARTUP_TIMEOUT_SECONDS = 300.0


def _wait_until_up(port: int, engine: threading.Thread,
                   timeout: float = STARTUP_TIMEOUT_SECONDS) -> bool:
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

    lock = _claim_single_instance()
    if lock is None:
        _alert(f"{APP_NAME} is already running.\n\n"
               f"Look for its window — if you just clicked the icon, the "
               f"first start can take a moment.", error=False)
        return 0

    port = _free_port()
    url = f"http://127.0.0.1:{port}"

    print(f"[{APP_NAME}] starting engine on {url}", flush=True)
    print(f"[{APP_NAME}] working directory: {os.getcwd()}", flush=True)

    engine = threading.Thread(target=_serve, args=(port,), daemon=True)
    engine.start()

    if not _wait_until_up(port, engine):
        if _engine_error is not None:
            _alert(f"{APP_NAME} could not start its engine:\n\n{_engine_error}")
        else:
            _alert(f"{APP_NAME}'s engine did not start in time.\n\n"
                   f"Try launching it again — the first start after a reboot "
                   f"is the slowest.")
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
