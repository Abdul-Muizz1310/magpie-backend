"""Tests for custom-source CRUD endpoints."""

from __future__ import annotations

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

from magpie.api.deps import get_db_session, get_session_factory_dep
from magpie.config.schema import SourceConfig
from magpie.main import app
from magpie.storage.models import SourceOrigin
from magpie.storage.sources_repo import SourcesRepository

VALID_YAML = """\
name: my-custom
url: https://example.com
schedule: "0 */6 * * *"
item:
  container: "div.card"
  fields:
    - { name: title, selector: "h2::text" }
    - { name: id, selector: "::attr(data-id)" }
  dedupe_key: id
"""


def _variant_yaml(name: str) -> str:
    return VALID_YAML.replace("my-custom", name)


@pytest.fixture
async def client(session_factory):
    async def _factory_override():
        return session_factory

    async def _session_override():
        async with session_factory() as s:
            yield s

    app.dependency_overrides[get_session_factory_dep] = _factory_override
    app.dependency_overrides[get_db_session] = _session_override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def _seed(session_factory, name: str, origin: SourceOrigin) -> None:
    cfg = SourceConfig(**yaml.safe_load(_variant_yaml(name)))
    async with session_factory() as session:
        await SourcesRepository(session).create(
            config=cfg, origin=origin, yaml_text=_variant_yaml(name)
        )
        await session.commit()


class TestCreateSource:
    async def test_yaml_body_creates_source(self, client: AsyncClient) -> None:
        resp = await client.post("/api/sources", json={"yaml": VALID_YAML})
        assert resp.status_code == 201
        body = resp.json()
        assert body["name"] == "my-custom"
        assert body["origin"] == "api"
        assert body["config_sha"]

    async def test_json_config_body_creates_source(self, client: AsyncClient) -> None:
        cfg_dict = yaml.safe_load(VALID_YAML)
        resp = await client.post("/api/sources", json={"config": cfg_dict})
        assert resp.status_code == 201

    async def test_empty_body_422(self, client: AsyncClient) -> None:
        resp = await client.post("/api/sources", json={})
        assert resp.status_code == 422

    async def test_malformed_yaml_422(self, client: AsyncClient) -> None:
        resp = await client.post("/api/sources", json={"yaml": "{{ not valid"})
        assert resp.status_code == 422

    async def test_invalid_selector_422(self, client: AsyncClient) -> None:
        bad = VALID_YAML.replace(
            'selector: "h2::text"',
            'selector: "h2[[[broken"',
        )
        resp = await client.post("/api/sources", json={"yaml": bad})
        assert resp.status_code == 422

    async def test_duplicate_name_409(self, client: AsyncClient, session_factory) -> None:
        await _seed(session_factory, "my-custom", SourceOrigin.api)
        resp = await client.post("/api/sources", json={"yaml": VALID_YAML})
        assert resp.status_code == 409

    @pytest.mark.parametrize(
        "bad_url",
        [
            "http://localhost/",
            "http://127.0.0.1/",
            "http://169.254.169.254/latest/meta-data/",
            "http://10.0.0.5/",
            "http://example.internal/",
        ],
    )
    async def test_ssrf_guard_rejects_private_url(self, client: AsyncClient, bad_url: str) -> None:
        attack = VALID_YAML.replace("https://example.com", bad_url)
        resp = await client.post("/api/sources", json={"yaml": attack})
        assert resp.status_code == 422
        assert "not allowed" in resp.json()["detail"]

    async def test_xpath_source_accepted(self, client: AsyncClient) -> None:
        xpath_yaml = """\
name: xpath-src
url: https://example.com
schedule: "0 */6 * * *"
item:
  container: "//div[@class='card']"
  container_type: xpath
  fields:
    - { name: title, selector: ".//h2/text()", selector_type: xpath }
    - { name: id, selector: "./@data-id", selector_type: xpath }
  dedupe_key: id
"""
        resp = await client.post("/api/sources", json={"yaml": xpath_yaml})
        assert resp.status_code == 201


class TestListAndGetSources:
    async def test_list_returns_all_by_default(self, client: AsyncClient, session_factory) -> None:
        await _seed(session_factory, "api-1", SourceOrigin.api)
        await _seed(session_factory, "file-1", SourceOrigin.file)
        resp = await client.get("/api/sources")
        assert resp.status_code == 200
        names = {s["name"] for s in resp.json()}
        assert names == {"api-1", "file-1"}

    async def test_list_filters_by_origin(self, client: AsyncClient, session_factory) -> None:
        await _seed(session_factory, "api-1", SourceOrigin.api)
        await _seed(session_factory, "file-1", SourceOrigin.file)
        resp = await client.get("/api/sources?origin=api")
        names = {s["name"] for s in resp.json()}
        assert names == {"api-1"}

    async def test_get_by_name(self, client: AsyncClient, session_factory) -> None:
        await _seed(session_factory, "x", SourceOrigin.api)
        resp = await client.get("/api/sources/x")
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "x"
        assert "config_yaml" in body

    async def test_get_missing_404(self, client: AsyncClient) -> None:
        resp = await client.get("/api/sources/ghost")
        assert resp.status_code == 404


class TestUpdateAndDelete:
    async def test_patch_api_origin_updates(self, client: AsyncClient, session_factory) -> None:
        await _seed(session_factory, "my-custom", SourceOrigin.api)
        new_yaml = VALID_YAML.replace("example.com", "updated.example.com")
        resp = await client.patch("/api/sources/my-custom", json={"yaml": new_yaml})
        assert resp.status_code == 200
        assert "updated.example.com" in resp.json()["config_yaml"]

    async def test_patch_file_origin_409(self, client: AsyncClient, session_factory) -> None:
        await _seed(session_factory, "locked", SourceOrigin.file)
        new_yaml = _variant_yaml("locked").replace("example.com", "changed.com")
        resp = await client.patch("/api/sources/locked", json={"yaml": new_yaml})
        assert resp.status_code == 409

    async def test_patch_missing_404(self, client: AsyncClient) -> None:
        resp = await client.patch("/api/sources/ghost", json={"yaml": _variant_yaml("ghost")})
        assert resp.status_code == 404

    async def test_patch_path_name_mismatch_422(self, client: AsyncClient, session_factory) -> None:
        await _seed(session_factory, "a", SourceOrigin.api)
        resp = await client.patch("/api/sources/a", json={"yaml": _variant_yaml("b")})
        assert resp.status_code == 422

    async def test_delete_api_origin(self, client: AsyncClient, session_factory) -> None:
        await _seed(session_factory, "deletable", SourceOrigin.api)
        resp = await client.delete("/api/sources/deletable")
        assert resp.status_code == 204
        resp = await client.get("/api/sources/deletable")
        assert resp.status_code == 404

    async def test_delete_file_origin_409(self, client: AsyncClient, session_factory) -> None:
        await _seed(session_factory, "locked", SourceOrigin.file)
        resp = await client.delete("/api/sources/locked")
        assert resp.status_code == 409

    async def test_delete_missing_404(self, client: AsyncClient) -> None:
        resp = await client.delete("/api/sources/ghost")
        assert resp.status_code == 404
