"""Curated RSS feed suggestions mapped to user profile interest areas."""

from __future__ import annotations

SUGGESTED_FEEDS: dict[str, list[dict]] = {
    "politics_overall": [
        {
            "url": "https://www.politico.eu/feed/",
            "title": "POLITICO Europe",
            "description": "EU politics and policy",
        },
        {
            "url": "https://feeds.bbci.co.uk/news/politics/rss.xml",
            "title": "BBC Politics",
            "description": "UK and world politics",
        },
        {
            "url": "https://www.europarl.europa.eu/rss/doc/top-stories/en.xml",
            "title": "European Parliament News",
            "description": "Official EU Parliament news feed",
        },
        {
            "url": "https://ground.news/interest/politics/feed",
            "title": "Ground News — Politics",
            "description": "Multi-source politics coverage with bias ratings",
        },
    ],
    "ai_ml_agents_rag_architecture": [
        {
            "url": "https://lilianweng.github.io/index.xml",
            "title": "Lil'Log (Lilian Weng)",
            "description": "Deep technical AI/ML posts from OpenAI researcher",
        },
        {
            "url": "https://simonwillison.net/atom/everything/",
            "title": "Simon Willison",
            "description": "LLMs, AI tools, and practical AI engineering",
        },
        {
            "url": "https://www.latent.space/feed",
            "title": "Latent Space",
            "description": "AI engineering podcast and newsletter",
        },
        {
            "url": "https://ground.news/interest/artificial-intelligence/feed",
            "title": "Ground News — AI",
            "description": "Multi-source AI news with bias ratings",
        },
    ],
    "startups_product_leadership": [
        {
            "url": "https://www.lennysnewsletter.com/feed",
            "title": "Lenny's Newsletter",
            "description": "Product management, growth, and startups",
        },
        {
            "url": "https://review.firstround.com/feed.xml",
            "title": "First Round Review",
            "description": "In-depth startup advice from experienced founders",
        },
    ],
    "software_engineering_system_design": [
        {
            "url": "https://martinfowler.com/feed.atom",
            "title": "Martin Fowler",
            "description": "Software architecture and development practices",
        },
        {
            "url": "https://netflixtechblog.com/feed",
            "title": "Netflix Tech Blog",
            "description": "Engineering at scale from Netflix",
        },
        {
            "url": "https://newsletter.pragmaticengineer.com/feed",
            "title": "The Pragmatic Engineer",
            "description": "Software engineering industry insights",
        },
    ],
    "personal_finance_investing_econ": [
        {
            "url": "https://ground.news/interest/economy/feed",
            "title": "Ground News — Economy",
            "description": "Multi-source economics coverage",
        },
    ],
    "languages_en_fr_for_career": [
        {
            "url": "https://ground.news/interest/european-union/feed",
            "title": "Ground News — EU",
            "description": "EU news in multiple languages",
        },
    ],
    "applied_math_prob_stats_opt_algo": [
        {
            "url": "https://blog.computationalcomplexity.org/feeds/posts/default",
            "title": "Computational Complexity",
            "description": "Theoretical CS and math blog",
        },
    ],
}


def get_suggestions_for_profile(
    user_profile: dict,
    existing_feed_urls: set[str],
) -> dict[str, list[dict]]:
    """Return suggested feeds grouped by interest area, excluding already-subscribed ones.

    Args:
        user_profile: User profile dict with 'interest_areas' list.
        existing_feed_urls: Set of already-subscribed feed URLs.

    Returns:
        Dict mapping interest area display name to list of feed suggestions.
    """
    interest_areas = user_profile.get("interest_areas", [])
    if not interest_areas:
        return {}

    suggestions: dict[str, list[dict]] = {}

    for area in interest_areas:
        area_id = area.get("id", "")
        area_label = area.get("label", area_id.replace("_", " ").title())

        if area_id not in SUGGESTED_FEEDS:
            continue

        available = [
            feed for feed in SUGGESTED_FEEDS[area_id]
            if feed["url"] not in existing_feed_urls
        ]

        if available:
            suggestions[area_label] = available

    return suggestions
