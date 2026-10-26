"""Langfuse tracing of the self-heal agent — docs/specs/12-tracing.md, cases T1-T15.

Spans go to an in-memory exporter (the ``traces`` fixture); nothing reaches Langfuse.
OpenRouter is a ``httpx.MockTransport`` behind the client the fixer opens.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from typing import Any, ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import yaml
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

import magpie.healer.run as heal_cli
import magpie.lifespan as lifespan_module
from magpie.config.schema import SourceConfig
from magpie.core import tracing
from magpie.healer import selector_fixer
from magpie.healer.apply import heal_source
from magpie.healer.outcome import HealedTarget, HealSummary
from magpie.healer.selector_fixer import DEFAULT_MODEL, _call_llm, fix_selector
from magpie.storage.models import HealMode, SourceOrigin
from magpie.storage.sources_repo import SourcesRepository
from tests.conftest import Traces, tracing_client

INPUT = "langfuse.observation.input"
OUTPUT = "langfuse.observation.output"
_REAL_ASYNC_CLIENT = httpx.AsyncClient

BROKEN_CONTAINER_YAML = """\
name: api-src
url: https://example.com/list
schedule: "0 */6 * * *"
item:
  container: "div.card"
  fields:
    - { name: title, selector: "h2::text" }
    - { name: id, selector: "::attr(data-id)" }
  dedupe_key: id
"""

BROKEN_FIELD_YAML = """\
name: file-src
url: https://example.com/list
schedule: "0 */6 * * *"
item:
  container: "div.card"
  fields:
    - { name: title, selector: ".does-not-exist::text" }
    - { name: id, selector: "::attr(data-id)" }
  dedupe_key: id
"""

ITEM_HTML = """
<html><body>
<div class="item" data-id="1"><h2>Hello</h2></div>
<div class="item" data-id="2"><h2>World</h2></div>
</body></html>
"""

CARD_HTML = """
<html><body>
<div class="card" data-id="1"><h2 class="headline">Hello</h2></div>
<div class="card" data-id="2"><h2 class="headline">World</h2></div>
</body></html>
"""


def _proposal(selector: str | None, **extra: Any) -> dict[str, Any]:
    return {
        "selector": selector,
        "confidence": 0.9,
        "reasoning": "the markup moved",
        "sample_values": ["Hello", "World"],
        **extra,
    }


def _reply(content: str | None, **extra: Any) -> dict[str, Any]:
    return {"choices": [{"message": {"role": "assistant", "content": content}}], **extra}


def _ok(selector: str | None = "div.item", **extra: Any) -> httpx.Response:
    return httpx.Response(200, json=_reply(json.dumps(_proposal(selector)), **extra))


@contextmanager
def openrouter(handler: Callable[[httpx.Request], httpx.Response]) -> Iterator[None]:
    """Answer the fixer's OpenRouter calls with ``handler``."""

    def client(*_args: Any, **_kwargs: Any) -> httpx.AsyncClient:
        return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler))

    with patch("magpie.healer.selector_fixer.httpx.AsyncClient", new=client):
        yield


def _in_order(*responses: httpx.Response) -> Callable[[httpx.Request], httpx.Response]:
    queue = list(responses)

    def handler(_request: httpx.Request) -> httpx.Response:
        return queue.pop(0)

    return handler


@pytest.fixture(autouse=True)
def _llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    for slot in ("PRIMARY", "FAST", "FALLBACK"):
        monkeypatch.delenv(f"OPENROUTER_MODEL_{slot}", raising=False)
    # Retries back off by RETRY_BASE_DELAY_S * 2^n: zero, so no test sleeps.
    monkeypatch.setattr(selector_fixer, "RETRY_BASE_DELAY_S", 0.0)


async def _seed(session_factory: Any, yaml_text: str, origin: SourceOrigin) -> str:
    cfg = SourceConfig(**yaml.safe_load(yaml_text))
    async with session_factory() as session:
        await SourcesRepository(session).create(config=cfg, origin=origin, yaml_text=yaml_text)
        await session.commit()
    return cfg.name


def _names(spans: Sequence[ReadableSpan]) -> list[str]:
    return [s.name for s in spans]


# ---------------------------------------------------------------------------
# T1, T2 — the client from the environment
# ---------------------------------------------------------------------------


class _Recorder:
    seen: ClassVar[dict[str, Any]] = {}

    def __init__(self, **kwargs: Any) -> None:
        _Recorder.seen = kwargs


