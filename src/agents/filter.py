"""Filter agent — evaluates item relevance and removes noise/duplicates before digest generation."""

import logging
from dataclasses import dataclass, field

from ..db.database import Database
from ..db.models import Item
from ..llm.provider import LLMProvider
from ..profile_defaults import build_agent_profile_prompt, get_scoring_thresholds
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
            user_profile_section=build_agent_profile_prompt(user_profile, "filter")
        )

    def update_profile(self, user_profile: dict) -> None:
        """Update the user profile used for filtering (e.g. after /setup)."""
        self.user_profile = user_profile
        self._prompt_template = self._load_prompt()
        self._prompt_template = self._format_prompt(
            user_profile_section=build_agent_profile_prompt(user_profile, "filter")
        )

    OUTPUT_SCHEMA = {
        "type": "object",
        "properties": {
            "kept_items": {
                "type": "array",
                "description": "Items to keep in the digest",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "Item ID"},
                        "relevance_score": {
                            "type": "number",
                            "description": "Relevance score 0.0-1.0",
                        },
                        "matched_areas": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "User interest area IDs this item matches",
                        },
                        "reason": {"type": "string", "description": "Brief reason why this is relevant"},
                    },
                    "required": ["id", "relevance_score", "reason"],
                },
            },
            "filtered_items": {
                "type": "array",
                "description": "Items to remove from the digest",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "Item ID"},
                        "relevance_score": {
                            "type": "number",
                            "description": "Relevance score 0.0-1.0",
                        },
                        "filter_type": {
                            "type": "string",
                            "enum": ["irrelevant", "duplicate", "noise", "shallow"],
                            "description": "Reason category for filtering",
                        },
                        "reason": {"type": "string", "description": "Specific reason for filtering"},
                        "duplicate_of": {
                            "type": "string",
                            "description": "ID of the kept item this duplicates (only for filter_type=duplicate)",
                        },
                    },
                    "required": ["id", "relevance_score", "filter_type", "reason"],
                },
            },
        },
        "required": ["kept_items", "filtered_items"],
    }

    async def process(
        self,
        items: list[Item],
        run_id: str | None = None,
    ) -> FilterResult:
        """Evaluate items for relevance and return filter decisions."""
        if not items:
            return FilterResult(kept_item_ids=[], filtered_items=[])

        user_message = self._build_user_message(items)
        valid_ids = {item.id for item in items}

        try:
            data = await self._call_llm_structured(
                user_message=user_message,
                tool_name="filter_items",
                tool_description="Evaluate items for relevance and decide which to keep or filter out",
                output_schema=self.OUTPUT_SCHEMA,
                run_id=run_id,
                max_tokens=4096,
                temperature=0.2,
            )
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

        except Exception as e:
            logger.warning("Failed to get structured filter response: %s — keeping all items", e)
            return FilterResult(
                kept_item_ids=[item.id for item in items],
                filtered_items=[],
            )

    def _build_user_message(self, items: list[Item]) -> str:
        strictness = self.user_profile.get("strictness",
                      self.user_profile.get("filtering_strictness", "moderate"))
        thresholds = get_scoring_thresholds(strictness)
        drop_below = thresholds["drop_below"]

        lines = [
            f"Filtering strictness: {strictness}",
            f"Drop-below threshold: {drop_below:.2f} — filter ONLY items with relevance_score strictly below this value; keep everything at or above it regardless of strictness preset.",
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
