"""A running count of API requests, so a day's budget is visible before it runs out.

Gemini's free tier caps requests *per day*, not per minute, and the only way
the app learned it had hit the cap was a 429 three chunks into a run that had
already paid for a download and a transcription. This keeps a local tally so
the number can be shown up front and a run can pick up the next day knowing
what it already spent.

The tally is local and therefore advisory: the same key used from another
machine, or another app, spends quota this file never sees. So a 429 that says
the daily quota is gone is treated as the truth and overrides the count --
`mark_exhausted()` exists for exactly that.

Google's free-tier day rolls over at midnight US Pacific, not local midnight
and not UTC, so that is the boundary used here.
"""
import json
import threading
from datetime import datetime, timedelta
from typing import Dict, Optional

from . import user_config

# Free-tier requests per day, by model. Anyone on a paid plan has no daily cap
# worth modelling, so DAILY_LIMIT_UNKNOWN means "don't pretend to know".
DAILY_LIMIT_UNKNOWN = 0
FREE_TIER_DAILY_LIMITS = {
    "gemini-2.5-flash-lite": 1000,
    "gemini-2.5-flash": 250,
    "gemini-2.0-flash": 200,
    "gemini-2.5-pro": 100,
    "gemini-3.6-flash": 20,
}

USAGE_FILENAME = "usage.json"

_lock = threading.Lock()


# --- the Pacific day boundary ----------------------------------------------
#
# zoneinfo would need the tzdata package on Windows, which is one more thing to
# remember to bundle into the exe and one more way for a build to break. The US
# Pacific rule is stable and short, so it is spelled out instead.

def _second_sunday_of_march(year: int) -> int:
    first_sunday = 1 + (6 - datetime(year, 3, 1).weekday()) % 7
    return first_sunday + 7


def _first_sunday_of_november(year: int) -> int:
    return 1 + (6 - datetime(year, 11, 1).weekday()) % 7


def _pacific_offset_hours(utc: datetime) -> int:
    """-7 during daylight time, -8 otherwise."""
    year = utc.year
    dst_start = datetime(year, _MARCH, _second_sunday_of_march(year), 10)
    dst_end = datetime(year, _NOVEMBER, _first_sunday_of_november(year), 9)
    return -7 if dst_start <= utc < dst_end else -8


_MARCH, _NOVEMBER = 3, 11


def _pacific_now(utc: Optional[datetime] = None) -> datetime:
    utc = utc or datetime.utcnow()
    return utc + timedelta(hours=_pacific_offset_hours(utc))


def quota_day(utc: Optional[datetime] = None) -> str:
    """The free-tier day this moment belongs to, as YYYY-MM-DD."""
    return _pacific_now(utc).strftime("%Y-%m-%d")


def seconds_until_reset(utc: Optional[datetime] = None) -> int:
    """How long until the daily allowance comes back."""
    now = _pacific_now(utc)
    midnight = datetime(now.year, now.month, now.day) + timedelta(days=1)
    return max(0, int((midnight - now).total_seconds()))


# --- the ledger -------------------------------------------------------------

def _path():
    return user_config.config_dir() / USAGE_FILENAME


def _blank(day: str) -> Dict:
    return {"day": day, "providers": {}}


def _read() -> Dict:
    """Load today's ledger, discarding any older day's numbers."""
    today = quota_day()
    try:
        data = json.loads(_path().read_text(encoding="utf-8-sig"))
    except Exception:
        return _blank(today)
    if not isinstance(data, dict) or data.get("day") != today:
        # Yesterday's tally is not an error, it is just spent history.
        return _blank(today)
    data.setdefault("providers", {})
    return data


def _write(data: Dict) -> None:
    try:
        _path().write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        # A ledger that cannot be written must never take a run down with it.
        pass


def _entry(data: Dict, provider: str) -> Dict:
    return data["providers"].setdefault(
        provider, {"requests": 0, "exhausted": False, "model": ""}
    )


def daily_limit(provider: str, model: str) -> int:
    """Requests per day for this model, or DAILY_LIMIT_UNKNOWN if uncapped."""
    override = user_config.get(
        "GEMINI_DAILY_LIMIT" if provider == "gemini" else "OPENAI_DAILY_LIMIT"
    )
    if override.isdigit():
        return int(override)
    if provider != "gemini":
        # OpenAI bills rather than cutting you off, so there is no honest
        # number to show here.
        return DAILY_LIMIT_UNKNOWN
    return FREE_TIER_DAILY_LIMITS.get(model.strip(), DAILY_LIMIT_UNKNOWN)


def record(provider: str, model: str = "", count: int = 1) -> None:
    """Count requests actually sent to a provider."""
    with _lock:
        data = _read()
        entry = _entry(data, provider)
        entry["requests"] = int(entry.get("requests", 0)) + count
        if model:
            entry["model"] = model
        _write(data)


def mark_exhausted(provider: str, model: str = "") -> None:
    """The provider says the day is spent — believe it over the local count."""
    with _lock:
        data = _read()
        entry = _entry(data, provider)
        entry["exhausted"] = True
        if model:
            entry["model"] = model
        limit = daily_limit(provider, model or entry.get("model", ""))
        if limit and int(entry.get("requests", 0)) < limit:
            # The count was behind reality — quota spent elsewhere, or a
            # request that failed after being counted against us.
            entry["requests"] = limit
        _write(data)


def is_exhausted(provider: str, model: str = "") -> bool:
    """Whether the provider itself has said today's budget is gone.

    Deliberately ignores the local count. The count is a guess -- it assumes
    the free-tier limit and that this machine is the only thing spending the
    key -- and refusing to call an API because a guess says so would strand
    anyone on a paid plan after twenty requests. A 429 naming the daily quota
    is the only thing that stops a run here.
    """
    with _lock:
        entry = _read()["providers"].get(provider) or {}
    return bool(entry.get("exhausted"))


def snapshot() -> Dict:
    """Everything the UI needs to show today's budget."""
    with _lock:
        data = _read()

    providers = {}
    for name, entry in data.get("providers", {}).items():
        model = entry.get("model", "")
        limit = daily_limit(name, model)
        used = int(entry.get("requests", 0))
        providers[name] = {
            "used": used,
            "model": model,
            "limit": limit or None,
            "remaining": max(0, limit - used) if limit else None,
            "exhausted": bool(entry.get("exhausted")) or (bool(limit) and used >= limit),
        }

    return {
        "day": data.get("day", quota_day()),
        "resets_in_seconds": seconds_until_reset(),
        "providers": providers,
    }