def test_t1_both_keys_build_an_enabled_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tracing, "Langfuse", _Recorder)
    tracing.build_client(
        {
            "LANGFUSE_PUBLIC_KEY": "pk-lf-1",
            "LANGFUSE_SECRET_KEY": "sk-lf-1",
            "LANGFUSE_BASE_URL": "https://us.cloud.langfuse.com",
            "APP_ENV": "production",
        }
    )
    assert _Recorder.seen == {
        "public_key": "pk-lf-1",
        "secret_key": "sk-lf-1",
        "base_url": "https://us.cloud.langfuse.com",
        "environment": "production",
    }


def test_t1_unset_app_env_and_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tracing, "Langfuse", _Recorder)
    tracing.build_client({"LANGFUSE_PUBLIC_KEY": "pk", "LANGFUSE_SECRET_KEY": "sk"})
    assert _Recorder.seen["environment"] == "development"
    assert _Recorder.seen["base_url"] is None


@pytest.mark.parametrize(
    "keys",
    [
        {"LANGFUSE_PUBLIC_KEY": "", "LANGFUSE_SECRET_KEY": "sk"},
        {"LANGFUSE_PUBLIC_KEY": "pk", "LANGFUSE_SECRET_KEY": "  "},
        {"LANGFUSE_SECRET_KEY": "sk"},
        {},
    ],
)
def test_t1_a_missing_key_disables_tracing(
    monkeypatch: pytest.MonkeyPatch, keys: dict[str, str]
) -> None:
    monkeypatch.setattr(tracing, "Langfuse", _Recorder)
    tracing.build_client(keys)
    assert _Recorder.seen == {"tracing_enabled": False}


def test_t1_the_client_is_built_once(monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[object] = []

    def build(environ: object) -> str:
        built.append(environ)
        return "client"

    monkeypatch.setattr(tracing, "_client", None)
    monkeypatch.setattr(tracing, "build_client", build)
    assert tracing.client() == "client"
    assert tracing.client() == "client"
    assert len(built) == 1


def test_t1_shutdown_before_first_use_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tracing, "_client", None)
    tracing.shutdown()


