"""Collector agent — classifies, summarizes, and tags each incoming message."""

import json
import logging
from dataclasses import dataclass

from ..db.database import Database
from ..db.models import ItemType
from ..llm.provider import LLMProvider
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
            user_profile_json=json.dumps(user_profile, ensure_ascii=False, indent=2)
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

        response = await self._call_llm(
            user_message=user_message,
            run_id=run_id,
            max_tokens=1024,
            temperature=0.3,
        )

        try:
            data = self._extract_json(response.content)
            return CollectorResult(
                summary=data.get("summary", raw_content[:200]),
                tags=data.get("tags", []),
                language=data.get("language", "ru"),
            )
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning("Failed to parse collector response: %s", e)
            # Fallback: use raw content as summary
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
