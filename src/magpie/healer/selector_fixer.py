"""LLM-powered selector fixer for broken scrapers."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

log = logging.getLogger("magpie.healer.selector_fixer")

MAX_HTML_LENGTH = 40000
MAX_RETRIES = 3
RETRY_BASE_DELAY_S = 2.0

# Default to the portfolio's chosen free-tier OpenRouter slug rather than a paid
# model. An unset OPENROUTER_MODEL_PRIMARY must never silently bill paid credits
# (MAG-5) — the free ``:free`` variant is the safe default for the demo deploy.
DEFAULT_MODEL = "nvidia/nemotron-nano-9b-v2:free"


class _LLMSelectorProposal(BaseModel):
    """Shape the LLM must return. Extra keys are ignored, missing ones reject.

    Why:
        LLMs occasionally return malformed JSON or miss keys. Without this
        model the first KeyError downstream is the error users see; with it
        the failure happens right at the parse boundary and the retry loop
        catches it before it reaches ``create_heal_pr``.
    """

    model_config = ConfigDict(extra="ignore")

    selector: str | None
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str
    sample_values: list[str] = []


_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style|noscript|template|svg)\b[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_WHITESPACE_RE = re.compile(r"[ \t\r\f\v]+")
_BLANKLINES_RE = re.compile(r"\n\s*\n+")


def _prepare_html_for_llm(html: str, old_selector: str) -> str:
    """Shrink raw HTML to the signal the LLM needs before truncating (MAG-4).

    Real target pages bury the item container behind kilobytes of ``<head>``,
    inline ``<script>`` and ``<style>`` noise, so a blind ``html[:N]`` head slice
    often cuts off the DOM region the healer must re-select. We first strip
    script/style/comments and collapse whitespace, then — when the old selector
    hints at a class/tag we can locate — window the HTML around its first
    occurrence so the relevant region survives the cap even on huge pages.
    """
    cleaned = _SCRIPT_STYLE_RE.sub(" ", html)
    cleaned = _COMMENT_RE.sub(" ", cleaned)
    cleaned = _WHITESPACE_RE.sub(" ", cleaned)
    cleaned = _BLANKLINES_RE.sub("\n", cleaned)

    if len(cleaned) <= MAX_HTML_LENGTH:
        return cleaned

    # Try to window around a distinctive token from the old selector (a class,
    # id, or tag name) so the container the healer needs isn't sliced away.
    for token in _selector_tokens(old_selector):
        idx = cleaned.find(token)
        if idx != -1:
            half = MAX_HTML_LENGTH // 2
            start = max(0, idx - half)
            return cleaned[start : start + MAX_HTML_LENGTH]

    return cleaned[:MAX_HTML_LENGTH]


def _selector_tokens(selector: str) -> list[str]:
    """Extract searchable literals (class/id/tag names) from a CSS/XPath selector."""
    tokens = [t for t in re.split(r"[^A-Za-z0-9_-]+", selector) if len(t) >= 3]
    # Longest first — a more specific class name is a better anchor than a tag.
    return sorted(dict.fromkeys(tokens), key=len, reverse=True)


async def fix_selector(
    *,
    field_name: str,
    old_selector: str,
    html: str,
    old_samples: list[str],
    selector_type: str = "css",
) -> dict[str, Any] | None:
    """Ask an LLM to propose a new selector (CSS or XPath) for a broken field.

    Returns the LLM response dict with keys: selector, confidence, reasoning,
    sample_values. Returns None only on total failure after retries.

    Transient errors (429/5xx) are retried with exponential backoff so all three
    attempts don't fire back-to-back into the same rate limit (spec 03-healer).
    """
    prepared_html = _prepare_html_for_llm(html, old_selector)

    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            return await _call_llm(
                field_name=field_name,
                old_selector=old_selector,
                html=prepared_html,
                old_samples=old_samples,
                selector_type=selector_type,
            )
        except Exception as e:
            last_error = e
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(_retry_delay(e, attempt))
            continue

    if last_error:
        raise last_error
    return None


def _retry_delay(error: Exception, attempt: int) -> float:
    """Seconds to wait before the next attempt — honour ``Retry-After`` on 429."""
    if isinstance(error, httpx.HTTPStatusError) and error.response.status_code == 429:
        retry_after = error.response.headers.get("retry-after")
        if retry_after:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                log.debug("Unparseable Retry-After header: %r", retry_after)
    return RETRY_BASE_DELAY_S * (2.0**attempt)


async def _call_llm(
    *,
    field_name: str,
    old_selector: str,
    html: str,
    old_samples: list[str],
    selector_type: str = "css",
) -> dict[str, Any]:
    """Call OpenRouter LLM to fix a broken selector."""
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    base_url = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    model = os.environ.get("OPENROUTER_MODEL_PRIMARY", "").strip() or DEFAULT_MODEL

    kind = "XPath" if selector_type == "xpath" else "CSS"
    prompt = (
        f"You are a web scraping expert. A {kind} selector previously used to "
        f"extract a field from a web page has stopped returning results. You need "
        f"to propose a new {kind} selector that works on the CURRENT HTML.\n\n"
        f"Field name: {field_name}\n"
        f"Old selector ({kind}): {old_selector}\n"
        f"Old sample extracted values: {json.dumps(old_samples)}\n\n"
        f"Here is the current HTML (truncated):\n```html\n{html}\n```\n\n"
        f"Return ONLY a {kind} selector — do not switch selector kinds.\n\n"
        f"Return JSON: "
        f'{{"selector": "new {kind} selector" | null, "reasoning": "why", '
        f'"confidence": 0.0 to 1.0, "sample_values": ["extracted", "examples"]}}'
    )

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"},
                "temperature": 0.1,
            },
            timeout=30.0,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        raw = json.loads(content)
        validated = _LLMSelectorProposal.model_validate(raw)
        return validated.model_dump()