def test_t1_shutdown_shuts_the_client_down(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MagicMock()
    monkeypatch.setattr(tracing, "_client", client)
    tracing.shutdown()
    client.shutdown.assert_called_once_with()


@pytest.mark.parametrize("value", ["production", "test", "dev_1", "pr-12"])
def test_t2_app_env_accepts_langfuse_environments(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setattr(tracing, "Langfuse", _Recorder)
    tracing.build_client(
        {"LANGFUSE_PUBLIC_KEY": "pk", "LANGFUSE_SECRET_KEY": "sk", "APP_ENV": value}
    )
    assert _Recorder.seen["environment"] == value


@pytest.mark.parametrize("value", ["Production", "langfuse-ci", "", "x" * 41, "prod env"])
@pytest.mark.parametrize("keys", [{"LANGFUSE_PUBLIC_KEY": "pk", "LANGFUSE_SECRET_KEY": "sk"}, {}])
def test_t2_app_env_rejects_what_langfuse_would_drop(
    monkeypatch: pytest.MonkeyPatch, value: str, keys: dict[str, str]
) -> None:
    monkeypatch.setattr(tracing, "Langfuse", _Recorder)
    with pytest.raises(ValueError, match="APP_ENV"):
        tracing.build_client({**keys, "APP_ENV": value})


# ---------------------------------------------------------------------------
# T3, T4 — usage and cost
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("counts", "expected"),
    [
        ({}, {}),
        ({"prompt": 10, "completion": 4}, {"input": 10, "output": 4}),
        (
            {"prompt": 10, "completion": 4, "cached": 6},
            {"input": 4, "input_cached_tokens": 6, "output": 4},
        ),
        (
            {"prompt": 10, "completion": 4, "reasoning": 3},
            {"input": 10, "output": 1, "output_reasoning_tokens": 3},
        ),
        ({"prompt": 10, "completion": 4, "cached": 0, "reasoning": 0}, {"input": 10, "output": 4}),
        (
            {"prompt": 10, "completion": 4, "cached": 12, "reasoning": 9},
            {"input": 0, "input_cached_tokens": 10, "output": 0, "output_reasoning_tokens": 4},
        ),
        ({"completion": 4, "cached": 3}, {"output": 4}),
        ({"prompt": 7, "reasoning": 2}, {"input": 7}),
    ],
)
def test_t3_usage_details_are_exclusive_buckets(
    counts: dict[str, int], expected: dict[str, int]
) -> None:
    assert tracing.usage_details(**counts) == expected


def test_t4_cost_details() -> None:
    assert tracing.cost_details(0.0) == {"total": 0.0}
    assert tracing.cost_details(0.0012) == {"total": 0.0012}
    assert tracing.cost_details(None) is None


# ---------------------------------------------------------------------------
# T5, T6 — generations
# ---------------------------------------------------------------------------


async def test_t5_an_answered_attempt_is_one_generation(traces: Traces) -> None:
    usage = {
        "prompt_tokens": 120,
        "completion_tokens": 30,
        "total_tokens": 150,
        "cost": 0,
        "prompt_tokens_details": {"cached_tokens": 20},
        "completion_tokens_details": {"reasoning_tokens": 10},
    }
    with openrouter(_in_order(_ok(usage=usage, provider="Google AI Studio"))):
        result = await _call_llm(
            field_name="title",
            old_selector="span.old::text",
            html="<html></html>",
            old_samples=["Hello"],
            attempt=2,
        )
    assert result["selector"] == "div.item"
    gen = traces.one("propose-selector")
    assert traces.type_of(gen) == "generation"
    assert traces.level(gen) == "DEFAULT"
    assert traces.attr(gen, "langfuse.observation.model.name") == DEFAULT_MODEL
    assert traces.decoded(gen, "langfuse.observation.model.parameters") == {
        "temperature": 0.1,
        "response_format": "json_object",
    }
    messages = traces.decoded(gen, INPUT)
    assert [m["role"] for m in messages] == ["user"]
    assert "Field name: title" in messages[0]["content"]
    assert traces.decoded(gen, OUTPUT) == {
        "role": "assistant",
        "content": json.dumps(_proposal("div.item")),
    }
    assert traces.decoded(gen, "langfuse.observation.usage_details") == {
        "input": 100,
        "input_cached_tokens": 20,
        "output": 20,
        "output_reasoning_tokens": 10,
    }
    assert traces.decoded(gen, "langfuse.observation.cost_details") == {"total": 0.0}
    assert traces.metadata(gen) == {"tier": "primary", "attempt": 2, "provider": "Google AI Studio"}


async def test_t5_reasoning_is_kept_on_the_output(traces: Traces) -> None:
    body = {
        "choices": [
            {"message": {"content": json.dumps(_proposal("div.item")), "reasoning": "look first"}}
        ]
    }
    with openrouter(_in_order(httpx.Response(200, json=body))):
        await _call_llm(field_name="t", old_selector="s", html="<html></html>", old_samples=[])
    out = traces.decoded(traces.one("propose-selector"), OUTPUT)
    assert out["reasoning"] == "look first"
    assert traces.metadata(traces.one("propose-selector")) == {"tier": "primary", "attempt": 1}


@pytest.mark.parametrize(
    "usage",
    [
        '{"prompt_tokens": "10", "completion_tokens": 4.5, "cost": "0.1"}',
        '{"prompt_tokens": -3, "completion_tokens": true, "cost": true}',
        # Not JSON, but Python's parser (and so httpx's) reads these.
        '{"prompt_tokens": null, "completion_tokens": null, "cost": NaN}',
        '{"cost": Infinity}',
        '"not-a-mapping"',
        "[1, 2]",
    ],
)
async def test_t5_unreadable_usage_never_fails_the_call(traces: Traces, usage: str) -> None:
    content = json.dumps(json.dumps(_proposal("div.item")))
    body = '{"choices": [{"message": {"content": ' + content + '}}], "usage": ' + usage + "}"
    with openrouter(_in_order(httpx.Response(200, content=body.encode()))):
        result = await _call_llm(
            field_name="t", old_selector="s", html="<html></html>", old_samples=[]
        )
    assert result["selector"] == "div.item"
    gen = traces.one("propose-selector")
    assert traces.decoded(gen, "langfuse.observation.usage_details") is None
    assert traces.decoded(gen, "langfuse.observation.cost_details") is None


async def test_t6_a_retried_attempt_is_its_own_error_generation(traces: Traces) -> None:
    with openrouter(_in_order(httpx.Response(429), _ok())):
        result = await fix_selector(
            field_name="title", old_selector="s", html="<html></html>", old_samples=[]
        )
    assert result is not None
    first, second = traces.named("propose-selector")
    assert traces.level(first) == "ERROR"
    assert traces.status_message(first) == "rate_limited: status=429"
    assert traces.metadata(first) == {"tier": "primary", "attempt": 1}
    assert traces.level(second) == "DEFAULT"
    assert traces.metadata(second)["attempt"] == 2


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (httpx.Response(500), "server_error: status=500"),
        (httpx.Response(404), "client_error: status=404"),
        (
            httpx.Response(200, json=_reply('{"only_a_selector": "div"}')),
            "bad_response: ValidationError",
        ),
        (httpx.Response(200, json=_reply("not json")), "bad_response: JSONDecodeError"),
        (httpx.Response(200, json=_reply(None)), "bad_response: ValueError"),
        (httpx.Response(200, json={"choices": []}), "bad_response: ValueError"),
        (httpx.Response(200, json={"nothing": "here"}), "bad_response: ValueError"),
        (httpx.Response(200, text="<html>"), "bad_response: JSONDecodeError"),
        (
            httpx.Response(200, json={"id": "gen-1", "error": {"code": 502, "message": "down"}}),
            "server_error: status=502 in the reply body",
        ),
        (
            httpx.Response(200, json={"error": {"message": "no code"}}),
            "upstream_error: in the reply body",
        ),
    ],
)
async def test_t6_each_kind_of_failed_attempt_names_its_stage(
    traces: Traces, failure: httpx.Response, message: str
) -> None:
    with openrouter(_in_order(failure, _ok())):
        await fix_selector(
            field_name="title", old_selector="s", html="<html></html>", old_samples=[]
        )
    first = traces.named("propose-selector")[0]
    assert traces.level(first) == "ERROR"
    assert traces.status_message(first) == message


