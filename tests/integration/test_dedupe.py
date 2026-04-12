"""Integration tests for dedupe pipeline with DB (spec 02-dedupe)."""

import pytest

from magpie.storage.repo import ItemRepository


@pytest.fixture
def item_repo(tmp_path):
    """Create an ItemRepository with a test database."""
    # Will use testcontainers or SQLite for testing
    raise NotImplementedError("Wire up test DB in S4")


class TestDedupeIntegration:
    def test_first_scrape_all_new(self, item_repo: ItemRepository) -> None:
        items = [
            {"title": "Article 1", "id": "1"},
            {"title": "Article 2", "id": "2"},
            {"title": "Article 3", "id": "3"},
        ]
        result = item_repo.persist_items("test-source", items, dedupe_key="id")
        assert result.items_new == 3
        assert result.items_updated == 0
        assert result.items_removed == 0

    def test_second_scrape_identical_no_changes(
        self, item_repo: ItemRepository
    ) -> None:
        items = [
            {"title": "Article 1", "id": "1"},
            {"title": "Article 2", "id": "2"},
        ]
        item_repo.persist_items("test-source", items, dedupe_key="id")
        result = item_repo.persist_items("test-source", items, dedupe_key="id")
        assert result.items_new == 0
        assert result.items_updated == 0
        assert result.items_removed == 0

    def test_updated_item_detected(self, item_repo: ItemRepository) -> None:
        items_v1 = [{"title": "Old Title", "id": "1"}]
        items_v2 = [{"title": "New Title", "id": "1"}]
        item_repo.persist_items("test-source", items_v1, dedupe_key="id")
        result = item_repo.persist_items("test-source", items_v2, dedupe_key="id")
        assert result.items_updated == 1

    def test_removed_item_detected(self, item_repo: ItemRepository) -> None:
        items_v1 = [
            {"title": "Keep", "id": "1"},
            {"title": "Remove", "id": "2"},
        ]
        items_v2 = [{"title": "Keep", "id": "1"}]
        item_repo.persist_items("test-source", items_v1, dedupe_key="id")
        result = item_repo.persist_items("test-source", items_v2, dedupe_key="id")
        assert result.items_removed == 1

    def test_new_item_added(self, item_repo: ItemRepository) -> None:
        items_v1 = [{"title": "Existing", "id": "1"}]
        items_v2 = [
            {"title": "Existing", "id": "1"},
            {"title": "Brand New", "id": "2"},
        ]
        item_repo.persist_items("test-source", items_v1, dedupe_key="id")
        result = item_repo.persist_items("test-source", items_v2, dedupe_key="id")
        assert result.items_new == 1

    def test_reappeared_item(self, item_repo: ItemRepository) -> None:
        items = [{"title": "Comeback", "id": "1"}]
        item_repo.persist_items("test-source", items, dedupe_key="id")
        item_repo.persist_items("test-source", [], dedupe_key="id")  # removed
        result = item_repo.persist_items(
            "test-source", items, dedupe_key="id"
        )  # reappeared
        assert result.items_new == 1

    def test_empty_scrape_marks_all_removed(
        self, item_repo: ItemRepository
    ) -> None:
        items = [{"title": "A", "id": "1"}, {"title": "B", "id": "2"}]
        item_repo.persist_items("test-source", items, dedupe_key="id")
        result = item_repo.persist_items("test-source", [], dedupe_key="id")
        assert result.items_removed == 2

    def test_missing_dedupe_key_raises(self, item_repo: ItemRepository) -> None:
        items = [{"title": "No Key"}]
        with pytest.raises(Exception):
            item_repo.persist_items("test-source", items, dedupe_key="id")

    def test_duplicate_dedupe_keys_raises(self, item_repo: ItemRepository) -> None:
        items = [
            {"title": "A", "id": "1"},
            {"title": "B", "id": "1"},
        ]
        with pytest.raises(Exception):
            item_repo.persist_items("test-source", items, dedupe_key="id")
