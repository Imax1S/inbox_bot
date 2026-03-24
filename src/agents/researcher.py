"""Researcher agent — produces research briefs via web search."""

import logging
from datetime import datetime
from uuid import uuid4

from ..db.database import Database
from ..db.models import Cluster, Item
from ..llm.provider import LLMProvider
from ..profile_defaults import build_agent_profile_prompt
from .base import BaseAgent

logger = logging.getLogger(__name__)


class ResearcherAgent(BaseAgent):
    prompt_file = "researcher.txt"
    agent_name = "researcher"
    MAX_TOKENS = 2048

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
            user_profile_section=build_agent_profile_prompt(user_profile, "researcher")
        )

    def update_profile(self, user_profile: dict) -> None:
        self.user_profile = user_profile
        self._prompt_template = self._load_prompt()
        self._prompt_template = self._format_prompt(
            user_profile_section=build_agent_profile_prompt(user_profile, "researcher")
        )

    async def process(
        self,
        cluster: Cluster,
        items: list[Item],
        run_id: str | None = None,
    ) -> str:
        """Produce a research brief via web search."""
        user_message = self._build_user_message(cluster, items)

        step_id = str(uuid4())
        started_at = datetime.now()

        try:
            response = await self.llm.generate_with_search(
                model=self.model,
                system_prompt=self._prompt_template,
                user_message=user_message,
                max_tokens=self.MAX_TOKENS,
                temperature=0.5,
            )

            if run_id:
                await self._log_step(
                    step_id=step_id,
                    run_id=run_id,
                    started_at=started_at,
                    response=response,
                    status="completed",
                )

            return response.content

        except Exception as e:
            if run_id:
                await self._log_step(
                    step_id=step_id,
                    run_id=run_id,
                    started_at=started_at,
                    response=None,
                    status="failed",
                    error=str(e),
                )
            raise

    def _build_user_message(self, cluster: Cluster, items: list[Item]) -> str:
        parts = [
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