async def test_t6_an_error_inside_a_200_is_filed_under_its_code(traces: Traces) -> None:
    """Seen live on 2026-10-26: OpenRouter answered 200 with the provider's rate limit."""
    body = {
        "id": "gen-1793011066",
        "provider": "Phala",
        "error": {
            "code": 429,
            "message": "Rate limit exceeded",
            "metadata": {"error_type": "rate_limit_exceeded"},
        },
    }
    with openrouter(_in_order(httpx.Response(200, json=body), _ok())):
        result = await fix_selector(
            field_name="title", old_selector="s", html="<html></html>", old_samples=[]
        )
    assert result is not None
    first, second = traces.named("propose-selector")
    assert traces.level(first) == "ERROR"
    assert traces.status_message(first) == "rate_limited: status=429 in the reply body"
    assert traces.decoded(first, OUTPUT) == {
        "error": {"code": 429, "message": "Rate limit exceeded"}
    }
    assert traces.metadata(first) == {"tier": "primary", "attempt": 1, "provider": "Phala"}
    assert traces.level(second) == "DEFAULT"


async def test_t6_a_rejected_reply_is_still_on_the_generation(traces: Traces) -> None:
    usage = {"prompt_tokens": 50, "completion_tokens": 5}
    bad = httpx.Response(200, json=_reply('{"only_a_selector": "div"}', usage=usage))
    with openrouter(_in_order(bad, _ok())):
        await fix_selector(
            field_name="title", old_selector="s", html="<html></html>", old_samples=[]
        )
    first = traces.named("propose-selector")[0]
    assert traces.decoded(first, OUTPUT) == {
        "role": "assistant",
        "content": '{"only_a_selector": "div"}',
    }
    assert traces.decoded(first, "langfuse.observation.usage_details") == {
        "input": 50,
        "output": 5,
    }


async def test_t6_every_model_failing_is_one_error_generation_per_attempt(
    traces: Traces,
) -> None:
    statuses = (500, 502, 503, 504, 500, 502)
    with (
        openrouter(_in_order(*(httpx.Response(code) for code in statuses))),
        pytest.raises(selector_fixer.ModelsExhaustedError),
    ):
        await fix_selector(
            field_name="title", old_selector="s", html="<html></html>", old_samples=[]
        )
    gens = traces.named("propose-selector")
    assert [(traces.metadata(g)["tier"], traces.metadata(g)["attempt"]) for g in gens] == [
        ("primary", 1),
        ("primary", 2),
        ("fast", 1),
        ("fast", 2),
        ("fallback", 1),
        ("fallback", 2),
    ]
    assert [traces.attr(g, "langfuse.observation.model.name") for g in gens] == [
        selector_fixer.DEFAULT_MODEL,
        selector_fixer.DEFAULT_MODEL,
        selector_fixer.DEFAULT_FAST_MODEL,
        selector_fixer.DEFAULT_FAST_MODEL,
        selector_fixer.DEFAULT_FALLBACK_MODEL,
        selector_fixer.DEFAULT_FALLBACK_MODEL,
    ]
    assert [traces.status_message(g) for g in gens] == [
        f"server_error: status={code}" for code in statuses
    ]


