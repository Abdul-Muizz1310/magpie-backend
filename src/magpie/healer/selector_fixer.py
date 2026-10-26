"""LLM-powered selector fixer for broken scrapers.

Every OpenRouter attempt is one Langfuse generation, ``propose-selector``
(docs/specs/12-tracing.md): the model it asked, the prompt, the reply, token usage
and cost, and level ERROR naming the stage when the attempt failed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
from collections.abc import Mapping
from typing import Annotated, Any

import httpx
from langfuse import LangfuseGeneration
from langfuse.api import MapValue
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from magpie.core import tracing

log = logging.getLogger("magpie.healer.selector_fixer")

MAX_HTML_LENGTH = 40000
MAX_RETRIES = 3
RETRY_BASE_DELAY_S = 2.0

# Default to the portfolio's primary free OpenRouter model rather than a paid one.
# An unset OPENROUTER_MODEL_PRIMARY must never silently bill paid credits (MAG-5).
# render.yaml and heal-on-failure.yml name the same slug (tests/unit/test_ops_baseline.py).
DEFAULT_MODEL = "google/gemma-4-26b-a4b-it:free"


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


def _count(value: object) -> int | None:
    """A token count OpenRouter reported, or ``None`` unless it is a non-negative int."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _cost(value: object) -> float | None:
    """A cost OpenRouter reported, or ``None`` unless it is a finite, non-negative number."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    cost = float(value)
    return cost if math.isfinite(cost) and cost >= 0 else None


def _mapping_or_none(value: object) -> object:
    return value if isinstance(value, Mapping) else None


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


_Count = Annotated[int | None, BeforeValidator(_count)]
_Cost = Annotated[float | None, BeforeValidator(_cost)]
_Text = Annotated[str | None, BeforeValidator(_str_or_none)]


class _Details(BaseModel):
    cached_tokens: _Count = None
    reasoning_tokens: _Count = None


class _Usage(BaseModel):
    """Telemetry only: a field that does not read is dropped, never fatal to the call."""

    prompt_tokens: _Count = None
    completion_tokens: _Count = None
    cost: _Cost = None
    prompt_tokens_details: Annotated[_Details | None, BeforeValidator(_mapping_or_none)] = None
    completion_tokens_details: Annotated[_Details | None, BeforeValidator(_mapping_or_none)] = None


class _Message(BaseModel):
    content: str | None = None
    reasoning: _Text = None


class _Choice(BaseModel):
    message: _Message


class _ReplyError(BaseModel):
    """OpenRouter's error object, which can come back inside a 200 instead of a completion."""

    code: _Count = None
    message: _Text = None


class _Reply(BaseModel):
    """The part of an OpenRouter chat completion the fixer reads."""

    provider: _Text = None
    choices: list[_Choice] = []
    usage: Annotated[_Usage | None, BeforeValidator(_mapping_or_none)] = None
    error: Annotated[_ReplyError | None, BeforeValidator(_mapping_or_none)] = None


class _UpstreamError(Exception):
    """OpenRouter answered 200 with its error object (spec 12): retried like any failure."""

    def __init__(self, error: _ReplyError) -> None:
        super().__init__(f"status={error.code}: {error.message}")
        self.code = error.code


def _status_stage(code: int) -> str:
    return "rate_limited" if code == 429 else "server_error" if code >= 500 else "client_error"


def _failure_stage(error: Exception) -> str:
    """The stage a failed attempt is filed under on its generation (spec 12)."""
    if isinstance(error, httpx.HTTPStatusError):
        code = error.response.status_code
        return f"{_status_stage(code)}: status={code}"
    if isinstance(error, _UpstreamError):
        if error.code is None:
            return "upstream_error: in the reply body"
        return f"{_status_stage(error.code)}: status={error.code} in the reply body"
    if isinstance(error, httpx.TimeoutException):
        return "timeout"
    if isinstance(error, httpx.HTTPError):
        return f"http_error: {type(error).__name__}"
    return f"bad_response: {type(error).__name__}"


def _record_reply(generation: LangfuseGeneration, reply: _Reply, content: str) -> None:
    output: dict[str, str] = {"role": "assistant", "content": content}
    reasoning = reply.choices[0].message.reasoning
    if reasoning:
        output["reasoning"] = reasoning
    usage = reply.usage or _Usage()
    prompt_details = usage.prompt_tokens_details or _Details()
    completion_details = usage.completion_tokens_details or _Details()
    buckets = tracing.usage_details(
        prompt=usage.prompt_tokens,
        completion=usage.completion_tokens,
        cached=prompt_details.cached_tokens,
        reasoning=completion_details.reasoning_tokens,
    )
    generation.update(
        output=output,
        usage_details=buckets or None,
        cost_details=tracing.cost_details(usage.cost),
        metadata={"provider": reply.provider} if reply.provider else None,
    )


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
                attempt=attempt + 1,
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
    attempt: int = 1,
) -> dict[str, Any]:
    """Call OpenRouter LLM to fix a broken selector: one attempt, one generation.

    ``attempt`` (1-based) is recorded on the generation. A failed attempt marks the
    generation ERROR with its stage and raises unchanged, for ``fix_selector`` to retry.
    """
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

    messages = [{"role": "user", "content": prompt}]
    parameters: dict[str, MapValue] = {"temperature": 0.1, "response_format": "json_object"}
    with tracing.client().start_as_current_observation(
        as_type="generation",
        name="propose-selector",
        model=model,
        input=messages,
        model_parameters=parameters,
        metadata={"attempt": attempt},
    ) as generation:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "messages": messages,
                        "response_format": {"type": "json_object"},
                        "temperature": 0.1,
                    },
                    timeout=30.0,
                )
                resp.raise_for_status()
            reply = _Reply.model_validate(resp.json())
            if reply.error is not None:
                generation.update(
                    output={"error": reply.error.model_dump()},
                    metadata={"provider": reply.provider} if reply.provider else None,
                )
                raise _UpstreamError(reply.error)
            if not reply.choices:
                msg = "the reply has no choices"
                raise ValueError(msg)
            content = reply.choices[0].message.content
            if content is None or not content.strip():
                msg = "the reply has no completion"
                raise ValueError(msg)
            # Recorded before the proposal is read: a reply the schema refuses
            # (and the tokens it cost) is still on the trace.
            _record_reply(generation, reply, content)
            raw = json.loads(content)
            validated = _LLMSelectorProposal.model_validate(raw)
        except Exception as e:
            generation.update(level="ERROR", status_message=_failure_stage(e))
            raise
        except asyncio.CancelledError:
            generation.update(level="ERROR", status_message="cancelled before the model answered")
            raise
        return validated.model_dump()
