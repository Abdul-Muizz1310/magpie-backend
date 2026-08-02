"""Deterministic, network-free selector re-derivation (spec 07).

This is the eval's stand-in for ``healer.selector_fixer.fix_selector``. Instead of
asking an LLM what the new selector should be, it *anchors on values the scraper
already extracted*: find the elements in the current HTML that still carry those
values, then describe them with the least specific CSS fragment that resolves in
most item containers.

Pure functions over strings only — no I/O, no clock, no randomness — so the eval
returns the same number on every machine.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Literal

from lxml.etree import _Element
from parsel import Selector

from magpie.config.schema import SelectorType

_WS_RE = re.compile(r"\s+")
_ATTR_RE = re.compile(r"::attr\(\s*([A-Za-z_:][-A-Za-z0-9_:.]*)\s*\)\s*$")
_TEXT_RE = re.compile(r"::text\s*$")

MIN_CONTAINER_COVERAGE = 0.5
"""A proposed field selector must resolve in at least this share of containers.

Anything less is drift that happens to match one row, not a repaired selector.
"""

MIN_CONTAINER_REPEATS = 2
"""An item container repeats by definition; a one-off signature is a page chrome
element, not the row we lost.
"""

AccessorKind = Literal["text", "attr"]


@dataclass(frozen=True)
class Accessor:
    """How a value is read off a matched element."""

    kind: AccessorKind
    attr: str | None

    def compose(self, fragment: str) -> str:
        """Re-attach this accessor to a CSS ``fragment`` (possibly empty)."""
        if self.kind == "attr":
            return f"{fragment}::attr({self.attr})"
        return f"{fragment}::text"


def normalize_value(value: str) -> str:
    """NFC-normalise, collapse whitespace, strip — the same shape hashing uses."""
    return _WS_RE.sub(" ", unicodedata.normalize("NFC", value)).strip()


def parse_accessor(selector: str) -> tuple[str, Accessor]:
    """Split a field selector into its element fragment and its value accessor.

    ``"span.x > a::attr(href)"`` → ``("span.x > a", Accessor("attr", "href"))``.
    A selector with no pseudo-element reads text, matching parsel's behaviour of
    returning serialised elements only when asked for them.
    """
    attr_match = _ATTR_RE.search(selector)
    if attr_match is not None:
        return selector[: attr_match.start()].strip(), Accessor("attr", attr_match.group(1))
    text_match = _TEXT_RE.search(selector)
    if text_match is not None:
        return selector[: text_match.start()].strip(), Accessor("text", None)
    return selector.strip(), Accessor("text", None)


def _wanted(samples: Iterable[str]) -> frozenset[str]:
    return frozenset(normalized for s in samples if (normalized := normalize_value(str(s))) != "")


def _tag_elements(scope: _Element) -> Iterator[_Element]:
    """Yield real tag elements under ``scope`` (skipping comments and PIs).

    ``_Element.iter()`` also yields comment and processing-instruction nodes,
    whose ``.tag`` is a callable rather than a tag name — they have no CSS
    representation, so they can never be an anchor.
    """
    for element in scope.iter():
        if isinstance(element.tag, str):
            yield element


def _own_text(element: _Element) -> str:
    """Direct text of an element, ignoring descendant text.

    Direct text is what makes the *deepest* holder of a value the anchor: for
    ``<span><a>Alpha</a></span>`` only the ``<a>`` matches, so the proposal is
    ``a``, not the whole span.
    """
    return normalize_value(element.text or "")


def _subtree_text(element: _Element) -> str:
    """All text under ``element``.

    ``itertext()`` yields ``bytes`` for trees parsed from bytes without a declared
    encoding, so decode defensively rather than letting a ``TypeError`` escape
    from ``str.join`` mid-eval.
    """
    chunks = [
        chunk.decode("utf-8", "replace") if isinstance(chunk, bytes) else chunk
        for chunk in element.itertext()
    ]
    return normalize_value("".join(chunks))


def _anchors(scope: _Element, wanted: frozenset[str]) -> list[tuple[_Element, Accessor]]:
    """Elements under ``scope`` whose own text or an attribute holds a wanted value.

    Two passes: direct text first (precise), whole-subtree text only if nothing
    was found (a value split across inline children still gets an anchor).
    """
    found: list[tuple[_Element, Accessor]] = []
    for element in _tag_elements(scope):
        if _own_text(element) in wanted:
            found.append((element, Accessor("text", None)))
        for name, value in element.attrib.items():
            if normalize_value(str(value)) in wanted:
                found.append((element, Accessor("attr", str(name))))
    if found:
        return found
    return [
        (element, Accessor("text", None))
        for element in _tag_elements(scope)
        if _subtree_text(element) in wanted
    ]


def _css_step(element: _Element) -> str:
    """``tag.class1.class2`` for one element — ids are skipped as too unique."""
    classes = [c for c in str(element.get("class") or "").split() if c]
    return str(element.tag) + "".join(f".{c}" for c in classes)


def _fragment_candidates(element: _Element, scope: _Element) -> list[str]:
    """CSS fragments locating ``element`` inside ``scope``, least specific first.

    Returns ``[""]`` when ``element`` *is* ``scope`` (a field read off the
    container), and ``[]`` when it isn't inside ``scope`` at all.
    """
    chain: list[str] = []
    node: _Element | None = element
    while node is not None and node is not scope:
        chain.append(_css_step(node))
        node = node.getparent()
    if node is not scope:
        return []
    if not chain:
        return [""]
    chain.reverse()
    candidates = [chain[-1]]
    if len(chain) >= 2:
        candidates.append(" ".join(chain[-2:]))
        candidates.append(" > ".join(chain))
    return list(dict.fromkeys(candidates))


def _containers(html: str, container: str, container_type: SelectorType) -> list[Selector]:
    sel = Selector(text=html)
    matches = sel.css(container) if container_type == "css" else sel.xpath(container)
    return list(matches)


def _resolves_in(containers: list[Selector], candidate: str) -> int:
    """How many containers the candidate selector yields a value for."""
    hits = 0
    for element in containers:
        try:
            if element.css(candidate).getall():
                hits += 1
        except Exception:
            return 0
    return hits


def propose_field_selector(
    *,
    html: str,
    container: str,
    container_type: SelectorType,
    old_selector: str,
    old_samples: Iterable[str],
) -> str | None:
    """Re-derive a broken field selector from values it used to extract.

    Returns ``None`` — never a guess — when the old values are absent from
    ``html``, when there are no usable samples, or when nothing resolves in a
    majority of containers.
    """
    wanted = _wanted(old_samples)
    if not wanted:
        return None
    containers = _containers(html, container, container_type)
    if not containers:
        return None

    _, old_accessor = parse_accessor(old_selector)
    scores: dict[str, int] = {}
    for element in containers:
        seen: set[str] = set()
        for anchor, accessor in _anchors(element.root, wanted):
            for fragment in _fragment_candidates(anchor, element.root):
                candidate = accessor.compose(fragment)
                if candidate not in seen:
                    seen.add(candidate)
                    scores[candidate] = scores.get(candidate, 0) + 1

    threshold = max(1, int(len(containers) * MIN_CONTAINER_COVERAGE + 0.999999))
    ranked = sorted(
        (c for c, hits in scores.items() if hits >= threshold),
        key=lambda c: (
            -scores[c],
            parse_accessor(c)[1].kind != old_accessor.kind,
            len(c),
            c,
        ),
    )
    for candidate in ranked:
        if _resolves_in(containers, candidate) >= threshold:
            return candidate
    return None


def propose_container_selector(
    *,
    html: str,
    samples_by_field: Mapping[str, Iterable[str]],
) -> str | None:
    """Re-derive a broken item container from the values its fields used to hold.

    Scores every repeated ``tag.class`` signature by how many of the config's
    fields it can anchor, preferring the signature that covers the most fields and
    — on a tie — the smallest subtree. That ordering is what picks the *row*
    (``tr.story-row``, which also carries the id attribute) over the deepest
    repeated element inside it (``span.titleline``, which covers fewer fields).
    """
    sel = Selector(text=html)
    root = sel.root

    anchors_by_field: dict[str, set[_Element]] = {}
    for field, samples in samples_by_field.items():
        wanted = _wanted(samples)
        if not wanted:
            continue
        matched = {anchor for anchor, _ in _anchors(root, wanted)}
        if matched:
            anchors_by_field[field] = matched
    if not anchors_by_field:
        return None

    signatures: list[str] = []
    for anchors in anchors_by_field.values():
        for anchor in anchors:
            node: _Element | None = anchor
            while node is not None:
                signature = _css_step(node)
                if signature not in signatures:
                    signatures.append(signature)
                node = node.getparent()

    best: tuple[int, int, str] | None = None
    for signature in signatures:
        try:
            matches = [m.root for m in sel.css(signature)]
        except Exception:
            continue
        if len(matches) < MIN_CONTAINER_REPEATS:
            continue
        reachable = {element for match in matches for element in _tag_elements(match)}
        covered = sum(1 for anchors in anchors_by_field.values() if anchors & reachable)
        if covered == 0:
            continue
        subtree = sum(1 for _ in _tag_elements(matches[0]))
        scored = (-covered, subtree, signature)
        if best is None or scored < best:
            best = scored
    return None if best is None else best[2]


__all__ = [
    "Accessor",
    "AccessorKind",
    "normalize_value",
    "parse_accessor",
    "propose_container_selector",
    "propose_field_selector",
]
