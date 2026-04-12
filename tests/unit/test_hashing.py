"""Tests for content-addressed hashing (spec 02-dedupe)."""

import pytest

from magpie.core.hashing import compute_item_hash


class TestHashingHappyPath:
    def test_same_item_same_hash(self) -> None:
        item = {"title": "Hello World", "url": "https://example.com"}
        assert compute_item_hash(item) == compute_item_hash(item)

    def test_different_values_different_hash(self) -> None:
        item_a = {"title": "Hello", "url": "https://a.com"}
        item_b = {"title": "World", "url": "https://b.com"}
        assert compute_item_hash(item_a) != compute_item_hash(item_b)

    def test_hash_is_hex_sha256(self) -> None:
        item = {"title": "test"}
        h = compute_item_hash(item)
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)


class TestHashingEdgeCases:
    def test_whitespace_normalization(self) -> None:
        item_a = {"title": "  Hello World  ", "url": "https://example.com"}
        item_b = {"title": "Hello World", "url": "https://example.com"}
        assert compute_item_hash(item_a) == compute_item_hash(item_b)

    def test_unicode_nfc_normalization(self) -> None:
        # e + combining acute vs precomposed e-acute
        item_a = {"title": "caf\u0065\u0301"}
        item_b = {"title": "caf\u00e9"}
        assert compute_item_hash(item_a) == compute_item_hash(item_b)

    def test_key_order_irrelevant(self) -> None:
        item_a = {"title": "Hello", "url": "https://example.com"}
        item_b = {"url": "https://example.com", "title": "Hello"}
        assert compute_item_hash(item_a) == compute_item_hash(item_b)

    def test_large_item_data(self) -> None:
        item = {"title": "x" * 15000, "content": "y" * 15000}
        h = compute_item_hash(item)
        assert len(h) == 64

    def test_special_chars_in_values(self) -> None:
        item = {"title": 'He said "hello" & <goodbye>'}
        h = compute_item_hash(item)
        assert len(h) == 64

    def test_empty_string_values(self) -> None:
        item_a = {"title": "", "url": ""}
        item_b = {"title": "nonempty", "url": ""}
        assert compute_item_hash(item_a) != compute_item_hash(item_b)
