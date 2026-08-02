"""Heal-rate eval driver (spec 07).

Everything except the *proposal* step is the code that ships: the scraper's own
extractor, the healer's threshold detector, its validator, and its YAML patcher.
The proposal step is ``evals.offline_proposer`` so the number reproduces offline.

Pure core / imperative shell: ``detect_breakage``, ``field_samples``,
``heal_offline`` and ``evaluate_case`` are pure functions over strings.
``load_cases`` and ``run_suite`` are the thin file-reading shell around them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from magpie.config.schema import SourceConfig
from magpie.evals.offline_proposer import (
    parse_accessor,
    propose_container_selector,
    propose_field_selector,
)
from magpie.healer.apply import CONTAINER_TARGET, broken_field_names, patched_yaml
from magpie.healer.detector import should_heal
from magpie.healer.validator import validate_selector
from magpie.scrapy.factory import _extract_items_from_html

BreakageKind = Literal["none", "container", "fields", "underflow"]

DEFAULT_MIN_ITEMS = 1


class InvalidEvalCaseError(ValueError):
    """A case does not describe a real before/after breakage.

    Loud on purpose: a case whose ``after`` fixture still extracts fine would
    count as a free heal and quietly inflate the published rate.
    """


class EmptyEvalSuiteError(ValueError):
    """No cases at all — 0/0 is not a heal rate."""


@dataclass(frozen=True)
class Breakage:
    """What is wrong with a config/HTML pairing, in the healer's own terms."""

    kind: BreakageKind
    item_count: int
    fields: tuple[str, ...] = ()

    @property
    def is_broken(self) -> bool:
        return self.kind != "none"


@dataclass(frozen=True)
class Proposal:
    """One selector the offline proposer replaced."""

    target: str
    """``"container"`` or a field name."""

    old_selector: str
    new_selector: str


@dataclass(frozen=True)
class HealAttempt:
    config: SourceConfig
    """The config after every accepted proposal was patched in."""

    proposals: tuple[Proposal, ...]


@dataclass(frozen=True)
class CaseSpec:
    name: str
    description: str
    config_path: Path
    before_path: Path
    after_path: Path
    min_items: int = DEFAULT_MIN_ITEMS
    expect_healable: bool = True


@dataclass(frozen=True)
class CaseResult:
    name: str
    description: str
    breakage_kind: BreakageKind
    broken_fields: tuple[str, ...]
    items_before: int
    items_broken: int
    items_healed: int
    healed: bool
    expect_healable: bool
    proposals: tuple[Proposal, ...] = field(default_factory=tuple)

    @property
    def matches_expectation(self) -> bool:
        return self.healed == self.expect_healable


@dataclass(frozen=True)
class HealRateReport:
    cases: tuple[CaseResult, ...]

    def __post_init__(self) -> None:
        if not self.cases:
            raise EmptyEvalSuiteError("heal-rate eval needs at least one case")

    @property
    def total(self) -> int:
        return len(self.cases)

    @property
    def healed(self) -> int:
        return sum(1 for case in self.cases if case.healed)

    @property
    def heal_rate(self) -> float:
        return self.healed / self.total

    @property
    def heal_rate_pct(self) -> float:
        return round(self.heal_rate * 100, 2)

    @property
    def expectation_failures(self) -> tuple[str, ...]:
        return tuple(case.name for case in self.cases if not case.matches_expectation)

    def as_dict(self) -> dict[str, Any]:
        """JSON-serialisable summary — the shape committed to evals/heal_rate.json."""
        return {
            "total_cases": self.total,
            "healed_cases": self.healed,
            "heal_rate_pct": self.heal_rate_pct,
            "cases": [
                {
                    "name": case.name,
                    "description": case.description,
                    "breakage": case.breakage_kind,
                    "broken_fields": list(case.broken_fields),
                    "items_before": case.items_before,
                    "items_when_broken": case.items_broken,
                    "items_after_heal": case.items_healed,
                    "healed": case.healed,
                    "expect_healable": case.expect_healable,
                    "proposals": [
                        {
                            "target": p.target,
                            "old_selector": p.old_selector,
                            "new_selector": p.new_selector,
                        }
                        for p in case.proposals
                    ],
                }
                for case in self.cases
            ],
        }


