"""Tests for filter batching and budget awareness."""

from datetime import datetime

import pytest
import pytest_asyncio

from src.agents.filter import FilterAgent, FilterResult, KeptItem
from src.db.models import Item, ItemStatus, ItemType
from tests.conftest import MockLLMProvider


def _make_item(item_id: str, summary: str = "test summary") -> Item:
    return Item(
        id=item_id,
        created_at=datetime.now(),
        type=ItemType.ARTICLE,
        raw_content=f"Content for {item_id}",
        summary=summary,
        tags=["test"],
        language="en",
        week_id="2026-W12",
        status=ItemStatus.COLLECTED,
    )


def _make_filter_response(items: list[Item]) -> dict:
    """Create a mock filter response that keeps all items."""
    return {
        "kept_items": [
            {"id": item.id, "relevance_score": 0.8, "reason": "relevant"}
            for item in items
        ],
        "filtered_items": [],
    }


@pytest.mark.asyncio
async def test_filter_batching(mock_db):
    """40 items should be split into 2 batches, results merged correctly."""
    items = [_make_item(f"item-{i}") for i in range(40)]

    # Mock that keeps all items
    mock_llm = MockLLMProvider(structured_response=_make_filter_response(items))

    profile = {"filtering_strictness": "moderate", "interest_areas": []}
    agent = FilterAgent(mock_llm, "test-model", mock_db, profile)

    result = await agent.process(items)

    # All 40 items should be kept
    assert len(result.kept_item_ids) == 40
    assert len(result.filtered_items) == 0
    # Should have made 2 LLM calls (30 + 10)
    assert len(mock_llm.calls) == 2


@pytest.mark.asyncio
async def test_filter_no_batching_small_list(mock_db):
    """Items under batch size should not be batched."""
    items = [_make_item(f"item-{i}") for i in range(5)]

    mock_llm = MockLLMProvider(structured_response=_make_filter_response(items))
    profile = {"filtering_strictness": "moderate", "interest_areas": []}
    agent = FilterAgent(mock_llm, "test-model", mock_db, profile)

    result = await agent.process(items)

    assert len(result.kept_item_ids) == 5
    assert len(mock_llm.calls) == 1


@pytest.mark.asyncio
async def test_filter_budget_in_user_message(mock_db):
    """Budget context should appear in user message when item count > 20."""
    items = [_make_item(f"item-{i}") for i in range(25)]

    mock_llm = MockLLMProvider(structured_response=_make_filter_response(items))
    profile = {"filtering_strictness": "moderate", "interest_areas": []}
    agent = FilterAgent(mock_llm, "test-model", mock_db, profile)

    await agent.process(items, target_read_minutes=25)

    # Check that budget context was included in the user message
    assert len(mock_llm.calls) == 1
    user_msg = mock_llm.calls[0]["user_message"]
    assert "Budget context" in user_msg
    assert "25 minutes" in user_msg


@pytest.mark.asyncio
async def test_filter_no_budget_for_small_list(mock_db):
    """Budget context should NOT appear when item count <= 20."""
    items = [_make_item(f"item-{i}") for i in range(10)]

    mock_llm = MockLLMProvider(structured_response=_make_filter_response(items))
    profile = {"filtering_strictness": "moderate", "interest_areas": []}
    agent = FilterAgent(mock_llm, "test-model", mock_db, profile)

    await agent.process(items, target_read_minutes=25)

    user_msg = mock_llm.calls[0]["user_message"]
    assert "Budget context" not in user_msg


@pytest.mark.asyncio
async def test_filter_result_keeps_scores(mock_db):
    """FilterResult should preserve kept item scores."""
    items = [_make_item("item-1"), _make_item("item-2")]
    response = {
        "kept_items": [
            {"id": "item-1", "relevance_score": 0.9, "reason": "very relevant"},
        ],
        "filtered_items": [
            {"id": "item-2", "relevance_score": 0.1, "filter_type": "irrelevant", "reason": "not relevant"},
        ],
    }

    mock_llm = MockLLMProvider(structured_response=response)
    profile = {"filtering_strictness": "moderate", "interest_areas": []}
    agent = FilterAgent(mock_llm, "test-model", mock_db, profile)

    result = await agent.process(items)

    assert len(result.kept_items_with_scores) == 1
    assert result.kept_items_with_scores[0].id == "item-1"
    assert result.kept_items_with_scores[0].relevance_score == 0.9
