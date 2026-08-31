"""Per-user settings stored outside the repo.

The .env file works fine when you cloned the source, but someone running a
packaged build has no repo to edit. This keeps their API key in the platform's
normal per-user config location instead, so the app can ask for it once in the
UI and remember it.

Precedence is deliberate: a real environment variable always wins, so an
existing .env setup keeps behaving exactly as before.
"""
import json
import os
from pathlib import Path
from typing import Dict, Optional


def config_dir() -> Path:
    """Per-user config directory, created on demand."""
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif os.uname().sysname == "Darwin":  # type: ignore[attr-defined]
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    d = Path(base) / "StreamToShorts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_path() -> Path:
    return config_dir() / "settings.json"


def load() -> Dict:
    try:
        return json.loads(config_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def save(values: Dict) -> Path:
    """Merge `values` into the stored config and write it back."""
    current = load()
    current.update({k: v for k, v in values.items() if v is not None})
    path = config_path()
    path.write_text(json.dumps(current, indent=2), encoding="utf-8")
    try:
        # The file holds an API key — keep it owner-only where that's meaningful.
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def get(key: str, default: str = "") -> str:
    """Environment first, then the stored config."""
    env = os.getenv(key, "").strip()
    if env:
        return env
    val = load().get(key)
    return str(val).strip() if val else default


# Where things land under the chosen root. Source videos keep the historical
# "output" name so transcripts already cached beside them stay valid.
SOURCE_SUBDIR = "output"
SHORTS_SUBDIR = "shorts"


def output_root() -> Path:
    """Root folder for everything this app writes. Defaults to the cwd."""
    configured = get("OUTPUT_ROOT")
    if configured:
        return Path(configured).expanduser()
    return Path.cwd()


def set_output_root(path: str) -> Path:
    """Point the app at a new save location, after proving we can write there."""
    p = Path(path).expanduser()
    if p.exists() and not p.is_dir():
        raise ValueError(f"{p} is a file, not a folder.")
    try:
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".stream-to-shorts-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as e:
        raise ValueError(f"Can't write to {p} ({e.strerror or e}).") from e

    resolved = p.resolve()
    save({"OUTPUT_ROOT": str(resolved)})
    return resolved


def source_dir() -> Path:
    """Where full downloaded videos and their transcripts live."""
    raw = os.getenv("LOCAL_OUTPUT_DIR", "").strip()
    if raw and Path(raw).is_absolute():
        d = Path(raw)
    else:
        d = output_root() / (raw or SOURCE_SUBDIR)
    d.mkdir(parents=True, exist_ok=True)
    return d


def shorts_dir() -> Path:
    """Where generated clips live, one subfolder per run."""
    d = output_root() / SHORTS_SUBDIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def has_llm_key() -> bool:
    provider = (get("LLM_PROVIDER", "gemini") or "gemini").lower()
    if provider == "openai":
        return bool(get("OPENAI_API_KEY"))
    return bool(get("GEMINI_API_KEY"))
