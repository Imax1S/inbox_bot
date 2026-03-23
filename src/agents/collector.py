"""Collector agent — classifies, summarizes, and tags each incoming message."""

import logging
from dataclasses import dataclass

from ..db.database import Database
from ..db.models import ItemType
from ..llm.provider import LLMProvider
from ..profile_defaults import build_agent_profile_prompt
from .base import BaseAgent

logger = logging.getLogger(__name__)


@dataclass
class CollectorResult:
    summary: str
    tags: list[str]
    language: str


class CollectorAgent(BaseAgent):
    prompt_file = "collector.txt"
    agent_name = "collector"

    OUTPUT_SCHEMA = {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "2-3 sentence summary capturing the key point and why it matters",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "3-7 lowercase snake_case semantic tags for clustering (e.g. 'ai_regulation', 'system_design')",
            },
            "language": {
                "type": "string",
                "description": "ISO 639-1 language code of the content (e.g. 'en', 'ru')",
            },
        },
        "required": ["summary", "tags", "language"],
    }

    def __init__(
        self,
        llm: LLMProvider,
        model: str,
        db: Database,
        user_profile: dict,
    ):
        super().__init__(llm, model, db)
        self.user_profile = user_profile
        self._prompt_template = self._format_prompt(
            user_profile_section=build_agent_profile_prompt(user_profile, "collector")
        )

    async def process(
        self,
        raw_content: str,
        extracted_text: str | None,
        item_type: ItemType,
        run_id: str | None = None,
    ) -> CollectorResult:
        """Process an incoming message and return summary + tags + language."""
        user_message = self._build_user_message(raw_content, extracted_text, item_type)

        try:
            data = await self._call_llm_structured(
                user_message=user_message,
                tool_name="classify_content",
                tool_description="Classify and summarize the incoming message",
                output_schema=self.OUTPUT_SCHEMA,
                run_id=run_id,
                max_tokens=1024,
                temperature=0.3,
            )
            return CollectorResult(
                summary=data.get("summary", raw_content[:200]),
                tags=data.get("tags", []),
                language=data.get("language", "ru"),
            )
        except Exception as e:
            logger.warning("Failed to get structured collector response: %s", e)
            return CollectorResult(
                summary=raw_content[:200],
                tags=[],
                language="ru",
            )

    def _build_user_message(
        self,
        raw_content: str,
        extracted_text: str | None,
        item_type: ItemType,
    ) -> str:
        parts = [f"Type: {item_type.value}", f"User message: {raw_content}"]
        if extracted_text:
            # Limit extracted text sent to collector to save tokens
            truncated = extracted_text[:3000]
            if len(extracted_text) > 3000:
                truncated += "\n[...truncated]"
            parts.append(f"Extracted article text:\n{truncated}")
        return "\n\n".join(parts)
