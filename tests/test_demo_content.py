"""Tests for demo content used by zero-cost dry runs."""

from src.db.models import ItemStatus, ItemType
from src.demo_content import build_demo_items


def test_build_demo_items_creates_collected_week_items():
    items = build_demo_items("2026-W12")

    assert len(items) == 5
    assert all(item.week_id == "2026-W12" for item in items)
    assert all(item.status == ItemStatus.COLLECTED for item in items)
    assert all(item.raw_content.startswith("[DEMO]") for item in items)
    assert {item.type for item in items} == {
        ItemType.ARTICLE,
        ItemType.TOPIC_SEED,
        ItemType.CONTEXT_NOTE,
    }
