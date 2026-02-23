"""Clusterer agent — groups items into coherent topic clusters."""

import json
import logging

from ..db.database import Database
from ..db.models import Cluster, ClusterResult, Item
from ..llm.provider import LLMProvider
from .base import BaseAgent

logger = logging.getLogger(__name__)


class ClustererAgent(BaseAgent):
    prompt_file = "clusterer.txt"
    agent_name = "clusterer"

    OUTPUT_SCHEMA = {
        "type": "object",
        "properties": {
            "clusters": {
                "type": "array",
                "description": "Topic clusters grouping related items",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": "Sequential cluster ID (e.g. 'cluster-1')",
                        },
                        "title": {
                            "type": "string",
                            "description": "Specific, concrete topic title",
                        },
                        "editorial_angle": {
                            "type": "string",
                            "description": "What concrete question or insight this cluster covers",
                        },
                        "item_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "IDs of items in this cluster (must match input IDs exactly)",
                        },
                        "estimated_read_minutes": {
                            "type": "integer",
                            "description": "Estimated read time in minutes (1-5)",
                        },
                        "priority": {
                            "type": "integer",
                            "description": "Priority ranking (1 = highest)",
                        },
                    },
                    "required": ["id", "title", "editorial_angle", "item_ids", "estimated_read_minutes", "priority"],
                },
            },
            "quick_bites_item_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "IDs of trivial items too short for a full cluster",
            },
        },
        "required": ["clusters", "quick_bites_item_ids"],
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
            user_profile_json=json.dumps(user_profile, ensure_ascii=False, indent=2)
        )

    async def process(
        self,
        items: list[Item],
        run_id: str | None = None,
    ) -> ClusterResult:
        """Group items into clusters."""
        user_message = self._build_user_message(items)

        try:
            data = await self._call_llm_structured(
                user_message=user_message,
                tool_name="create_clusters",
                tool_description="Group items into coherent topic clusters for the weekly digest",
                output_schema=self.OUTPUT_SCHEMA,
                run_id=run_id,
                max_tokens=2048,
                temperature=0.3,
            )
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

        except Exception as e:
            logger.warning("Failed to get structured clusterer response: %s — falling back", e)
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
