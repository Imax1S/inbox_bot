"""Tests for RSS feed suggestions."""

import pytest

from src.rss_suggestions import get_suggestions_for_profile


def test_suggest_returns_feeds_for_profile():
    profile = {
        "interest_areas": [
            {"id": "ai_ml_agents_rag_architecture", "label": "AI/ML", "weight": 0.9},
            {"id": "politics_overall", "label": "Politics", "weight": 0.8},
        ],
    }
    suggestions = get_suggestions_for_profile(profile, existing_feed_urls=set())
    assert "AI/ML" in suggestions
    assert "Politics" in suggestions
    assert len(suggestions["AI/ML"]) > 0
    assert all("url" in f and "title" in f for f in suggestions["AI/ML"])


def test_suggest_excludes_already_subscribed():
    profile = {
        "interest_areas": [
            {"id": "ai_ml_agents_rag_architecture", "label": "AI/ML", "weight": 0.9},
        ],
    }
    # Get all suggestions first
    all_suggestions = get_suggestions_for_profile(profile, existing_feed_urls=set())
    all_urls = {f["url"] for f in all_suggestions.get("AI/ML", [])}

    # Now exclude all of them
    suggestions = get_suggestions_for_profile(profile, existing_feed_urls=all_urls)
    # Should have no AI/ML suggestions left
    assert "AI/ML" not in suggestions


def test_suggest_empty_profile():
    suggestions = get_suggestions_for_profile({}, existing_feed_urls=set())
    assert suggestions == {}


def test_suggest_unknown_interest_area():
    profile = {
        "interest_areas": [
            {"id": "underwater_basket_weaving", "label": "Baskets", "weight": 0.9},
        ],
    }
    suggestions = get_suggestions_for_profile(profile, existing_feed_urls=set())
    assert suggestions == {}
