"""Writer agent — writes magazine-quality articles from clusters + research briefs."""

import json
import logging

from ..db.database import Database
from ..db.models import Cluster, Item
from ..llm.provider import LLMProvider
from .base import BaseAgent

logger = logging.getLogger(__name__)


class WriterAgent(BaseAgent):
    prompt_file = "writer.txt"
    agent_name = "writer"

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
        research_brief: str,
        run_id: str | None = None,
        language: str = "Russian",
    ) -> str:
        """Write an article for a cluster."""
        user_message = self._build_user_message(cluster, items, research_brief, language)

        # Target word count based on read time (~250 words/minute)
        target_words = cluster.estimated_read_minutes * 250
        max_tokens = max(2048, target_words * 2)  # tokens ≈ words * 1.3, with margin

        response = await self._call_llm(
            user_message=user_message,
            run_id=run_id,
            max_tokens=min(max_tokens, 8192),
            temperature=0.8,
        )

        return response.content

    def _build_user_message(
        self,
        cluster: Cluster,
        items: list[Item],
        research_brief: str,
        language: str = "Russian",
    ) -> str:
        parts = [
            f"## Output language: {language}",
            f"## Topic: {cluster.title}",
            f"Editorial angle: {cluster.editorial_angle}",
            f"Target read time: {cluster.estimated_read_minutes} minutes (~{cluster.estimated_read_minutes * 250} words)",
            f"\n## Source Materials ({len(items)} items):\n",
        ]

        for i, item in enumerate(items, 1):
            parts.append(f"### Source {i}")
            parts.append(f"Summary: {item.summary}")
            if item.source_url:
                parts.append(f"URL: {item.source_url}")
            content = item.extracted_text or item.raw_content
            if len(content) > 5000:
                content = content[:5000] + "\n[...truncated]"
            parts.append(f"Content:\n{content}\n")

        parts.append(f"\n## Research Brief:\n{research_brief}")

        return "\n".join(parts)
