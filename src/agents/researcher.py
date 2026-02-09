"""Researcher agent — produces research briefs to fill gaps in source material."""

import json
import logging

from ..db.database import Database
from ..db.models import Cluster, Item
from ..llm.provider import LLMProvider
from .base import BaseAgent

logger = logging.getLogger(__name__)


class ResearcherAgent(BaseAgent):
    prompt_file = "researcher.txt"
    agent_name = "researcher"

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
        cluster: Cluster,
        items: list[Item],
        run_id: str | None = None,
        language: str = "Russian",
    ) -> str:
        """Produce a research brief for a cluster."""
        user_message = self._build_user_message(cluster, items, language)

        response = await self._call_llm(
            user_message=user_message,
            run_id=run_id,
            max_tokens=2048,
            temperature=0.7,
        )

        return response.content

    def _build_user_message(self, cluster: Cluster, items: list[Item], language: str = "Russian") -> str:
        parts = [
            f"## Output language: {language}",
            f"## Cluster: {cluster.title}",
            f"Editorial angle: {cluster.editorial_angle}",
            f"Target read time: {cluster.estimated_read_minutes} minutes",
            f"\n## Source Materials ({len(items)} items):\n",
        ]

        for i, item in enumerate(items, 1):
            parts.append(f"### Source {i}: {item.summary}")
            parts.append(f"Type: {item.type.value}")
            if item.source_url:
                parts.append(f"URL: {item.source_url}")
            # Include full content if available, otherwise raw content
            content = item.extracted_text or item.raw_content
            # Truncate per-item to fit context
            if len(content) > 4000:
                content = content[:4000] + "\n[...truncated]"
            parts.append(f"Content:\n{content}\n")

        return "\n".join(parts)
