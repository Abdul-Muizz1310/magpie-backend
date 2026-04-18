"""Pydantic models for YAML scraper config."""

from __future__ import annotations

from typing import Literal

from parsel import Selector as _ParselSelector
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

SelectorType = Literal["css", "xpath"]

_VALIDATION_HTML = "<html><body></body></html>"


def _validate_selector(selector: str, selector_type: SelectorType) -> None:
    """Compile the selector against an empty document so syntax errors surface at load time.

    parsel raises at the ``.css()``/``.xpath()`` call when a selector's syntax is
    malformed, so we can reuse the same library that extraction uses without
    pulling in ``cssselect`` or ``lxml`` directly. An empty-match result means
    the selector is syntactically valid, which is all we can check without a
    real document.
    """
    try:
        sel = _ParselSelector(text=_VALIDATION_HTML)
        if selector_type == "css":
            sel.css(selector)
        else:
            sel.xpath(selector)
    except Exception as exc:
        msg = f"Invalid {selector_type} selector {selector!r}: {exc}"
        raise ValueError(msg) from exc


class FieldDef(BaseModel):
    """A single field to extract from each container element."""

    model_config = ConfigDict(extra="forbid")

    name: str
    selector: str
    selector_type: SelectorType = "css"
    attr: str | None = None

    @model_validator(mode="after")
    def _selector_compiles(self) -> FieldDef:
        _validate_selector(self.selector, self.selector_type)
        return self


class ItemDef(BaseModel):
    """Defines how to locate and extract items from a page."""

    model_config = ConfigDict(extra="forbid")

    container: str
    container_type: SelectorType = "css"
    fields: list[FieldDef] = Field(min_length=1)
    dedupe_key: str

    @model_validator(mode="after")
    def _dedupe_key_must_be_a_field(self) -> ItemDef:
        field_names = [f.name for f in self.fields]
        if self.dedupe_key not in field_names:
            msg = (
                f"dedupe_key '{self.dedupe_key}' must reference one of the field names: "
                f"{field_names}"
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _no_duplicate_field_names(self) -> ItemDef:
        names = [f.name for f in self.fields]
        if len(names) != len(set(names)):
            dupes = [n for n in names if names.count(n) > 1]
            msg = f"Duplicate field names: {set(dupes)}"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _container_compiles(self) -> ItemDef:
        _validate_selector(self.container, self.container_type)
        return self


class PaginationDef(BaseModel):
    """Pagination configuration."""

    model_config = ConfigDict(extra="forbid")

    next: str | None = None
    next_type: SelectorType = "css"
    max_pages: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def _next_compiles(self) -> PaginationDef:
        if self.next is not None:
            _validate_selector(self.next, self.next_type)
        return self


class ActionDef(BaseModel):
    """A browser action for JS-rendered pages."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["click", "wait", "scroll", "type"]
    selector: str | None = None
    ms: int | None = None
    text: str | None = None

    @model_validator(mode="after")
    def _validate_action_fields(self) -> ActionDef:
        if self.type in ("click", "type") and self.selector is None:
            msg = f"Action type '{self.type}' requires 'selector'"
            raise ValueError(msg)
        if self.type == "wait" and self.ms is None:
            msg = "Action type 'wait' requires 'ms'"
            raise ValueError(msg)
        return self


class HealthDef(BaseModel):
    """Health check thresholds for a source."""

    model_config = ConfigDict(extra="forbid")

    min_items: int = Field(default=1, ge=0)
    max_staleness: str = "24h"


class RateLimitDef(BaseModel):
    """Rate limiting configuration."""

    model_config = ConfigDict(extra="forbid")

    rps: int = Field(default=1, ge=1)


class SourceConfig(BaseModel):
    """Top-level config for a single scrape source."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z0-9-]+$", min_length=1)
    description: str = ""
    url: HttpUrl
    render: bool = False
    schedule: str
    rate_limit: RateLimitDef = RateLimitDef()
    item: ItemDef
    pagination: PaginationDef = PaginationDef()
    wait_for: str | None = None
    actions: list[ActionDef] = []
    health: HealthDef = HealthDef()

    @model_validator(mode="after")
    def _static_must_not_have_js_fields(self) -> SourceConfig:
        if not self.render:
            if self.actions:
                msg = "Static config (render=false) must not have 'actions'"
                raise ValueError(msg)
            if self.wait_for is not None:
                msg = "Static config (render=false) must not have 'wait_for'"
                raise ValueError(msg)
        return self
