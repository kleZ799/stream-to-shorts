"""Local LLM backend — OpenAI or Gemini, selected by LLM_PROVIDER."""
import re
import time

from ..config import (
    GEMINI_MODEL,
    LLM_PROVIDER,
    OPENAI_MODEL,
    require_gemini_key,
    require_openai_key,
)


def call_openai_llm(prompt: str) -> str:
    """OpenAI Chat Completions backend used by --mode local."""
    try:
        from openai import OpenAI  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "openai is required for --mode local. Install it with:\n"
            "    pip install -r requirements-local.txt"
        ) from e

    client = OpenAI(api_key=require_openai_key())
    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        temperature=0.7,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content or ""


def call_gemini_llm(prompt: str) -> str:
    """Gemini backend used by --mode local when LLM_PROVIDER=gemini."""
    try:
        from google import genai  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "google-genai is required for LLM_PROVIDER=gemini. Install it with:\n"
            "    pip install -r requirements-local.txt"
        ) from e

    client = genai.Client(api_key=require_gemini_key())
    # Gemini 3.x spends part of the output budget on internal reasoning before
    # emitting any JSON, so 8192 truncates long-chunk responses mid-object.
    config = {
        "temperature": 0.2,
        "response_mime_type": "application/json",
        "max_output_tokens": 32768,
    }

    # The free tier rate-limits aggressively and tells us how long to wait;
    # honour that instead of failing the whole pipeline mid-run.
    last_error = None
    for attempt in range(4):
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL, contents=prompt, config=config
            )
            break
        except Exception as e:
            last_error = e
            if "RESOURCE_EXHAUSTED" not in str(e) and "429" not in str(e):
                raise
            m = re.search(r"retry in ([0-9.]+)s", str(e))
            delay = float(m.group(1)) + 1 if m else 30.0 * (attempt + 1)
            if attempt == 3:
                raise
            print(f"[llm] rate limited; waiting {delay:.0f}s", flush=True)
            time.sleep(delay)
    else:
        raise last_error

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


def call_local_llm(prompt: str) -> str:
    """Dispatch to the configured local LLM provider."""
    provider = (LLM_PROVIDER or "openai").strip().lower()
    if provider == "openai":
        return call_openai_llm(prompt)
    if provider == "gemini":
        return call_gemini_llm(prompt)
    raise RuntimeError(
        f"Unknown LLM_PROVIDER={provider!r}. Use 'openai' or 'gemini'."
    )
