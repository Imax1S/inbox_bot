"""Tests for pre-filter deduplication."""

from datetime import datetime

import pytest

from src.content.dedup import find_near_duplicates
from src.db.models import Item, ItemStatus, ItemType


def _make_item(item_id: str, summary: str, extracted_text: str = "") -> Item:
    return Item(
        id=item_id,
        created_at=datetime.now(),
        type=ItemType.ARTICLE,
        raw_content=summary,
        summary=summary,
        tags=[],
        language="en",
        week_id="2026-W12",
        status=ItemStatus.COLLECTED,
        extracted_text=extracted_text,
    )


def test_find_near_duplicates_identical_summaries():
    items = [
        _make_item("a", "AI safety research overview and implications"),
        _make_item("b", "AI safety research overview and implications"),
    ]
    pairs = find_near_duplicates(items, threshold=0.6)
    assert len(pairs) == 1
    dup_id, keep_id = pairs[0]
    assert {dup_id, keep_id} == {"a", "b"}


def test_find_near_duplicates_similar_summaries():
    items = [
        _make_item("a", "New EU regulation on artificial intelligence safety standards"),
        _make_item("b", "EU regulation artificial intelligence safety new standards proposal"),
        _make_item("c", "Python web framework comparison Django vs Flask"),
    ]
    pairs = find_near_duplicates(items, threshold=0.6)
    # a and b are similar, c is unrelated
    assert len(pairs) == 1
    dup_ids = {p[0] for p in pairs}
    keep_ids = {p[1] for p in pairs}
    assert (dup_ids | keep_ids) == {"a", "b"}
    assert "c" not in dup_ids


def test_find_near_duplicates_no_duplicates():
    items = [
        _make_item("a", "Machine learning model training best practices"),
        _make_item("b", "European parliament election results 2026"),
        _make_item("c", "Python asyncio tutorial for beginners"),
    ]
    pairs = find_near_duplicates(items, threshold=0.6)
    assert len(pairs) == 0


def test_find_near_duplicates_keeps_richer_content():
    items = [
        _make_item("a", "AI safety overview", extracted_text="short"),
        _make_item("b", "AI safety overview", extracted_text="much longer extracted text content here"),
    ]
    pairs = find_near_duplicates(items, threshold=0.6)
    assert len(pairs) == 1
    dup_id, keep_id = pairs[0]
    # b has longer extracted_text, so a should be the duplicate
    assert dup_id == "a"
    assert keep_id == "b"


def test_find_near_duplicates_single_item():
    items = [_make_item("a", "Only one item")]
    pairs = find_near_duplicates(items, threshold=0.6)
    assert len(pairs) == 0


def test_find_near_duplicates_empty():
    pairs = find_near_duplicates([], threshold=0.6)
    assert len(pairs) == 0