async def test_t6_a_timeout_and_a_dropped_connection(traces: Traces) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with openrouter(handler), pytest.raises(httpx.ReadTimeout):
        await _call_llm(field_name="t", old_selector="s", html="<html></html>", old_samples=[])
    assert traces.status_message(traces.one("propose-selector")) == "timeout"

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with openrouter(refuse), pytest.raises(httpx.ConnectError):
        await _call_llm(field_name="t", old_selector="s", html="<html></html>", old_samples=[])
    assert traces.status_message(traces.named("propose-selector")[-1]) == "http_error: ConnectError"


# ---------------------------------------------------------------------------
# T7-T12 — a whole heal
# ---------------------------------------------------------------------------


async def test_t7_an_api_origin_container_heal_is_one_trace(
    traces: Traces, session_factory: Any
) -> None:
    name = await _seed(session_factory, BROKEN_CONTAINER_YAML, SourceOrigin.api)
    run_id = uuid.uuid4()
    with (
        patch("magpie.healer.apply._fetch_html", new=AsyncMock(return_value=ITEM_HTML)),
        openrouter(_in_order(_ok("div.item", usage={"prompt_tokens": 9, "completion_tokens": 3}))),
    ):
        summary = await heal_source(
            source=name, run_id=run_id, session_factory=session_factory, trigger="worker"
        )
    assert summary.succeeded

    spans = traces.spans()
    assert len({s.context.trace_id for s in spans}) == 1
    root = traces.one("heal-source")
    assert traces.parent(root) is None
    assert traces.type_of(root) == "agent"
    assert traces.level(root) == "DEFAULT"
    assert traces.decoded(root, INPUT) == {"source": name, "run_id": str(run_id)}
    assert traces.decoded(root, OUTPUT) == summary.model_dump(mode="json")

    fetch, step = traces.children(root)
    assert (fetch.name, traces.type_of(fetch)) == ("fetch-page", "tool")
    assert traces.decoded(fetch, INPUT) == {"url": "https://example.com/list", "render": False}
    assert traces.decoded(fetch, OUTPUT) == {"char_count": len(ITEM_HTML)}
    assert (step.name, traces.type_of(step)) == ("heal-selector", "agent")
    assert traces.decoded(step, INPUT) == {
        "target": "container",
        "old_selector": "div.card",
        "selector_type": "css",
        "mode": "db_patch",
    }
    assert traces.decoded(step, OUTPUT) == {
        "healed": True,
        "new_selector": "div.item",
        "mode": "db_patch",
        "pr_url": None,
        "applied": True,
    }
    kids = traces.children(step)
    assert [(k.name, traces.type_of(k)) for k in kids] == [
        ("propose-selector", "generation"),
        ("validate-selector", "evaluator"),
        ("patch-source-config", "tool"),
    ]
    check = kids[1]
    assert traces.decoded(check, INPUT) == {"selector": "div.item", "selector_type": "css"}
    out = traces.decoded(check, OUTPUT)
    assert out["accepted"] is True
    assert out["match_count"] == 2
    assert len(out["sample_values"]) == 2
    assert traces.decoded(kids[2], INPUT) == {
        "source": name,
        "target": "container",
        "new_selector": "div.item",
    }
    assert traces.decoded(kids[2], OUTPUT) == {"applied": True}

    for span in spans:
        assert traces.attr(span, "langfuse.trace.name") == "heal-source"
        assert traces.attr(span, "langfuse.trace.tags") == ("magpie", "trigger:worker")
        assert traces.attr(span, "langfuse.environment") == "test"
        assert traces.attr(span, "langfuse.trace.metadata.source") == json.dumps(name)
        assert traces.attr(span, "langfuse.trace.metadata.run_id") == json.dumps(str(run_id))
        assert traces.attr(span, "session.id") is None


