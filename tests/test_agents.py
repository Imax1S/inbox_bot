"""Tests for individual agents — collector, clusterer, filter, profiler."""

import pytest

from src.agents.collector import CollectorAgent, CollectorResult
from src.agents.clusterer import ClustererAgent
from src.agents.filter import FilterAgent, FilterResult
from src.agents.profiler import ProfilerAgent
from src.db.models import ItemType
from tests.conftest import MockLLMProvider


# ── CollectorAgent ──


@pytest.mark.asyncio
async def test_collector_structured_success(mock_db, sample_user_profile):
    provider = MockLLMProvider(structured_response={
        "summary": "An article about AI safety measures",
        "tags": ["ai", "safety", "policy"],
        "language": "en",
    })
    agent = CollectorAgent(
        llm=provider, model="test", db=mock_db, user_profile=sample_user_profile,
    )
    result = await agent.process(
        raw_content="Check out this AI safety article",
        extracted_text="Full article text here...",
        item_type=ItemType.ARTICLE,
    )
    assert isinstance(result, CollectorResult)
    assert result.summary == "An article about AI safety measures"
    assert result.tags == ["ai", "safety", "policy"]
    assert result.language == "en"


@pytest.mark.asyncio
async def test_collector_structured_fallback(mock_db, sample_user_profile):
    provider = MockLLMProvider(should_raise=RuntimeError("LLM error"))
    agent = CollectorAgent(
        llm=provider, model="test", db=mock_db, user_profile=sample_user_profile,
    )
    result = await agent.process(
        raw_content="Some raw content here",
        extracted_text=None,
        item_type=ItemType.TOPIC_SEED,
    )
    assert isinstance(result, CollectorResult)
    assert result.summary == "Some raw content here"
    assert result.tags == []
    assert result.language == "ru"


# ── ClustererAgent ──


@pytest.mark.asyncio
async def test_clusterer_structured_success(mock_db, sample_user_profile, sample_items):
    provider = MockLLMProvider(structured_response={
        "clusters": [
            {
                "id": "cluster-1",
                "title": "AI & Safety",
                "editorial_angle": "Recent AI safety developments",
                "item_ids": ["item-1"],
                "estimated_read_minutes": 3,
                "priority": 1,
            },
            {
                "id": "cluster-2",
                "title": "Engineering",
                "editorial_angle": "System design insights",
                "item_ids": ["item-2"],
                "estimated_read_minutes": 2,
                "priority": 2,
            },
        ],
        "quick_bites_item_ids": ["item-3"],
    })
    agent = ClustererAgent(
        llm=provider, model="test", db=mock_db, user_profile=sample_user_profile,
    )
    result = await agent.process(items=sample_items)
    assert len(result.clusters) == 2
    assert result.clusters[0].title == "AI & Safety"
    assert result.clusters[0].item_ids == ["item-1"]
    assert result.quick_bites_item_ids == ["item-3"]


@pytest.mark.asyncio
async def test_clusterer_validates_item_ids(mock_db, sample_user_profile, sample_items):
    provider = MockLLMProvider(structured_response={
        "clusters": [
            {
                "id": "cluster-1",
                "title": "Mixed",
                "editorial_angle": "Test",
                "item_ids": ["item-1", "item-999"],  # item-999 doesn't exist
                "estimated_read_minutes": 3,
                "priority": 1,
            },
        ],
        "quick_bites_item_ids": ["item-888"],  # also invalid
    })
    agent = ClustererAgent(
        llm=provider, model="test", db=mock_db, user_profile=sample_user_profile,
    )
    result = await agent.process(items=sample_items)
    assert result.clusters[0].item_ids == ["item-1"]
    assert result.quick_bites_item_ids == []


@pytest.mark.asyncio
async def test_clusterer_structured_fallback(mock_db, sample_user_profile, sample_items):
    provider = MockLLMProvider(should_raise=RuntimeError("LLM error"))
    agent = ClustererAgent(
        llm=provider, model="test", db=mock_db, user_profile=sample_user_profile,
    )
    result = await agent.process(items=sample_items)
    assert len(result.clusters) == 1
    assert result.clusters[0].title == "This Week's Highlights"
    assert set(result.clusters[0].item_ids) == {"item-1", "item-2", "item-3"}


