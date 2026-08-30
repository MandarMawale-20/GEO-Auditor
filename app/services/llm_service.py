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
import logging
from typing import Any

from cachetools import TTLCache

from app.config import settings

logger = logging.getLogger("geo_auditor.llm")

# cache key: (final_url, finding_type) -> parsed JSON/text result
# Stores up to 1000 entries, expires them after 1 hour (3600 seconds).
llm_cache: TTLCache = TTLCache(maxsize=1000, ttl=3600)


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
    """Low-level Groq REST call. Returns (raw_text, error).

    Uses the centralized SSRF-safe HTTP client for the API request itself.
    The Groq API endpoint is a fixed, allow-listed URL — no user-controlled
    input reaches the URL, but routing through the shared client ensures
    consistent connection pooling and timeout behaviour.
    """
    if not settings.groq_api_key:
        return None, "llm_disabled: no GROQ_API_KEY configured"

    endpoint = f"{settings.groq_base_url.rstrip('/')}/chat/completions"

    payload: dict[str, Any] = {
        "model": settings.groq_model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 1024,
    }

    if expect_json:
        payload["response_format"] = {"type": "json_object"}

    import httpx

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


def _strip_reasoning(text: str) -> str:
    """Remove `` and `` blocks emitted by reasoning models (e.g. Qwen3).

    These models prepend chain-of-thought content wrapped in `` tags
    before the actual answer. Left intact, that preamble can corrupt JSON
    parsing, so we strip it on both sides of the first content token.
    """
    import re

    # Drop any full  ...  blocks (non-greedy, across newlines).
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    # Also defend against an unterminated trailing  block: strip just the
    # line(s) that start with the tag (MULTILINE keeps the JSON below intact).
    cleaned = re.sub(r"(?m)^\s*<think>.*$", "", cleaned, flags=re.IGNORECASE)
    return cleaned


def _strip_json_fences(text: str) -> str:
    """Extract the first balanced JSON object from text that may include markdown fences.

    Uses ``json.JSONDecoder.raw_decode`` to find the first parseable JSON
    object, avoiding the greedy regex pitfall where nested objects cause
    incorrect extraction. Reasoning-model `` blocks are stripped first.
    """
    # Strip leading markdown fences / preamble.
    stripped = _strip_reasoning(text).strip()
    # Remove leading ```json or ``` fences.
    if stripped.startswith("```"):
        # Find the content between fences.
        lines = stripped.split("\n")
        in_fence = False
        content_lines = []
        for line in lines:
            if line.strip().startswith("```"):
                if not in_fence:
                    in_fence = True
                    continue
                else:
                    in_fence = False
                    continue
            if in_fence:
                content_lines.append(line)
        if content_lines:
            stripped = "\n".join(content_lines).strip()

    # Try to find the first '{' that starts valid JSON.
    decoder = json.JSONDecoder()
    for i, char in enumerate(stripped):
        if char == "{":
            try:
                obj, _end = decoder.raw_decode(stripped[i:])
                return json.dumps(obj)
            except json.JSONDecodeError:
                continue

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