async def test_t8_a_file_origin_field_heal_opens_a_pr(traces: Traces, session_factory: Any) -> None:
    name = await _seed(session_factory, BROKEN_FIELD_YAML, SourceOrigin.file)
    pr = "https://github.com/owner/repo/pull/42"
    with (
        patch("magpie.healer.apply._fetch_html", new=AsyncMock(return_value=CARD_HTML)),
        patch("magpie.healer.apply.create_heal_pr", new=AsyncMock(return_value=pr)),
        openrouter(_in_order(_ok("h2.headline::text"))),
    ):
        summary = await heal_source(
            source=name, run_id=None, session_factory=session_factory, trigger="cli"
        )
    assert summary.succeeded
    step = traces.one("heal-selector")
    assert traces.decoded(step, INPUT)["target"] == "title"
    assert [k.name for k in traces.children(step)] == [
        "propose-selector",
        "validate-selector",
        "open-heal-pr",
    ]
    tool = traces.one("open-heal-pr")
    assert traces.type_of(tool) == "tool"
    assert traces.decoded(tool, INPUT) == {
        "source": name,
        "target": "title",
        "file_path": f"configs/{name}.yaml",
        "new_selector": "h2.headline::text",
    }
    assert traces.decoded(tool, OUTPUT) == {"pr_url": pr}
    assert {traces.level(s) for s in traces.spans()} == {"DEFAULT"}
    root = traces.one("heal-source")
    assert traces.decoded(root, INPUT) == {"source": name, "run_id": None}
    assert traces.attr(root, "langfuse.trace.tags") == ("magpie", "trigger:cli")
    assert traces.attr(root, "langfuse.trace.metadata.run_id") is None


async def test_t9_a_selector_that_matches_nothing_is_rejected(
    traces: Traces, session_factory: Any
) -> None:
    name = await _seed(session_factory, BROKEN_CONTAINER_YAML, SourceOrigin.api)
    with (
        patch("magpie.healer.apply._fetch_html", new=AsyncMock(return_value=ITEM_HTML)),
        openrouter(_in_order(_ok("section.nothing"))),
    ):
        summary = await heal_source(
            source=name, run_id=None, session_factory=session_factory, trigger="worker"
        )
    assert not summary.succeeded
    check = traces.one("validate-selector")
    assert traces.level(check) == "DEFAULT"
    assert traces.decoded(check, OUTPUT) == {
        "accepted": False,
        "match_count": 0,
        "sample_values": [],
    }
    step = traces.one("heal-selector")
    assert traces.level(step) == "ERROR"
    reason = "selector rejected: 'section.nothing' matches nothing on the live page"
    assert traces.status_message(step) == reason
    assert traces.decoded(step, OUTPUT) == {"healed": False, "reason": reason}
    root = traces.one("heal-source")
    assert traces.level(root) == "ERROR"
    assert traces.status_message(root) == f"container: {reason}"
    assert [k.name for k in traces.children(step)] == ["propose-selector", "validate-selector"]


async def test_t9_a_selector_that_does_not_compile_is_a_rejection_not_an_error(
    traces: Traces, session_factory: Any
) -> None:
    name = await _seed(session_factory, BROKEN_CONTAINER_YAML, SourceOrigin.api)
    with (
        patch("magpie.healer.apply._fetch_html", new=AsyncMock(return_value=ITEM_HTML)),
        openrouter(_in_order(_ok("div["))),
    ):
        summary = await heal_source(
            source=name, run_id=None, session_factory=session_factory, trigger="worker"
        )
    assert not summary.succeeded
    check = traces.one("validate-selector")
    assert traces.level(check) == "DEFAULT"
    output = traces.decoded(check, OUTPUT)
    assert output["accepted"] is False
    assert output["match_count"] == 0
    assert output["invalid"].startswith("'div[' is not a valid CSS selector")
    step = traces.one("heal-selector")
    assert traces.level(step) == "ERROR"
    assert str(traces.status_message(step)).startswith(
        "selector rejected: 'div[' is not a valid CSS selector"
    )


