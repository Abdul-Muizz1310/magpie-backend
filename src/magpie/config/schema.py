"""Pydantic models for YAML scraper config."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


class FieldDef(BaseModel):
    """A single field to extract from each container element."""

    model_config = ConfigDict(extra="forbid")

    name: str
    selector: str
    attr: str | None = None


class ItemDef(BaseModel):
    """Defines how to locate and extract items from a page."""

    model_config = ConfigDict(extra="forbid")

    container: str
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


class PaginationDef(BaseModel):
    """Pagination configuration."""

    model_config = ConfigDict(extra="forbid")

    next: str | None = None
    max_pages: int = Field(default=1, ge=1)


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
