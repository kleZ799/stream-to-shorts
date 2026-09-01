"""Local LLM backend — OpenAI or Gemini, selected by LLM_PROVIDER."""
import re
import time
from typing import Dict, List, Optional, Tuple

from .. import usage
from ..config import (
    current_model,
    current_provider,
    require_gemini_key,
    require_openai_key,
)

# Models whose name says they are not for ranking text.
_NON_TEXT = ("image", "tts", "embed", "robotics", "computer-use", "transcribe",
             "omni", "audio", "veo", "imagen")

# ListModels is a different quota from generateContent, but it is still a
# network round trip on a settings load, so it is cached for the process.
_model_cache: Tuple[float, List[str]] = (0.0, [])
_MODEL_CACHE_SECONDS = 600


def list_gemini_models(force: bool = False) -> List[str]:
    """Text models this key can actually call, newest-looking first.

    Asked of the API rather than hardcoded, because Google retires models:
    `gemini-2.0-flash` was in a hand-written list here and had already been
    withdrawn, so offering it produced a 404 mid-run. A list that comes from
    the account itself cannot drift out of date.
    """
    global _model_cache
    age, cached = _model_cache
    if cached and not force and (time.time() - age) < _MODEL_CACHE_SECONDS:
        return cached

    try:
        from google import genai  # type: ignore
        client = genai.Client(api_key=require_gemini_key())
        names = []
        for m in client.models.list():
            name = str(m.name or "").replace("models/", "")
            if not name.startswith("gemini"):
                continue
            if "generateContent" not in (m.supported_actions or []):
                continue
            if any(bad in name for bad in _NON_TEXT):
                continue
            names.append(name)
    except Exception as e:
        print(f"[llm] could not list models ({e}); using the built-in list", flush=True)
        return []

    names.sort()
    _model_cache = (time.time(), names)
    return names


def check_gemini_model(model: str) -> Optional[str]:
    """Try the model once. Returns None if it works, else why it does not.

    ListModels is not proof of anything: it happily returns models that answer
    "no longer available to new users" when you actually call them, and that
    404 used to surface nine chunks into a run. One tiny request at the moment
    of choosing is far cheaper than discovering it later.
    """
    try:
        from google import genai  # type: ignore
        client = genai.Client(api_key=require_gemini_key())
        client.models.generate_content(model=model, contents="hi",
                                       config={"max_output_tokens": 1})
        return None
    except Exception as e:
        msg = str(e)
        if "429" in msg:
            return None     # it exists and answers; today's allowance is just spent
        if "no longer available" in msg or "404" in msg:
            return f"{model} is not available on this API key."
        if "503" in msg:
            return f"{model} is temporarily unavailable — try another."
        return f"{model} could not be used: {msg.splitlines()[0][:120]}"


class DailyQuotaExceeded(RuntimeError):
    """The provider's per-day allowance is gone. Waiting will not help."""


def call_openai_llm(prompt: str) -> str:
    """OpenAI Chat Completions backend used by --mode local."""
    try:
        from openai import OpenAI  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "openai is required for --mode local. Install it with:\n"
            "    pip install -r requirements-local.txt"
        ) from e

    model = current_model("openai")
    client = OpenAI(api_key=require_openai_key())
    response = client.chat.completions.create(
        model=model,
        temperature=0.7,
        messages=[{"role": "user", "content": prompt}],
    )
    usage.record("openai", model)
    return response.choices[0].message.content or ""


def _is_daily_quota(msg: str) -> bool:
    """Tell a per-day cap apart from a per-minute one.

    They arrive as the same 429. The per-minute one clears in a minute and is
    worth sleeping through; the per-day one does not clear until midnight
    Pacific, so sleeping on it just wastes five minutes before failing anyway.
    Google names the quota in the payload -- GenerateRequestsPerDayPerProject
    -- which is the only reliable way to tell them apart.
    """
    lowered = msg.lower()
    return "perday" in lowered.replace("_", "") or "per day" in lowered


