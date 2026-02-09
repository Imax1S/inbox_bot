"""Editor agent — assembles individual articles into a polished weekly magazine."""

import json
import logging
from datetime import datetime

from ..db.database import Database
from ..db.models import Cluster, ClusterResult, Item
from ..llm.provider import LLMProvider
from .base import BaseAgent

logger = logging.getLogger(__name__)


class EditorAgent(BaseAgent):
    prompt_file = "editor.txt"
    agent_name = "editor"

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
        articles: dict[str, str],  # cluster_id -> article markdown
        cluster_result: ClusterResult,
        quick_bites_items: list[Item],
        all_items: list[Item],
        week_id: str,
        run_id: str | None = None,
    ) -> str:
        """Assemble the final magazine from all articles."""
        user_message = self._build_user_message(
            articles, cluster_result, quick_bites_items, all_items, week_id
        )

        response = await self._call_llm(
            user_message=user_message,
            run_id=run_id,
            max_tokens=8192,
            temperature=0.5,
        )

        return response.content

    def _build_user_message(
        self,
        articles: dict[str, str],
        cluster_result: ClusterResult,
        quick_bites_items: list[Item],
        all_items: list[Item],
        week_id: str,
    ) -> str:
        # Calculate date range from items
        if all_items:
            dates = [item.created_at for item in all_items]
            min_date = min(dates)
            max_date = max(dates)
            date_range = f"{min_date.strftime('%b %d')}–{max_date.strftime('%b %d, %Y')}"
        else:
            date_range = week_id

        # Parse week number from week_id
        week_number = week_id.split("-W")[-1] if "-W" in week_id else week_id

        total_read_min = sum(
            c.estimated_read_minutes for c in cluster_result.clusters
        )

        parts = [
            f"## Metadata",
            f"Week: {week_id}",
            f"Date range: {date_range}",
            f"Week number: {week_number}",
            f"Total items: {len(all_items)}",
            f"Topic count: {len(cluster_result.clusters)}",
            f"Total estimated read time: {total_read_min} minutes",
            "",
        ]

        # Articles ordered by priority
        sorted_clusters = sorted(
            cluster_result.clusters, key=lambda c: c.priority
        )

        parts.append("## Articles\n")
        for cluster in sorted_clusters:
            article_text = articles.get(cluster.id, "")
            cluster_items = [
                item for item in all_items if item.id in cluster.item_ids
            ]
            source_urls = [
                item.source_url for item in cluster_items if item.source_url
            ]

            parts.append(f"### {cluster.title}")
            parts.append(f"Read time: ~{cluster.estimated_read_minutes} min")
            if source_urls:
                parts.append(f"Sources: {', '.join(source_urls)}")
            parts.append(f"\n{article_text}\n")
            parts.append("---\n")

        # Quick bites
        if quick_bites_items:
            parts.append("## Quick Bites Items\n")
            for item in quick_bites_items:
                url_part = f" [{item.source_url}]" if item.source_url else ""
                parts.append(f"- {item.summary}{url_part}")
            parts.append("")

        # All source URLs for the appendix
        parts.append("## All Source URLs\n")
        for item in all_items:
            if item.source_url:
                parts.append(
                    f"- {item.source_url} — collected {item.created_at.strftime('%a, %b %d')}"
                )

        return "\n".join(parts)
