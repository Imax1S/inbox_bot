"""Clusterer agent — groups items into coherent topic clusters."""

import json
import logging

from ..db.database import Database
from ..db.models import ClusterResult, Item
from ..llm.provider import LLMProvider
from .base import BaseAgent

logger = logging.getLogger(__name__)


class ClustererAgent(BaseAgent):
    prompt_file = "clusterer.txt"
    agent_name = "clusterer"

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
        items: list[Item],
        run_id: str | None = None,
    ) -> ClusterResult:
        """Group items into clusters."""
        user_message = self._build_user_message(items)

        response = await self._call_llm(
            user_message=user_message,
            run_id=run_id,
            max_tokens=2048,
            temperature=0.3,
        )

        try:
            data = self._extract_json(response.content)
            result = ClusterResult.from_json(data)

            # Validate that all referenced item IDs exist
            valid_ids = {item.id for item in items}
            for cluster in result.clusters:
                cluster.item_ids = [
                    iid for iid in cluster.item_ids if iid in valid_ids
                ]
            result.quick_bites_item_ids = [
                iid for iid in result.quick_bites_item_ids if iid in valid_ids
            ]

            return result

        except (json.JSONDecodeError, KeyError) as e:
            logger.warning("Failed to parse clusterer response: %s — falling back", e)
            # Fallback: put everything in one cluster
            from ..db.models import Cluster

            return ClusterResult(
                clusters=[
                    Cluster(
                        id="cluster-1",
                        title="This Week's Highlights",
                        editorial_angle="A collection of this week's items",
                        item_ids=[item.id for item in items],
                        estimated_read_minutes=5,
                        priority=1,
                    )
                ],
                quick_bites_item_ids=[],
            )

    def _build_user_message(self, items: list[Item]) -> str:
        lines = [f"Items to cluster ({len(items)} total):\n"]
        for item in items:
            lines.append(
                f"- ID: {item.id}\n"
                f"  Type: {item.type.value}\n"
                f"  Summary: {item.summary}\n"
                f"  Tags: {', '.join(item.tags)}\n"
                f"  Language: {item.language}"
            )
        return "\n".join(lines)
