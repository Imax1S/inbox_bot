"""Profiler agent — extracts user interests from free-form text to build a user profile."""

import json
import logging

from ..db.database import Database
from ..llm.provider import LLMProvider
from .base import BaseAgent

logger = logging.getLogger(__name__)


# Template for assembling the full profile from extracted + confirmed data
_PROFILE_TEMPLATE = {
    "version": "2.0",
    "processing_priorities": {
        "ranking_rules": [
            "Prefer content that explains mechanisms (inputs → process → outputs) and shows trade-offs.",
            "Prefer structured material (frameworks, checklists, diagrams, evaluation methods).",
            "Prefer primary sources (official docs, laws, standards, papers) over commentary.",
            "Prefer content that can be applied to real tasks/projects within 1–4 weeks.",
            "Deprioritize anything that is pseudo-scientific, mystical, or unfalsifiable.",
        ],
        "source_quality_preferences": {
            "preferred_source_types": [
                "official_docs",
                "peer_reviewed_papers",
                "engineering_blogs_from_major_orgs",
                "books_textbooks",
                "reputable_policy_briefs",
            ],
            "deprioritize_source_types": [
                "generic_listicles",
                "opinion_only_threads",
                "unverified_news_roundups",
                "growth_hacks_without_data",
            ],
        },
        "format_preferences": {
            "preferred_formats": [
                "structured_notes",
                "frameworks_and_templates",
                "case_studies",
                "comparative_analysis",
                "implementation_guides",
            ],
        },
    },
    "news_filtering": {
        "mode": "fundamental_only",
        "definition_of_fundamental": [
            "Major institutional decisions or laws with real enforcement impact",
            "Major geopolitical events that change incentives/alliances materially",
            "Major tech shifts that change architectures or capabilities",
            "Major economic regime changes with broad second-order effects",
        ],
        "hard_deprioritize_topics": [
            "celebrity news",
            "social media drama",
            "minor product launches",
            "speculation without confirmation",
        ],
    },
    "noise_filters": {
        "hard_exclude_keywords": [],
        "soft_deprioritize_keywords": [
            "hot take",
            "top-10",
        ],
        "content_patterns_to_deprioritize": [
            "claims without sources",
            "unfalsifiable claims",
            "overly long with low information density",
            "salesy marketing tone",
        ],
    },
}

# Scoring thresholds per strictness level
STRICTNESS_THRESHOLDS = {
    "strict": {"drop_below": 0.40, "review_if_between": [0.40, 0.55], "keep_if_above": 0.55},
    "moderate": {"drop_below": 0.25, "review_if_between": [0.25, 0.45], "keep_if_above": 0.45},
    "relaxed": {"drop_below": 0.15, "review_if_between": [0.15, 0.35], "keep_if_above": 0.35},
}


class ProfilerAgent(BaseAgent):
    prompt_file = "profiler.txt"
    agent_name = "profiler"

    def __init__(self, llm: LLMProvider, model: str, db: Database):
        super().__init__(llm, model, db)

    async def extract_interests(self, user_text: str) -> dict:
        """Extract structured interests from user's free-form description.

        Returns a dict with: user, interest_areas, noise_topics, detected_languages.
        """
        response = await self._call_llm(
            user_message=f"User's self-description:\n\n{user_text}",
            max_tokens=4096,
            temperature=0.3,
        )

        try:
            data = self._extract_json(response.content)
            # Validate minimum structure
            if "interest_areas" not in data:
                data["interest_areas"] = []
            return data
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning("Failed to parse profiler response: %s", e)
            return {"interest_areas": [], "error": str(e)}

    @staticmethod
    def build_profile(
        extracted: dict,
        confirmed_areas: list[dict],
        strictness: str = "moderate",
    ) -> dict:
        """Assemble a complete user profile from extraction + questionnaire results.

        Args:
            extracted: Raw output from extract_interests()
            confirmed_areas: List of interest areas with adjusted weights from user
            strictness: "strict", "moderate", or "relaxed"
        """
        profile = json.loads(json.dumps(_PROFILE_TEMPLATE))  # deep copy

        # User info
        user_data = extracted.get("user", {})
        profile["user"] = {
            "preferred_name": user_data.get("preferred_name", ""),
            "primary_language": user_data.get("primary_language", "en"),
            "core_style_preferences": user_data.get("core_style_preferences", {
                "practicality_over_theory": True,
                "depth_over_breadth": True,
                "systems_thinking": True,
                "prefers_actionable_outputs": True,
            }),
        }

        # Interest areas — use confirmed (user-adjusted) data
        profile["interest_areas"] = []
        for area in confirmed_areas:
            if area.get("weight", 0) <= 0:
                continue  # User removed this area
            profile["interest_areas"].append({
                "id": area["id"],
                "weight": area["weight"],
                "keywords": area.get("keywords", []),
                "must_include_signals": area.get("must_include_signals", []),
                "avoid_signals": area.get("avoid_signals", []),
            })

        # Noise filters from extraction
        noise = extracted.get("noise_topics", [])
        profile["noise_filters"]["hard_exclude_keywords"] = noise

        # Scoring hints adjusted by strictness
        thresholds = STRICTNESS_THRESHOLDS.get(strictness, STRICTNESS_THRESHOLDS["moderate"])
        profile["scoring_hints_for_python"] = {
            "base_score": 0.0,
            "area_weight_multiplier": 1.0,
            "keyword_match": {
                "title_multiplier": 2.2,
                "summary_multiplier": 1.3,
                "body_multiplier": 1.0,
            },
            "bonus_signals": {
                "has_framework_or_checklist": 0.14,
                "has_case_study": 0.12,
                "has_tradeoffs": 0.10,
                "has_primary_sources": 0.18,
                "has_code_or_repro_steps": 0.10,
            },
            "penalty_signals": {
                "no_sources": -0.22,
                "clickbait_title": -0.15,
                "pure_opinion": -0.12,
                "salesy_marketing": -0.18,
                "pseudo_science": -1.0,
            },
            "thresholds": thresholds,
            "final_score_clamp": [0.0, 1.0],
        }

        # Store strictness for use by FilterAgent
        profile["filtering_strictness"] = strictness

        return profile
