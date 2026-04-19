"""Shared helpers that shape ``Item`` rows into API responses.

Both the run-scoped endpoint (``GET /api/runs/{id}/items``) and the
source-scoped endpoint (``GET /sources/{name}/items``) return the same
``RunItemView`` shape, so the conversion logic lives here instead of being
duplicated per router.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin

import yaml

from magpie.schemas.jobs import RunItemView
from magpie.storage.models import Item, Source


def derive_content_text(data: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("title", "content", "body", "summary"):
        val = data.get(key)
        if isinstance(val, str) and val:
            parts.append(val)
    if not parts:
        for key in sorted(data.keys()):
            if key in ("id", "url", "link", "href", "html_snapshot_url"):
                continue
            val = data.get(key)
            if isinstance(val, str) and val:
                parts.append(val)
    return "\n".join(parts)


def extract_url(data: dict[str, Any]) -> str:
    """Pick the first non-empty URL-ish value from the scraped item dict.

    Different source configs name the URL field differently — arxiv-cs uses
    ``link``, most others use ``url`` — so the stored ``data`` blob can carry
    the link under any of these keys.
    """
    for key in ("url", "link", "href"):
        val = data.get(key)
        if isinstance(val, str) and val:
            return val
    return ""


def resolve_url(raw: str, base: str | None) -> str:
    """Resolve a possibly-relative URL against the source's configured base.

    Sites link internally with root-relative paths (e.g. huggingface.co
    serves ``/papers/<id>``); we need them absolute so clicking a scraped
    item opens the site, not the magpie frontend.
    """
    if not raw or not base:
        return raw
    if raw.startswith(("http://", "https://", "mailto:", "data:")):
        return raw
    return urljoin(base, raw)


def source_base_url(source: Source | None) -> str | None:
    if source is None or not source.config_yaml:
        return None
    try:
        parsed = yaml.safe_load(source.config_yaml)
    except yaml.YAMLError:
        return None
    if not isinstance(parsed, dict):
        return None
    url = parsed.get("url")
    return url if isinstance(url, str) and url else None


def item_view(item: Item, *, source_base: str | None = None) -> RunItemView:
    data = item.data or {}
    url_val = extract_url(data)
    title_val = data.get("title")
    snapshot = data.get("html_snapshot_url")
    return RunItemView(
        id=item.id,
        stable_id=item.dedupe_key,
        url=resolve_url(url_val, source_base),
        title=str(title_val) if title_val else "",
        content_text=derive_content_text(data),
        content_hash=item.content_hash,
        first_seen_at=item.first_seen_at,
        last_seen_at=item.last_seen_at,
        html_snapshot_url=(
            resolve_url(str(snapshot), source_base) if isinstance(snapshot, str) else None
        ),
        data=dict(data),
    )
