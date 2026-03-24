"""Tests for digest feedback persistence and helper logic."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.config import (
    Config,
    LLMConfig,
    ObsidianConfig,
    ScheduleConfig,
    TelegramConfig,
)
from src.db.models import Cluster
from src.telegram.bot import DigestBot


def _build_test_bot(mock_db, sample_user_profile):
    config = Config(
        telegram=TelegramConfig(bot_token="token", user_ids=[123]),
        llm=LLMConfig(
            provider="openai",
            anthropic_api_key="",
            openai_api_key="",
            collector_model="test",
            clusterer_model="test",
            researcher_model="test",
            writer_model="test",
            editor_model="test",
            translator_model="test",
            profiler_model="test",
            filter_model="test",
        ),
        obsidian=ObsidianConfig(vault_path=Path("/tmp")),
        schedule=ScheduleConfig(
            enabled=False,
            day_of_week=6,
            hour=23,
            minute=0,
            timezone="UTC",
        ),
        user_profile=sample_user_profile,
        db_path=Path("/tmp/test-feedback.db"),
    )
    return DigestBot(
        config=config,
        db=mock_db,
        collector=None,
        orchestrator=SimpleNamespace(filter_agent=None),
    )


@pytest.mark.asyncio
async def test_save_and_get_article_feedback(mock_db):
    await mock_db.save_article_feedback(
        week_id="2026-W08",
        cluster_id="cluster-1",
        cluster_title="AI & Safety",
        feedback="like",
        matched_area_ids=["ai", "safety"],
    )

    rows = await mock_db.get_feedback_by_week("2026-W08")

    assert len(rows) == 1
    assert rows[0]["cluster_id"] == "cluster-1"
    assert rows[0]["cluster_title"] == "AI & Safety"
    assert rows[0]["feedback"] == "like"
    assert rows[0]["matched_area_ids"] == ["ai", "safety"]


@pytest.mark.asyncio
async def test_truncate_preview_prefers_sentence_boundary(mock_db, sample_user_profile):
    bot = _build_test_bot(mock_db, sample_user_profile)

    preview = bot._truncate_preview(
        "# Heading\n\nFirst sentence is here. Second sentence should not fit.",
        max_len=35,
    )

    assert preview == "Heading First sentence is here."


@pytest.mark.asyncio
async def test_map_cluster_to_areas_matches_tags_to_area_ids_and_keywords(
    mock_db,
    sample_user_profile,
    sample_items,
):
    bot = _build_test_bot(mock_db, sample_user_profile)
    profile = {
        **sample_user_profile,
        "interest_areas": [
            *sample_user_profile["interest_areas"],
            {
                "id": "system_design",
                "name": "System Design",
                "weight": 0.7,
                "keywords": ["design", "architecture"],
            },
        ],
    }
    cluster = Cluster(
        id="cluster-1",
        title="AI and Engineering",
        editorial_angle="",
        item_ids=["item-1", "item-2"],
        estimated_read_minutes=4,
        priority=1,
    )

    matched = bot._map_cluster_to_areas(
        cluster,
        sample_items,
        profile["interest_areas"],
    )

    assert matched == ["ai", "system_design"]
