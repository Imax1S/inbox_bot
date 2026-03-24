"""Tests for low-cost real smoke-test helpers."""

from datetime import datetime

from src.db.models import Item, ItemStatus, ItemType
from src.smoke_test import get_smoke_models, prepare_smoke_test_items


def test_get_smoke_models_returns_low_cost_defaults():
    assert get_smoke_models("openai") == ("gpt-4.1-nano", "gpt-4.1-mini")
    assert get_smoke_models("anthropic") == (
        "claude-haiku-3-5-20241022",
        "claude-haiku-3-5-20241022",
    )


def test_prepare_smoke_test_items_limits_count_and_truncates_content():
    now = datetime.now()
    items = [
        Item(
            id="1",
            created_at=now,
            type=ItemType.ARTICLE,
            raw_content="a" * 900,
            summary="s" * 300,
            tags=["a"],
            language="en",
            week_id="2026-W12",
            status=ItemStatus.COLLECTED,
            extracted_text="x" * 1800,
        ),
        Item(
            id="2",
            created_at=now,
            type=ItemType.ARTICLE,
            raw_content="short",
            summary="short summary",
            tags=["b"],
            language="en",
            week_id="2026-W12",
            status=ItemStatus.COLLECTED,
            extracted_text="small",
        ),
        Item(
            id="3",
            created_at=now,
            type=ItemType.TOPIC_SEED,
            raw_content="medium" * 80,
            summary="medium",
            tags=["c"],
            language="en",
            week_id="2026-W12",
            status=ItemStatus.COLLECTED,
        ),
        Item(
            id="4",
            created_at=now,
            type=ItemType.CONTEXT_NOTE,
            raw_content="tiny",
            summary="tiny",
            tags=["d"],
            language="en",
            week_id="2026-W12",
            status=ItemStatus.COLLECTED,
        ),
    ]

    prepared = prepare_smoke_test_items(items, limit=3)

    assert [item.id for item in prepared] == ["4", "2", "3"]
    assert len(prepared) == 3
    assert all(item.week_id == "2026-W12" for item in prepared)

    prepared_long = prepare_smoke_test_items([items[0]], limit=1)[0]
    assert "[...truncated for smoke test]" in prepared_long.raw_content