# ── Pure core ────────────────────────────────────────────────────────────────


def detect_breakage(config: SourceConfig, html: str, *, min_items: int) -> Breakage:
    """Classify a config/HTML pairing exactly the way ``healer.apply`` does.

    Precedence mirrors the healer: no items at all means the container selector
    died; otherwise a field that is ``None`` in *every* item is a field break;
    otherwise too few items is an underflow the healer cannot fix by re-selecting.
    """
    items = _extract_items_from_html(html, config)
    if not items:
        return Breakage(kind="container", item_count=0)
    broken = tuple(broken_field_names(config=config, raw_items=items))
    if broken:
        return Breakage(kind="fields", item_count=len(items), fields=broken)
    if should_heal(item_count=len(items), min_items=min_items):
        return Breakage(kind="underflow", item_count=len(items))
    return Breakage(kind="none", item_count=len(items))


def field_samples(config: SourceConfig, html: str) -> dict[str, list[str]]:
    """Values each field actually extracted from ``html`` — the heal anchors.

    Fields that extracted nothing are omitted rather than mapped to an empty
    list, so a caller cannot mistake "no data" for "extracted blanks".
    """
    items = _extract_items_from_html(html, config)
    samples: dict[str, list[str]] = {}
    for spec in config.item.fields:
        values = [str(item[spec.name]) for item in items if item.get(spec.name) is not None]
        if values:
            samples[spec.name] = values
    return samples


def _patched(config: SourceConfig, *, target: str, new_selector: str) -> SourceConfig:
    """Round-trip the fix through the healer's own YAML patcher.

    Going via YAML (rather than mutating the model) is deliberate: it proves the
    proposed selector survives ``SourceConfig`` re-validation, which is what the
    real heal writes to disk or to ``sources.config_yaml``.
    """
    text = patched_yaml(original_config=config, target=target, new_selector=new_selector)
    return SourceConfig(**yaml.safe_load(text))


def _field_candidate_validates(*, html: str, config: SourceConfig, candidate: str) -> bool:
    """Second gate: the composed document-level selector must still match.

    The proposer already checked the candidate resolves inside the containers;
    this runs the healer's ``validate_selector`` on ``"<container> <fragment>"`` so
    the same validator that guards a real heal also guards an eval heal. A
    container-relative candidate (``::attr(id)``) has no composable document form,
    so it passes on the container gate alone.
    """
    fragment, accessor = parse_accessor(candidate)
    if not fragment or config.item.container_type != "css":
        return True
    composed = accessor.compose(f"{config.item.container} {fragment}")
    return validate_selector(html, composed) != []


def heal_offline(*, config: SourceConfig, before_html: str, after_html: str) -> HealAttempt:
    """Run the heal loop with the offline proposer standing in for the LLM.

    Two stages, same order as ``healer.apply.heal_source``: repair the container
    first (so field selectors are re-derived against the *new* container), then
    every field that is broken afterwards.
    """
    samples = field_samples(config, before_html)
    current = config
    proposals: list[Proposal] = []

    breakage = detect_breakage(current, after_html, min_items=DEFAULT_MIN_ITEMS)

    if breakage.kind == "container":
        proposed = propose_container_selector(html=after_html, samples_by_field=samples)
        if (
            proposed is not None
            and validate_selector(after_html, proposed, current.item.container_type) != []
        ):
            proposals.append(
                Proposal(
                    target=CONTAINER_TARGET,
                    old_selector=current.item.container,
                    new_selector=proposed,
                )
            )
            current = _patched(current, target=CONTAINER_TARGET, new_selector=proposed)
        breakage = detect_breakage(current, after_html, min_items=DEFAULT_MIN_ITEMS)

    for name in breakage.fields:
        spec = next(f for f in current.item.fields if f.name == name)
        proposed = propose_field_selector(
            html=after_html,
            container=current.item.container,
            container_type=current.item.container_type,
            old_selector=spec.selector,
            old_samples=samples.get(name, []),
        )
        if proposed is None or not _field_candidate_validates(
            html=after_html, config=current, candidate=proposed
        ):
            continue
        proposals.append(Proposal(target=name, old_selector=spec.selector, new_selector=proposed))
        current = _patched(current, target=name, new_selector=proposed)

    return HealAttempt(config=current, proposals=tuple(proposals))