def call_gemini_llm(prompt: str) -> str:
    """Gemini backend used by --mode local when LLM_PROVIDER=gemini."""
    try:
        from google import genai  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "google-genai is required for LLM_PROVIDER=gemini. Install it with:\n"
            "    pip install -r requirements-local.txt"
        ) from e

    model = current_model("gemini")
    client = genai.Client(api_key=require_gemini_key())
    # Gemini 3.x spends part of the output budget on internal reasoning before
    # emitting any JSON, so 8192 truncates long-chunk responses mid-object.
    config = {
        "temperature": 0.2,
        "response_mime_type": "application/json",
        "max_output_tokens": 32768,
    }

    # By the time we get here the run has already paid for a download and a
    # transcription, so a blip must not sink it. Gemini fails transiently in
    # several ways — free-tier rate limits, capacity spikes, and plain network
    # timeouts — and none of them read the same in the error string. So the
    # rule is inverted: give up immediately only on errors that retrying can
    # never fix (bad key, bad request, a spent daily allowance), and retry
    # everything else.
    attempts = 5
    last_error = None
    for attempt in range(attempts):
        try:
            response = client.models.generate_content(
                model=model, contents=prompt, config=config
            )
            break
        except Exception as e:
            last_error = e
            msg = str(e)

            if _is_daily_quota(msg):
                usage.mark_exhausted("gemini", model)
                raise DailyQuotaExceeded(
                    f"Gemini's free-tier daily quota for {model} is used up. "
                    f"It resets at midnight US Pacific."
                ) from e

            permanent = any(t in msg for t in (
                "API_KEY_INVALID", "API key not valid", "PERMISSION_DENIED",
                "UNAUTHENTICATED", "401", "403",
                "INVALID_ARGUMENT", "NOT_FOUND", "404",
            ))
            if permanent or attempt == attempts - 1:
                raise

            m = re.search(r"retry in ([0-9.]+)s", msg)
            if m:
                # The rate limiter told us exactly how long to wait.
                delay, reason = float(m.group(1)) + 1, "rate limited"
            else:
                delay = min(60.0, 5.0 * (2 ** attempt))
                reason = "transient error"

            short = msg.splitlines()[0][:120]
            print(f"[llm] {reason}; retrying in {delay:.0f}s "
                  f"(attempt {attempt + 1}/{attempts}) — {short}", flush=True)
            time.sleep(delay)
    else:
        raise last_error

    usage.record("gemini", model)

    text = response.text or ""
    if not text.strip():
        # Surface *why* it came back empty instead of failing as "invalid JSON".
        reason = "unknown"
        try:
            cand = (response.candidates or [None])[0]
            reason = str(getattr(cand, "finish_reason", "unknown"))
        except Exception:
            pass
        raise RuntimeError(f"Gemini returned no text (finish_reason={reason})")
    return text


# Once a run has switched providers there is no point asking the spent one
# again on every remaining chunk, so the choice sticks for the process.
_fallback_provider: Optional[str] = None


def _openai_is_configured() -> bool:
    try:
        return bool(require_openai_key())
    except RuntimeError:
        return False


def _switch_to_openai(why: str) -> None:
    global _fallback_provider
    _fallback_provider = "openai"
    print(f"[llm] {why} — continuing on OpenAI ({current_model('openai')})",
          flush=True)


def reset_fallback() -> None:
    """Forget a previous switch, so a new run re-checks the preferred provider."""
    global _fallback_provider
    _fallback_provider = None


def call_local_llm(prompt: str) -> str:
    """Dispatch to the configured local LLM provider.

    When Gemini's daily allowance runs out mid-run, hand the rest of the run
    to OpenAI rather than losing a download and a transcription to a quota
    that will not come back for hours. Without an OpenAI key there is nothing
    to fall back to, so the original error stands.
    """
    provider = _fallback_provider or current_provider()

    if provider == "openai":
        return call_openai_llm(prompt)
    if provider != "gemini":
        raise RuntimeError(
            f"Unknown LLM_PROVIDER={provider!r}. Use 'openai' or 'gemini'."
        )

    if usage.is_exhausted("gemini", current_model("gemini")):
        if _openai_is_configured():
            _switch_to_openai("today's Gemini quota is already spent")
            return call_openai_llm(prompt)
        raise DailyQuotaExceeded(
            "Gemini's free-tier daily quota is already used up for today. "
            "It resets at midnight US Pacific — or add an OpenAI key to keep going."
        )

    try:
        return call_gemini_llm(prompt)
    except DailyQuotaExceeded:
        if not _openai_is_configured():
            raise
        _switch_to_openai("Gemini's daily quota ran out")
        return call_openai_llm(prompt)
