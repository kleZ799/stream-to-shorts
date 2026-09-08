"""In-place updates for the packaged app.

The app is a single unsigned .exe that people download once and keep. Without
this they have no way to learn a new version exists, short of visiting the
repository on a hunch -- so a fix can ship and never reach the person it was
written for.

How the swap works
------------------
Windows will not let a running .exe be overwritten, but it will happily let it
be *renamed*. So the running file is moved aside rather than deleted, the new
build takes its place, and the app relaunches from the same path. The stale
file is removed on the next start, once nothing has it open.

That ordering matters: the rename happens only after the download is complete
and its hash checked, so a half-downloaded or tampered file never becomes the
thing that runs. If anything fails before that point the running exe has not
been touched at all.

What this deliberately does not do
----------------------------------
It never runs the downloaded file to "install" anything, and it never takes a
URL from the page. The repository is compiled in, the asset URL comes from
GitHub's API response for that repository, and the download must match the
SHA-256 that the API reports. The app is unsigned, so this is integrity
against a corrupted or swapped download, not proof of authorship -- but it is
the strongest check available without a code-signing certificate.

Only the one-file build can update itself. The one-folder build is hundreds of
files, and swapping those under a running process is a different and far more
fragile problem, so it is told to update by hand instead.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

from shorts_generator.version import APP_VERSION, UPDATE_REPO

API_LATEST = f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest"
ASSET_NAME = "StreamToShorts.exe"

# GitHub serves release downloads off its own domains and redirects between
# them. Anything else means the API response was not what we think it was, so
# the download stops rather than following it.
ALLOWED_HOSTS = {
    "api.github.com",
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
}

USER_AGENT = f"StreamToShorts/{APP_VERSION}"
NETWORK_TIMEOUT = 20

# The file the previous version was renamed to, cleaned up on next launch.
BACKUP_SUFFIX = ".old-version"


# --- what kind of build is this -------------------------------------------

def exe_path() -> Optional[Path]:
    """The .exe the user actually launched, or None when run from source."""
    if not getattr(sys, "frozen", False):
        return None
    return Path(sys.executable).resolve()


def is_onefile() -> bool:
    """True for the single-file build.

    PyInstaller unpacks a one-file build to a temp directory, so _MEIPASS sits
    somewhere else entirely. A one-folder build unpacks nowhere -- _MEIPASS is
    the _internal folder sitting next to the exe.
    """
    exe = exe_path()
    meipass = getattr(sys, "_MEIPASS", None)
    if exe is None or not meipass:
        return False
    try:
        return Path(meipass).resolve().parent != exe.parent
    except OSError:
        return False


def can_self_update() -> bool:
    return is_onefile()


def cleanup_previous() -> None:
    """Delete the file the last update renamed aside. Safe to call always."""
    exe = exe_path()
    if exe is None:
        return
    stale = exe.with_name(exe.name + BACKUP_SUFFIX)
    if stale.exists():
        try:
            stale.unlink()
        except OSError:
            # Still held open, or gone already. It costs disk, not correctness,
            # and the next launch gets another go.
            pass


# --- version comparison ---------------------------------------------------

def _parts(v: str) -> tuple:
    """"v1.4.0" -> (1, 4, 0). Unparseable pieces sort as 0 rather than raise."""
    cleaned = (v or "").strip().lstrip("vV").split("+")[0].split("-")[0]
    out = []
    for piece in cleaned.split("."):
        try:
            out.append(int(piece))
        except ValueError:
            out.append(0)
    while len(out) < 3:
        out.append(0)
    return tuple(out[:3])


def compare(latest: str, current: str) -> int:
    """1 if latest is newer, -1 if older, 0 if the same."""
    a, b = _parts(latest), _parts(current)
    return (a > b) - (a < b)


# --- talking to GitHub ----------------------------------------------------

def _request(url: str):
    if urllib.parse.urlsplit(url).hostname not in ALLOWED_HOSTS:
        raise ValueError("Refusing to fetch from an unexpected host.")
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json",
    })
    return urllib.request.urlopen(req, timeout=NETWORK_TIMEOUT)


def check() -> dict:
    """Ask GitHub what the newest release is.

    Never raises for the ordinary reasons a check fails -- offline, rate
    limited, no releases yet. An update check is not worth an error in the
    user's face, so those come back as a quiet 'no update' with a reason.
    """
    state = {
        "current": APP_VERSION,
        "latest": None,
        "status": "current",     # current | update | rollback | unavailable
        "can_install": False,
        "size": 0,
        "notes_url": f"https://github.com/{UPDATE_REPO}/releases/latest",
        "reason": "",
    }
    try:
        with _request(API_LATEST) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        state["reason"] = ("No releases published yet." if e.code == 404
                           else f"GitHub returned {e.code}.")
        return state
    except Exception:
        state["reason"] = "Could not reach GitHub."
        return state

    tag = (data.get("tag_name") or "").strip()
    if not tag:
        state["reason"] = "That release has no version tag."
        return state
    state["latest"] = tag.lstrip("vV")

    asset = next((a for a in data.get("assets", [])
                  if a.get("name") == ASSET_NAME), None)
    if asset is None:
        state["reason"] = f"That release has no {ASSET_NAME} attached."
        return state

    state["size"] = int(asset.get("size") or 0)
    state["_url"] = asset.get("browser_download_url") or ""
    state["_digest"] = (asset.get("digest") or "")

    cmp = compare(state["latest"], APP_VERSION)
    if cmp > 0:
        state["status"] = "update"
    elif cmp < 0:
        # Happens when a release is pulled or an older build is re-published.
        # Offered rather than hidden, because that is how a rollback reaches
        # the people who already installed the version being withdrawn.
        state["status"] = "rollback"
    else:
        state["status"] = "current"
        return state

    if not can_self_update():
        state["status"] = "unavailable"
        state["reason"] = (
            "Running from source — update with git, not from here."
            if exe_path() is None else
            "This is the one-folder build, which cannot replace itself. "
            "Download the new one instead."
        )
        return state

    state["can_install"] = bool(state["_url"] and state["_digest"])
    if not state["can_install"]:
        state["reason"] = "That release is missing a download or its checksum."
    return state


# --- doing the update -----------------------------------------------------

class Install:
    """One update attempt, run on a worker thread so the UI can watch it."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.state = "idle"          # idle | downloading | verifying | ready | failed
        self.done = 0
        self.total = 0
        self.error = ""
        self.staged: Optional[Path] = None
        self.thread: Optional[threading.Thread] = None

    def snapshot(self) -> dict:
        with self.lock:
            pct = (self.done / self.total * 100) if self.total else 0
            return {
                "state": self.state,
                "done": self.done,
                "total": self.total,
                "percent": round(pct, 1),
                "error": self.error,
            }

    def _set(self, **kw) -> None:
        with self.lock:
            for k, v in kw.items():
                setattr(self, k, v)

    def start(self, url: str, digest: str) -> bool:
        with self.lock:
            if self.state in ("downloading", "verifying"):
                return False
            self.state = "downloading"
            self.done = 0
            self.total = 0
            self.error = ""
            self.staged = None
        self.thread = threading.Thread(
            target=self._run, args=(url, digest), daemon=True)
        self.thread.start()
        return True

    def _run(self, url: str, digest: str) -> None:
        tmp = None
        try:
            exe = exe_path()
            if exe is None:
                raise RuntimeError("Not running as a packaged app.")

            # Staged next to the exe, not in %TEMP%: the final step has to be a
            # rename, and a rename only works within one volume. %TEMP% is
            # often on a different drive.
            fd, tmp_name = tempfile.mkstemp(
                dir=str(exe.parent), prefix=".update-", suffix=".part")
            os.close(fd)
            tmp = Path(tmp_name)

            sha = hashlib.sha256()
            with _request(url) as resp:
                if resp.url and urllib.parse.urlsplit(resp.url).hostname not in ALLOWED_HOSTS:
                    raise RuntimeError("Download redirected somewhere unexpected.")
                total = int(resp.headers.get("Content-Length") or 0)
                self._set(total=total)
                with open(tmp, "wb") as out:
                    while True:
                        chunk = resp.read(1024 * 256)
                        if not chunk:
                            break
                        out.write(chunk)
                        sha.update(chunk)
                        with self.lock:
                            self.done += len(chunk)

            self._set(state="verifying")
            want = digest.split(":", 1)[-1].strip().lower()
            got = sha.hexdigest()
            if not want or got != want:
                raise RuntimeError("The download did not match its checksum.")
            if tmp.stat().st_size == 0:
                raise RuntimeError("The download was empty.")

            self._set(state="ready", staged=tmp)
            tmp = None                      # kept, not cleaned up
        except Exception as e:
            self._set(state="failed", error=str(e) or e.__class__.__name__)
        finally:
            if tmp is not None:
                try:
                    tmp.unlink()
                except OSError:
                    pass

    def apply_and_restart(self) -> None:
        """Swap the staged build in and relaunch. Does not return normally."""
        with self.lock:
            if self.state != "ready" or not self.staged:
                raise RuntimeError("No verified download is ready.")
            staged = self.staged

        exe = exe_path()
        if exe is None:
            raise RuntimeError("Not running as a packaged app.")

        backup = exe.with_name(exe.name + BACKUP_SUFFIX)
        if backup.exists():
            try:
                backup.unlink()
            except OSError:
                pass

        # The running file steps aside; the new one takes its name. If the
        # second move fails, put the original back rather than leaving the
        # user with no app at all.
        os.replace(exe, backup)
        try:
            os.replace(staged, exe)
        except Exception:
            os.replace(backup, exe)
            raise

        # Detached, so it is not killed along with this process a moment later.
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | \
                    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen([str(exe)], cwd=str(exe.parent), close_fds=True,
                         creationflags=flags)

        # Let the reply reach the browser before the window disappears.
        threading.Timer(1.5, lambda: os._exit(0)).start()


INSTALL = Install()
