"""System defaults and profile prompt builder.

User-facing profile is kept minimal (interest areas, blocked topics, strictness).
Everything else lives here as constants and gets injected into agent prompts only
when needed.
"""

# Scoring thresholds per strictness level (used by FilterAgent)
STRICTNESS_THRESHOLDS: dict[str, dict] = {
    "strict": {"drop_below": 0.40, "review_if_between": [0.40, 0.55], "keep_if_above": 0.55},
    "moderate": {"drop_below": 0.25, "review_if_between": [0.25, 0.45], "keep_if_above": 0.45},
    "relaxed": {"drop_below": 0.15, "review_if_between": [0.15, 0.35], "keep_if_above": 0.35},
}


def get_scoring_thresholds(strictness: str) -> dict:
    """Return scoring thresholds for the given strictness level."""
    return STRICTNESS_THRESHOLDS.get(strictness, STRICTNESS_THRESHOLDS["moderate"])


def _format_areas_with_keywords(areas: list[dict], include_weights: bool = False) -> str:
    """Format interest areas as readable text."""
    if not areas:
        return "No interest areas configured."
    lines = []
    for area in areas:
        name = area.get("name", area.get("id", "unknown"))
        keywords = area.get("keywords", [])
        kw_str = ", ".join(keywords[:15]) if keywords else "no keywords"
        if include_weights:
            weight = area.get("weight", 0.0)
            lines.append(f"- {name} (weight: {weight:.2f}): {kw_str}")
        else:
            lines.append(f"- {name}: {kw_str}")
    return "\n".join(lines)


def _format_blocked_topics(profile: dict) -> str:
    """Format blocked topics list."""
    blocked = profile.get("blocked_topics", [])
    if not blocked:
        return ""
    return "Blocked topics (hard exclude): " + ", ".join(blocked)


def build_agent_profile_prompt(profile: dict, agent_role: str) -> str:
    """Build a focused profile section for a specific agent.

    Each agent gets only the parts of the profile it needs,
    formatted as readable text instead of raw JSON.
    """
    areas = profile.get("interest_areas", [])
    user = profile.get("user", {})
    name = user.get("preferred_name", "")
    blocked = _format_blocked_topics(profile)

    if agent_role == "collector":
        # Collector needs: areas with keywords, blocked topics
        parts = [
            "Interest areas:",
            _format_areas_with_keywords(areas),
        ]
        if blocked:
            parts.append(blocked)
        return "\n\n".join(parts)

    if agent_role == "filter":
        # Filter needs: areas with weights + keywords, blocked topics, strictness
        strictness = profile.get("strictness", "moderate")
        parts = [
            "Interest areas (with relevance weights):",
            _format_areas_with_keywords(areas, include_weights=True),
        ]
        if blocked:
            parts.append(blocked)
        parts.append(f"Filtering strictness: {strictness}")
        return "\n\n".join(parts)

    if agent_role == "clusterer":
        # Clusterer needs: areas with keywords
        parts = [
            "Interest areas:",
            _format_areas_with_keywords(areas),
        ]
        return "\n\n".join(parts)

    if agent_role in ("researcher", "writer", "editor"):
        # Content agents need: area names, user name, style note
        area_names = [a.get("name", a.get("id", "?")) for a in areas]
        parts = []
        if name:
            parts.append(f"Reader: {name}")
        parts.append("Interest areas: " + ", ".join(area_names))
        parts.append(
            "Style: practical over theoretical, depth over breadth, "
            "prefers mechanisms/trade-offs, actionable outputs."
        )
        return "\n".join(parts)

    if agent_role == "translator":
        # Translator needs: user language
        lang = user.get("primary_language", "en")
        parts = [f"User's primary language: {lang}"]
        if name:
            parts.append(f"Reader: {name}")
        return "\n".join(parts)

    # Fallback: area names only
    area_names = [a.get("name", a.get("id", "?")) for a in areas]
    return "Interest areas: " + ", ".join(area_names)


