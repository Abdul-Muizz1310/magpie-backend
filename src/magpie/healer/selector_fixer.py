"""LLM-powered selector fixer for broken scrapers."""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

MAX_HTML_LENGTH = 20000
MAX_RETRIES = 3


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
    """
    truncated_html = html[:MAX_HTML_LENGTH]

    last_error: Exception | None = None
    for _attempt in range(MAX_RETRIES):
        try:
            result = await _call_llm(
                field_name=field_name,
                old_selector=old_selector,
                html=truncated_html,
                old_samples=old_samples,
                selector_type=selector_type,
            )
            return result
        except Exception as e:
            last_error = e
            continue

    if last_error:
        raise last_error
    return None


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
    model = os.environ.get("OPENROUTER_MODEL_PRIMARY", "meta-llama/llama-3.3-70b-instruct")

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
