"""Langfuse tracing: Langfuse v4 through Python SDK v5 (docs/specs/12-tracing.md).

A heal is one trace. ``heal_trace`` opens its root observation, ``heal-source``,
and gives every observation under it the trace attributes; the healer opens its
steps on ``client()``; the selector fixer records each OpenRouter attempt as a
generation, with ``usage_details`` and ``cost_details`` turning OpenRouter's
counts into Langfuse's buckets. Without both Langfuse keys every observation is a
no-op and nothing is exported.

Export runs in the SDK's background batches; nothing here flushes on the heal
path. ``shutdown`` flushes what is left when the app stops or the heal job exits.
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Literal

from langfuse import Langfuse, LangfuseAgent, propagate_attributes

SERVICE_TAG = "magpie"

#: What started a heal: the Procrastinate task in the web service, or the heal job's CLI.
Trigger = Literal["worker", "cli"]

_ENVIRONMENT = re.compile(r"[a-z0-9_-]{1,40}")

_client: Langfuse | None = None


def environment(environ: Mapping[str, str]) -> str:
    """``APP_ENV`` (``development`` when unset), refused unless Langfuse accepts it."""
    value = environ.get("APP_ENV", "development")
    if _ENVIRONMENT.fullmatch(value) is None or value.startswith("langfuse"):
        msg = (
            "APP_ENV is also the Langfuse environment: 1-40 of a-z, 0-9, '_' and '-', "
            f"not starting with 'langfuse' (got {value!r})"
        )
        raise ValueError(msg)
    return value


def build_client(environ: Mapping[str, str]) -> Langfuse:
    """A client for ``environ``: tracing on with both keys, a no-op without either."""
    env = environment(environ)
    public_key = environ.get("LANGFUSE_PUBLIC_KEY", "").strip()
    secret_key = environ.get("LANGFUSE_SECRET_KEY", "").strip()
    if not (public_key and secret_key):
        return Langfuse(tracing_enabled=False)
    return Langfuse(
        public_key=public_key,
        secret_key=secret_key,
        base_url=environ.get("LANGFUSE_BASE_URL", "").strip() or None,
        environment=env,
    )


def client() -> Langfuse:
    """The process's client, built from the environment on first use.

    The app lifespan and the heal job call it at startup, so an invalid ``APP_ENV``
    stops the boot rather than a heal.
    """
    global _client
    if _client is None:
        _client = build_client(os.environ)
    return _client


def shutdown() -> None:
    """Flush buffered observations and stop the exporter (app shutdown, end of the heal job)."""
    if _client is not None:
        _client.shutdown()


@contextmanager
def heal_trace(
    *, source: str, run_id: uuid.UUID | None, trigger: Trigger
) -> Iterator[LangfuseAgent]:
    """Open ``heal-source`` and give every observation under it the trace attributes.

    No session: no request id reaches a heal (spec 12, "Trace attributes").
    """
    run = None if run_id is None else str(run_id)
    metadata = {"source": source} if run is None else {"source": source, "run_id": run}
    with (
        client().start_as_current_observation(
            as_type="agent",
            name="heal-source",
            input={"source": source, "run_id": run},
        ) as root,
        propagate_attributes(
            trace_name="heal-source",
            tags=[SERVICE_TAG, f"trigger:{trigger}"],
            metadata=metadata,
        ),
    ):
        yield root


def usage_details(
    *,
    prompt: int | None = None,
    completion: int | None = None,
    cached: int | None = None,
    reasoning: int | None = None,
) -> dict[str, int]:
    """OpenRouter's token counts as Langfuse's exclusive usage buckets.

    OpenRouter counts cached tokens inside ``prompt`` and reasoning tokens inside
    ``completion``; Langfuse adds its buckets up, so each detail is taken out of its
    total (and never more than the total). A total that was not reported drops its
    buckets, details included.
    """
    buckets: dict[str, int] = {}
    if prompt is not None:
        hit = min(cached or 0, prompt)
        buckets["input"] = prompt - hit
        if hit:
            buckets["input_cached_tokens"] = hit
    if completion is not None:
        thought = min(reasoning or 0, completion)
        buckets["output"] = completion - thought
        if thought:
            buckets["output_reasoning_tokens"] = thought
    return buckets


def cost_details(cost: float | None) -> dict[str, float] | None:
    """The cost OpenRouter reported for a call, as the generation's total (USD)."""
    return None if cost is None else {"total": cost}
