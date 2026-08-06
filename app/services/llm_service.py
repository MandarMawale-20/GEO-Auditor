"""
Groq API client for the semantic layer.

Design goals:
  * Fully optional. If GROQ_API_KEY is unset, `is_available` is False and
    every call short-circuits to (None, "llm_disabled") without a network call.
  * temperature=0, strict JSON output enforced via response_format.
  * In-memory cache keyed by (url, finding_type) so repeat audits of the
    same page during a session don't re-spend API quota.
  * Never raises. Every failure mode returns (None, error_message) so
    callers can fall back to deterministic templates.

The Groq Chat Completions API is OpenAI-compatible:
  POST https://api.groq.com/openai/v1/chat/completions
  Authorization: Bearer <GROQ_API_KEY>
  Content-Type: application/json
"""
from __future__ import annotations

import json
import re
from typing import Any

import httpx

from app.config import settings

# cache key: (final_url, finding_type) -> parsed JSON/text result
llm_cache: dict[tuple[str, str], dict[str, Any] | str] = {}


class LLMResult:
    def __init__(self, data: dict[str, Any] | str | None, error: str | None, from_cache: bool = False):
        self.data = data
        self.error = error
        self.from_cache = from_cache

    @property
    def ok(self) -> bool:
        return self.error is None and self.data is not None


def is_available() -> bool:
    return settings.llm_enabled


async def _call_groq(prompt: str, expect_json: bool) -> tuple[str | None, str | None]:
    """Low-level Groq REST call. Returns (raw_text, error)."""
    if not settings.groq_api_key:
        return None, "llm_disabled: no GROQ_API_KEY configured"

    # Groq uses OpenAI-compatible endpoint format.
    # settings.groq_base_url is "https://api.groq.com/openai/v1"
    endpoint = f"{settings.groq_base_url.rstrip('/')}/chat/completions"

    payload: dict[str, Any] = {
        "model": settings.groq_model,
        "messages": [{"role": "user", "parts": [{"text": prompt}]}],
        "temperature": 0,
        "max_tokens": 1024,
    }

    # Groq's OpenAI-compatible API uses "parts" for text, but the actual
    # field name is "content". Build the correct message structure.
    payload["messages"] = [{"role": "user", "content": prompt}]

    if expect_json:
        payload["response_format"] = {"type": "json_object"}

    try:
        async with httpx.AsyncClient(
            timeout=settings.llm_timeout_seconds,
            headers={
                "Authorization": f"Bearer {settings.groq_api_key}",
                "Content-Type": "application/json",
            },
        ) as client:
            response = await client.post(endpoint, json=payload)
            if response.status_code == 429:
                return None, "Groq API quota exhausted (429)."
            if response.status_code == 401 or response.status_code == 403:
                return None, "Groq API key invalid or unauthorized."
            if response.status_code >= 400:
                return None, f"Groq API error {response.status_code}: {response.text[:200]}"

            body = response.json()
            choices = body.get("choices", [])
            if not choices:
                return None, "Groq API returned no choices."
            content = choices[0].get("message", {}).get("content", "")
            if not content:
                return None, "Groq API returned empty content."
            return content.strip(), None
    except httpx.TimeoutException:
        return None, "Groq API request timed out."
    except httpx.HTTPError as exc:
        return None, f"Groq API network error: {exc}"
    except Exception as exc:  # noqa: BLE001
        return None, f"Unexpected Groq API error: {exc}"


def _strip_json_fences(text: str) -> str:
    """Extract clean JSON from a model response that may include markdown
    fences, backticks, or conversational preamble.

    Strategy (tried in order):
      1. Triple-backtick code block (```json ... ```)
      2. Single-backtick inline code containing a JSON object (tries each,
         returns the first that parses as valid JSON)
      3. Individual balanced brace-pairs via regex (tries from last to first)
      4. Fallback: first '{' to last '}' brace matching
    """
    stripped = text.strip()

    # --- 1. Triple-backtick fences -------------------------------------------
    if "```" in stripped:
        parts = stripped.split("```", 2)
        if len(parts) >= 2:
            inner = parts[1].strip()
            if inner.lower().startswith("json"):
                inner = inner[4:].strip()
            stripped = inner

    # --- 2. Single-backtick inline code that looks like JSON -----------------
    for match in re.finditer(r"`([^`]+)`", stripped):
        candidate = match.group(1).strip()
        if candidate.startswith("{") and candidate.endswith("}"):
            try:
                json.loads(candidate)
                return candidate
            except json.JSONDecodeError:
                continue

    # --- 3. Try every balanced brace-pair found by regex ----------------------
    matches = re.findall(r"\{[^{}]*\}", stripped, re.DOTALL)
    if matches:
        for candidate in reversed(matches):
            candidate = candidate.strip()
            try:
                json.loads(candidate)
                return candidate
            except json.JSONDecodeError:
                continue

    # --- 4. Fallback: first { to last } brace matching --------------------
    first_brace = stripped.find("{")
    last_brace = stripped.rfind("}")
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        return stripped[first_brace : last_brace + 1]

    return stripped


async def get_json_completion(
    cache_url: str, finding_type: str, prompt: str
) -> LLMResult:
    """Call Groq expecting strict JSON output, with caching and fallback-safe errors."""
    cache_key = (cache_url, finding_type)
    if cache_key in llm_cache:
        cached = llm_cache[cache_key]
        return LLMResult(data=cached, error=None, from_cache=True)

    if not is_available():
        return LLMResult(data=None, error="llm_disabled: no GROQ_API_KEY configured")

    raw_text, error = await _call_groq(prompt, expect_json=True)
    if error or raw_text is None:
        return LLMResult(data=None, error=error or "Unknown LLM error")

    try:
        cleaned = _strip_json_fences(raw_text)
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        return LLMResult(data=None, error="Groq returned malformed JSON.")

    llm_cache[cache_key] = parsed
    return LLMResult(data=parsed, error=None)


async def get_text_completion(cache_url: str, finding_type: str, prompt: str) -> LLMResult:
    """Call Groq expecting plain text output (e.g. rewrites), with caching."""
    cache_key = (cache_url, finding_type)
    if cache_key in llm_cache:
        cached = llm_cache[cache_key]
        return LLMResult(data=cached, error=None, from_cache=True)

    if not is_available():
        return LLMResult(data=None, error="llm_disabled: no GROQ_API_KEY configured")

    raw_text, error = await _call_groq(prompt, expect_json=False)
    if error or raw_text is None:
        return LLMResult(data=None, error=error or "Unknown LLM error")

    llm_cache[cache_key] = raw_text
    return LLMResult(data=raw_text, error=None)


def clear_cache() -> None:
    llm_cache.clear()
