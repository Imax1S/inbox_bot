"""Filter agent — evaluates item relevance and removes noise/duplicates before digest generation."""

import json
import logging
from dataclasses import dataclass, field

from ..db.database import Database
from ..db.models import Item
from ..llm.provider import LLMProvider
from .base import BaseAgent

logger = logging.getLogger(__name__)


@dataclass
class FilteredItem:
    id: str
    relevance_score: float
    filter_type: str  # "irrelevant", "duplicate", "noise", "shallow"
    reason: str
    duplicate_of: str | None = None


@dataclass
class FilterResult:
    kept_item_ids: list[str]
    filtered_items: list[FilteredItem]

    @classmethod
    def from_json(cls, data: dict, valid_ids: set[str]) -> "FilterResult":
        kept = [
            item["id"]
            for item in data.get("kept_items", [])
            if item.get("id") in valid_ids
        ]
        filtered = []
        for item in data.get("filtered_items", []):
            if item.get("id") not in valid_ids:
                continue
            filtered.append(FilteredItem(
                id=item["id"],
                relevance_score=item.get("relevance_score", 0.0),
                filter_type=item.get("filter_type", "irrelevant"),
                reason=item.get("reason", "No reason provided"),
                duplicate_of=item.get("duplicate_of"),
            ))
        return cls(kept_item_ids=kept, filtered_items=filtered)


class FilterAgent(BaseAgent):
    prompt_file = "filter.txt"
    agent_name = "filter"

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

    def update_profile(self, user_profile: dict) -> None:
        """Update the user profile used for filtering (e.g. after /setup)."""
        self.user_profile = user_profile
        self._prompt_template = self._load_prompt()
        self._prompt_template = self._format_prompt(
            user_profile_json=json.dumps(user_profile, ensure_ascii=False, indent=2)
        )

    async def process(
        self,
        items: list[Item],
        run_id: str | None = None,
    ) -> FilterResult:
        """Evaluate items for relevance and return filter decisions."""
        if not items:
            return FilterResult(kept_item_ids=[], filtered_items=[])

        user_message = self._build_user_message(items)

        response = await self._call_llm(
            user_message=user_message,
            run_id=run_id,
            max_tokens=4096,
            temperature=0.2,
        )

        valid_ids = {item.id for item in items}

        try:
            data = self._extract_json(response.content)
            result = FilterResult.from_json(data, valid_ids)

            # Safety: any items not mentioned in either list → keep them
            mentioned_ids = set(result.kept_item_ids) | {f.id for f in result.filtered_items}
            for item in items:
                if item.id not in mentioned_ids:
                    result.kept_item_ids.append(item.id)

            logger.info(
                "Filter result: %d kept, %d filtered",
                len(result.kept_item_ids),
                len(result.filtered_items),
            )
            return result

        except (json.JSONDecodeError, KeyError) as e:
            logger.warning("Failed to parse filter response: %s — keeping all items", e)
            return FilterResult(
                kept_item_ids=[item.id for item in items],
                filtered_items=[],
            )

    def _build_user_message(self, items: list[Item]) -> str:
        strictness = self.user_profile.get("filtering_strictness", "moderate")

        lines = [
            f"Filtering strictness: {strictness}",
            f"\nItems to evaluate ({len(items)} total):\n",
        ]
        for item in items:
            lines.append(
                f"- ID: {item.id}\n"
                f"  Type: {item.type.value}\n"
                f"  Summary: {item.summary}\n"
                f"  Tags: {', '.join(item.tags)}\n"
                f"  Language: {item.language}\n"
                f"  Raw content (first 500 chars): {item.raw_content[:500]}"
            )
        return "\n".join(lines)