async def test_t10_a_model_that_never_answers_is_an_llm_error(
    traces: Traces, session_factory: Any
) -> None:
    name = await _seed(session_factory, BROKEN_CONTAINER_YAML, SourceOrigin.api)
    with (
        patch("magpie.healer.apply._fetch_html", new=AsyncMock(return_value=ITEM_HTML)),
        openrouter(_in_order(*(httpx.Response(500) for _ in range(6)))),
    ):
        summary = await heal_source(
            source=name, run_id=None, session_factory=session_factory, trigger="worker"
        )
    assert not summary.succeeded
    step = traces.one("heal-selector")
    assert traces.level(step) == "ERROR"
    assert str(traces.status_message(step)).startswith(
        "LLM error: ModelsExhaustedError: primary=server_error: status=500"
    )
    gens = traces.children(step)
    assert _names(gens) == ["propose-selector"] * 6
    assert {traces.level(g) for g in gens} == {"ERROR"}
    assert traces.level(traces.one("heal-source")) == "ERROR"


async def test_t11_a_pr_that_does_not_open_is_an_error(
    traces: Traces, session_factory: Any
) -> None:
    name = await _seed(session_factory, BROKEN_FIELD_YAML, SourceOrigin.file)
    with (
        patch("magpie.healer.apply._fetch_html", new=AsyncMock(return_value=CARD_HTML)),
        patch("magpie.healer.apply.create_heal_pr", new=AsyncMock(return_value=None)),
        openrouter(_in_order(_ok("h2.headline::text"))),
    ):
        summary = await heal_source(
            source=name, run_id=None, session_factory=session_factory, trigger="worker"
        )
    assert not summary.succeeded
    tool = traces.one("open-heal-pr")
    assert traces.level(tool) == "ERROR"
    assert traces.status_message(tool) == "PR not opened"
    assert traces.decoded(tool, OUTPUT) == {"pr_url": None}
    step = traces.one("heal-selector")
    assert traces.level(step) == "ERROR"
    assert str(traces.status_message(step)).startswith("PR not opened for validated selector")


async def test_t12_a_failed_fetch_is_an_error_tool(traces: Traces, session_factory: Any) -> None:
    name = await _seed(session_factory, BROKEN_CONTAINER_YAML, SourceOrigin.api)
    refused = httpx.ConnectError("refused")
    with patch("magpie.healer.apply._fetch_html", new=AsyncMock(side_effect=refused)):
        summary = await heal_source(
            source=name, run_id=None, session_factory=session_factory, trigger="worker"
        )
    assert summary.failures[0].reason == "fetch failed: refused"
    root = traces.one("heal-source")
    fetch = traces.one("fetch-page")
    assert traces.parent(fetch) == root
    assert traces.level(fetch) == "ERROR"
    assert "refused" in str(traces.status_message(fetch))
    assert traces.level(root) == "ERROR"
    assert traces.status_message(root) == "source: fetch failed: refused"
    assert traces.children(root) == [fetch]


async def test_t12_an_unknown_source_is_a_lone_error_root(
    traces: Traces, session_factory: Any
) -> None:
    await heal_source(
        source="ghost", run_id=None, session_factory=session_factory, trigger="worker"
    )
    root = traces.one("heal-source")
    assert traces.children(root) == []
    assert traces.level(root) == "ERROR"
    assert traces.status_message(root) == "source: source not found"


# ---------------------------------------------------------------------------
# T13 — tracing never changes a heal
# ---------------------------------------------------------------------------


class _BrokenExporter(SpanExporter):
    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        raise RuntimeError("langfuse is down")

    def shutdown(self) -> None:
        return None


async def test_t13_an_exporter_failure_never_changes_the_heal(
    monkeypatch: pytest.MonkeyPatch, session_factory: Any
) -> None:
    client = tracing_client(_BrokenExporter())
    monkeypatch.setattr(tracing, "_client", client)
    name = await _seed(session_factory, BROKEN_CONTAINER_YAML, SourceOrigin.api)
    with (
        patch("magpie.healer.apply._fetch_html", new=AsyncMock(return_value=ITEM_HTML)),
        openrouter(_in_order(_ok("div.item"))),
    ):
        summary = await heal_source(
            source=name, run_id=None, session_factory=session_factory, trigger="worker"
        )
    client.flush()
    client.shutdown()
    assert summary.succeeded
    async with session_factory() as session:
        row = await SourcesRepository(session).get_by_name(name)
        assert row is not None
        assert "div.item" in row.config_yaml


# ---------------------------------------------------------------------------
# T14 — the heal job and the queue task
# ---------------------------------------------------------------------------


def _healed(source: str) -> HealSummary:
    return HealSummary(
        source=source,
        origin=SourceOrigin.api,
        healed=(
            HealedTarget(
                target="container",
                mode=HealMode.db_patch,
                applied=True,
                pr_url=None,
                new_selector="div.item",
            ),
        ),
    )


