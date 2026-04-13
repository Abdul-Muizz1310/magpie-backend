"""Tests for selector_fixer._call_llm and retry exhaustion."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from magpie.healer.selector_fixer import _call_llm, fix_selector


class TestCallLlm:
    @pytest.mark.asyncio
    async def test_call_llm_returns_parsed_json(self) -> None:
        """Successful LLM call returns parsed JSON."""
        llm_response = {
            "choices": [
                {
                    "message": {
                        "content": '{"selector": "div.new", "confidence": 0.9, "reasoning": "ok", "sample_values": ["a"]}'
                    }
                }
            ]
        }
        mock_resp = httpx.Response(
            200,
            json=llm_response,
            request=httpx.Request("POST", "http://test"),
        )

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("magpie.healer.selector_fixer.httpx.AsyncClient", return_value=mock_ctx),
            patch.dict("os.environ", {"OPENROUTER_API_KEY": "fake-key"}),
        ):
            result = await _call_llm(
                field_name="title",
                old_selector="span.old::text",
                html="<html></html>",
                old_samples=["Sample"],
            )

        assert result["selector"] == "div.new"
        assert result["confidence"] == 0.9

    @pytest.mark.asyncio
    async def test_call_llm_raises_on_http_error(self) -> None:
        """HTTP error from LLM raises."""
        mock_resp = httpx.Response(
            500,
            json={"error": "internal"},
            request=httpx.Request("POST", "http://test"),
        )

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("magpie.healer.selector_fixer.httpx.AsyncClient", return_value=mock_ctx),
            patch.dict("os.environ", {"OPENROUTER_API_KEY": "fake-key"}),
            pytest.raises(httpx.HTTPStatusError),
        ):
            await _call_llm(
                field_name="title",
                old_selector="span.old::text",
                html="<html></html>",
                old_samples=[],
            )


class TestFixSelectorRetryExhaustion:
    @pytest.mark.asyncio
    async def test_all_retries_fail_raises_last_error(self) -> None:
        """When all 3 retries fail, the last error is raised."""
        with (
            patch(
                "magpie.healer.selector_fixer._call_llm",
                new_callable=AsyncMock,
                side_effect=ValueError("always fails"),
            ),
            pytest.raises(ValueError, match="always fails"),
        ):
            await fix_selector(
                field_name="title",
                old_selector="span.old::text",
                html="<html></html>",
                old_samples=[],
            )

    @pytest.mark.asyncio
    async def test_zero_retries_returns_none(self) -> None:
        """When MAX_RETRIES is 0, loop doesn't run and returns None (line 46)."""
        with patch("magpie.healer.selector_fixer.MAX_RETRIES", 0):
            result = await fix_selector(
                field_name="title",
                old_selector="span.old::text",
                html="<html></html>",
                old_samples=[],
            )
            assert result is None