# ── FilterAgent ──


@pytest.mark.asyncio
async def test_filter_structured_success(mock_db, sample_user_profile, sample_items):
    provider = MockLLMProvider(structured_response={
        "kept_items": [
            {"id": "item-1", "relevance_score": 0.9, "reason": "Highly relevant"},
            {"id": "item-2", "relevance_score": 0.7, "reason": "Relevant"},
        ],
        "filtered_items": [
            {
                "id": "item-3",
                "relevance_score": 0.2,
                "filter_type": "shallow",
                "reason": "Too shallow",
            },
        ],
    })
    agent = FilterAgent(
        llm=provider, model="test", db=mock_db, user_profile=sample_user_profile,
    )
    result = await agent.process(items=sample_items)
    assert isinstance(result, FilterResult)
    assert "item-1" in result.kept_item_ids
    assert "item-2" in result.kept_item_ids
    assert len(result.filtered_items) == 1
    assert result.filtered_items[0].id == "item-3"


@pytest.mark.asyncio
async def test_filter_keeps_unmentioned_items(mock_db, sample_user_profile, sample_items):
    """Items not mentioned in LLM output should be kept (safety net)."""
    provider = MockLLMProvider(structured_response={
        "kept_items": [
            {"id": "item-1", "relevance_score": 0.9, "reason": "Good"},
        ],
        "filtered_items": [],
        # item-2 and item-3 are not mentioned at all
    })
    agent = FilterAgent(
        llm=provider, model="test", db=mock_db, user_profile=sample_user_profile,
    )
    result = await agent.process(items=sample_items)
    assert "item-1" in result.kept_item_ids
    assert "item-2" in result.kept_item_ids
    assert "item-3" in result.kept_item_ids


@pytest.mark.asyncio
async def test_filter_structured_fallback(mock_db, sample_user_profile, sample_items):
    provider = MockLLMProvider(should_raise=RuntimeError("LLM error"))
    agent = FilterAgent(
        llm=provider, model="test", db=mock_db, user_profile=sample_user_profile,
    )
    result = await agent.process(items=sample_items)
    assert set(result.kept_item_ids) == {"item-1", "item-2", "item-3"}
    assert result.filtered_items == []


@pytest.mark.asyncio
async def test_filter_empty_items(mock_db, sample_user_profile):
    provider = MockLLMProvider(structured_response={"should": "not be called"})
    agent = FilterAgent(
        llm=provider, model="test", db=mock_db, user_profile=sample_user_profile,
    )
    result = await agent.process(items=[])
    assert result.kept_item_ids == []
    assert result.filtered_items == []
    assert len(provider.calls) == 0  # LLM should not be called


# ── ProfilerAgent ──


@pytest.mark.asyncio
async def test_profiler_structured_success(mock_db):
    provider = MockLLMProvider(structured_response={
        "user": {
            "preferred_name": "Alice",
            "primary_language": "en",
            "core_style_preferences": {
                "practicality_over_theory": True,
                "depth_over_breadth": True,
                "systems_thinking": False,
                "prefers_actionable_outputs": True,
            },
        },
        "interest_areas": [
            {
                "id": "machine_learning",
                "name": "Machine Learning",
                "weight": 0.85,
                "keywords": ["ml", "neural networks", "deep learning"],
            },
        ],
        "noise_topics": ["celebrity gossip"],
        "detected_languages": ["en"],
    })
    agent = ProfilerAgent(llm=provider, model="test", db=mock_db)
    result = await agent.extract_interests("I'm Alice, interested in ML")
    assert result["user"]["preferred_name"] == "Alice"
    assert len(result["interest_areas"]) == 1
    assert result["interest_areas"][0]["id"] == "machine_learning"
    assert result["noise_topics"] == ["celebrity gossip"]


@pytest.mark.asyncio
async def test_profiler_structured_fallback(mock_db):
    provider = MockLLMProvider(should_raise=RuntimeError("LLM error"))
    agent = ProfilerAgent(llm=provider, model="test", db=mock_db)
    result = await agent.extract_interests("Some description")
    assert result["interest_areas"] == []
    assert "error" in result
    assert "LLM error" in result["error"]