def test_t14_the_heal_job_builds_the_client_first_and_shuts_it_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def heal() -> int:
        calls.append("heal")
        return 0

    monkeypatch.setattr(tracing, "client", lambda: calls.append("client"))
    monkeypatch.setattr(tracing, "shutdown", lambda: calls.append("shutdown"))
    monkeypatch.setattr(heal_cli, "_main", heal)
    assert heal_cli.main([]) == 0
    assert calls == ["client", "heal", "shutdown"]


def test_t14_the_heal_job_shuts_down_when_the_heal_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def heal() -> int:
        raise RuntimeError("database down")

    monkeypatch.setattr(tracing, "client", lambda: None)
    monkeypatch.setattr(tracing, "shutdown", lambda: calls.append("shutdown"))
    monkeypatch.setattr(heal_cli, "_main", heal)
    with pytest.raises(RuntimeError, match="database down"):
        heal_cli.main([])
    assert calls == ["shutdown"]


def test_t14_a_bad_app_env_stops_the_heal_job(monkeypatch: pytest.MonkeyPatch) -> None:
    heal = AsyncMock(return_value=0)
    monkeypatch.setattr(tracing, "_client", None)
    monkeypatch.setenv("APP_ENV", "Production")
    monkeypatch.setattr(heal_cli, "_main", heal)
    with pytest.raises(ValueError, match="APP_ENV"):
        heal_cli.main([])
    heal.assert_not_called()


async def test_t14_the_heal_job_heals_with_trigger_cli(
    monkeypatch: pytest.MonkeyPatch, session_factory: Any
) -> None:
    heal = AsyncMock(return_value=_healed("src"))
    monkeypatch.setattr(heal_cli, "heal_source", heal)
    monkeypatch.setattr(heal_cli, "get_session_factory", lambda: session_factory)
    assert await heal_cli._heal_one_source("src") == 0
    assert heal.call_args.kwargs["trigger"] == "cli"


async def test_t14_the_queue_task_heals_with_trigger_worker(
    monkeypatch: pytest.MonkeyPatch, session_factory: Any
) -> None:
    from magpie.queue import tasks
    from magpie.queue.tasks import heal_source_task

    heal = AsyncMock(return_value=_healed("src"))
    monkeypatch.setattr("magpie.healer.apply.heal_source", heal)
    monkeypatch.setattr(tasks, "get_session_factory", lambda: session_factory)
    await heal_source_task(source="src", run_id=None)
    assert heal.call_args.kwargs["trigger"] == "worker"


# ---------------------------------------------------------------------------
# T15 — the app lifespan
# ---------------------------------------------------------------------------


@pytest.fixture
def quiet_lifespan(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The lifespan with no database and no worker: only the tracing calls are real."""
    calls: list[str] = []

    async def no_sync() -> int:
        return 0

    async def nothing(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(lifespan_module, "_sync_file_sources_to_db", no_sync)
    monkeypatch.setattr(lifespan_module.queue_app, "open_async", nothing)
    monkeypatch.setattr(lifespan_module.queue_app, "close_async", nothing)
    monkeypatch.setattr(lifespan_module.queue_app, "run_worker_async", nothing)
    return calls


async def test_t15_the_lifespan_builds_tracing_at_startup_and_shuts_it_down(
    monkeypatch: pytest.MonkeyPatch, quiet_lifespan: list[str]
) -> None:
    monkeypatch.setattr(tracing, "client", lambda: quiet_lifespan.append("client"))
    monkeypatch.setattr(tracing, "shutdown", lambda: quiet_lifespan.append("shutdown"))
    async with lifespan_module.magpie_lifespan(object()):  # type: ignore[arg-type]
        assert quiet_lifespan == ["client"]
    assert quiet_lifespan == ["client", "shutdown"]


async def test_t15_a_bad_app_env_stops_the_boot(
    monkeypatch: pytest.MonkeyPatch, quiet_lifespan: list[str]
) -> None:
    monkeypatch.setattr(tracing, "_client", None)
    monkeypatch.setenv("APP_ENV", "langfuse-prod")
    with pytest.raises(ValueError, match="APP_ENV"):
        async with lifespan_module.magpie_lifespan(object()):  # type: ignore[arg-type]
            pass  # pragma: no cover - the boot stops before the app serves
