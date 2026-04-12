"""Content-addressed hashing for scraped items."""

import hashlib
import json
import unicodedata


def compute_item_hash(item: dict[str, object]) -> str:
    """Compute a deterministic SHA-256 hash of a scraped item.

    Normalization:
    - Keys sorted alphabetically
    - String values stripped of leading/trailing whitespace
    - Unicode NFC-normalized
    - JSON-serialized with sorted keys, no spaces
    """
    normalized = _normalize_item(item)
    serialized = json.dumps(normalized, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _normalize_item(item: dict[str, object]) -> dict[str, object]:
    """Normalize an item dict for deterministic hashing."""
    result: dict[str, object] = {}
    for key in sorted(item.keys()):
        value = item[key]
        if isinstance(value, str):
            value = unicodedata.normalize("NFC", value.strip())
        result[key] = value
    return result