def evaluate_case(
    *,
    name: str,
    description: str,
    config: SourceConfig,
    before_html: str,
    after_html: str,
    min_items: int,
    expect_healable: bool,
) -> CaseResult:
    """Score one before/after pair. Raises when the case itself is not valid."""
    before = detect_breakage(config, before_html, min_items=min_items)
    if before.is_broken:
        msg = (
            f"case {name!r}: the 'before' fixture is already broken "
            f"({before.kind}, {before.item_count} items) — it must be the healthy baseline"
        )
        raise InvalidEvalCaseError(msg)

    broken = detect_breakage(config, after_html, min_items=min_items)
    if not broken.is_broken:
        msg = (
            f"case {name!r}: the 'after' fixture still extracts cleanly "
            f"({broken.item_count} items) — it does not describe a breakage"
        )
        raise InvalidEvalCaseError(msg)

    attempt = heal_offline(config=config, before_html=before_html, after_html=after_html)
    after_heal = detect_breakage(attempt.config, after_html, min_items=min_items)

    return CaseResult(
        name=name,
        description=description,
        breakage_kind=broken.kind,
        broken_fields=broken.fields,
        items_before=before.item_count,
        items_broken=broken.item_count,
        items_healed=after_heal.item_count,
        healed=not after_heal.is_broken,
        expect_healable=expect_healable,
        proposals=attempt.proposals,
    )


# ── Imperative shell ─────────────────────────────────────────────────────────


def _require_file(root: Path, relative: str, *, case: str, key: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_file():
        msg = f"case {case!r}: {key} fixture {relative!r} not found at {path}"
        raise InvalidEvalCaseError(msg)
    return path


def load_cases(manifest_path: Path, *, root: Path | None = None) -> tuple[CaseSpec, ...]:
    """Read the case manifest. Paths inside it are relative to ``root``."""
    base = root if root is not None else manifest_path.resolve().parent.parent
    raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    entries = raw.get("cases") or []
    if not entries:
        msg = f"{manifest_path} declares no cases"
        raise InvalidEvalCaseError(msg)

    specs: list[CaseSpec] = []
    for entry in entries:
        name = str(entry["name"])
        specs.append(
            CaseSpec(
                name=name,
                description=str(entry.get("description", "")),
                config_path=_require_file(base, entry["config"], case=name, key="config"),
                before_path=_require_file(base, entry["before"], case=name, key="before"),
                after_path=_require_file(base, entry["after"], case=name, key="after"),
                min_items=int(entry.get("min_items", DEFAULT_MIN_ITEMS)),
                expect_healable=bool(entry.get("expect_healable", True)),
            )
        )
    return tuple(specs)


def run_case(spec: CaseSpec) -> CaseResult:
    config_data: Mapping[str, Any] = yaml.safe_load(spec.config_path.read_text(encoding="utf-8"))
    return evaluate_case(
        name=spec.name,
        description=spec.description,
        config=SourceConfig(**config_data),
        before_html=spec.before_path.read_text(encoding="utf-8"),
        after_html=spec.after_path.read_text(encoding="utf-8"),
        min_items=spec.min_items,
        expect_healable=spec.expect_healable,
    )


def run_suite(specs: Sequence[CaseSpec]) -> HealRateReport:
    return HealRateReport(cases=tuple(run_case(spec) for spec in specs))


__all__ = [
    "Breakage",
    "BreakageKind",
    "CaseResult",
    "CaseSpec",
    "EmptyEvalSuiteError",
    "HealAttempt",
    "HealRateReport",
    "InvalidEvalCaseError",
    "Proposal",
    "detect_breakage",
    "evaluate_case",
    "field_samples",
    "heal_offline",
    "load_cases",
    "run_case",
    "run_suite",
]
