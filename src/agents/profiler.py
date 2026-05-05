"""Profiler agent — extracts user interests from free-form text to build a user profile."""

import logging

from ..db.database import Database
from ..llm.provider import LLMProvider
from .base import BaseAgent

logger = logging.getLogger(__name__)


class ProfilerAgent(BaseAgent):
    prompt_file = "profiler.txt"
    agent_name = "profiler"

    OUTPUT_SCHEMA = {
        "type": "object",
        "properties": {
            "user": {
                "type": "object",
                "description": "User identity",
                "properties": {
                    "preferred_name": {
                        "type": "string",
                        "description": "User's name extracted from the description, or empty string",
                    },
                    "primary_language": {
                        "type": "string",
                        "description": "ISO 639-1 code of the description language (e.g. 'en', 'ru')",
                    },
                },
            },
            "interest_areas": {
                "type": "array",
                "description": "All interest areas mentioned by the user",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": "Descriptive snake_case ID",
                        },
                        "name": {
                            "type": "string",
                            "description": "Human readable area name",
                        },
                        "weight": {
                            "type": "number",
                            "description": "Importance weight 0.40-0.95 based on emphasis in description",
                        },
                        "keywords": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "10-20 keywords to help identify content in this area",
                        },
                    },
                    "required": ["id", "name", "weight", "keywords"],
                },
            },
            "noise_topics": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Topics the user explicitly does NOT want",
            },
        },
        "required": ["interest_areas"],
    }

    def __init__(self, llm: LLMProvider, model: str, db: Database):
        super().__init__(llm, model, db)

    async def extract_interests(self, user_text: str) -> dict:
        """Extract structured interests from user's free-form description.

        Returns a dict with: user, interest_areas, noise_topics.
        """
        try:
            data = await self._call_llm_structured(
                user_message=f"User's self-description:\n\n{user_text}",
                tool_name="extract_interests",
                tool_description="Extract user interest areas and noise topics from their self-description",
                output_schema=self.OUTPUT_SCHEMA,
                max_tokens=4096,
                temperature=0.3,
            )
            if "interest_areas" not in data:
                data["interest_areas"] = []
            return data
        except Exception as e:
            logger.exception(
                "Profiler agent failed (%s: %s) — returning empty profile",
                type(e).__name__,
                e,
            )
            return {"interest_areas": [], "error": str(e)}

    @staticmethod
    def build_profile(
        extracted: dict,
        confirmed_areas: list[dict],
        strictness: str = "moderate",
    ) -> dict:
        """Assemble a v3 user profile from extraction + questionnaire results.

        The profile contains only user-facing data. System defaults (scoring,
        processing priorities, etc.) are handled by profile_defaults.py.
        """
        user_data = extracted.get("user", {})

        profile: dict = {
            "version": "3.0",
            "user": {
                "preferred_name": user_data.get("preferred_name", ""),
                "primary_language": user_data.get("primary_language", "en"),
            },
            "interest_areas": [],
            "blocked_topics": extracted.get("noise_topics", []),
            "strictness": strictness,
        }

        for area in confirmed_areas:
            if area.get("weight", 0) <= 0:
                continue
            profile["interest_areas"].append({
                "id": area["id"],
                "name": area.get("name", area["id"]),
                "weight": area["weight"],
                "keywords": area.get("keywords", []),
            })

        return profile
