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
class KeptItem:
    id: str
    relevance_score: float
    reason: str


@dataclass
class FilterResult:
    kept_item_ids: list[str]
    filtered_items: list[FilteredItem]
    kept_items_with_scores: list[KeptItem] = field(default_factory=list)

    @classmethod
    def from_json(cls, data: dict, valid_ids: set[str]) -> "FilterResult":
        kept = []
        kept_with_scores = []
        for item in data.get("kept_items", []):
            if item.get("id") not in valid_ids:
                continue
            kept.append(item["id"])
            kept_with_scores.append(KeptItem(
                id=item["id"],
                relevance_score=item.get("relevance_score", 0.0),
                reason=item.get("reason", ""),
            ))
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
        return cls(kept_item_ids=kept, filtered_items=filtered, kept_items_with_scores=kept_with_scores)


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

    _BATCH_SIZE = 30

    async def process(
        self,
        items: list[Item],
        run_id: str | None = None,
        target_read_minutes: int | None = None,
    ) -> FilterResult:
        """Evaluate items for relevance and return filter decisions."""
        if not items:
            return FilterResult(kept_item_ids=[], filtered_items=[])

        if len(items) <= self._BATCH_SIZE:
            return await self._process_batch(items, run_id, target_read_minutes)

        # Split into batches and merge results
        all_kept: list[str] = []
        all_filtered: list[FilteredItem] = []
        all_kept_with_scores: list[KeptItem] = []

        for i in range(0, len(items), self._BATCH_SIZE):
            batch = items[i : i + self._BATCH_SIZE]
            result = await self._process_batch(batch, run_id, target_read_minutes)
            all_kept.extend(result.kept_item_ids)
            all_filtered.extend(result.filtered_items)
            all_kept_with_scores.extend(result.kept_items_with_scores)

        logger.info(
            "Filter batched result: %d kept, %d filtered across %d batches",
            len(all_kept),
            len(all_filtered),
            (len(items) + self._BATCH_SIZE - 1) // self._BATCH_SIZE,
        )
        return FilterResult(
            kept_item_ids=all_kept,
            filtered_items=all_filtered,
            kept_items_with_scores=all_kept_with_scores,
        )

    async def _process_batch(
        self,
        items: list[Item],
        run_id: str | None = None,
        target_read_minutes: int | None = None,
    ) -> FilterResult:
        """Process a single batch of items through the filter."""
        user_message = self._build_user_message(items, target_read_minutes)
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

    def _build_user_message(
        self,
        items: list[Item],
        target_read_minutes: int | None = None,
    ) -> str:
        strictness = self.user_profile.get("filtering_strictness", "moderate")
        drop_below = (
            self.user_profile
            .get("scoring_hints_for_python", {})
            .get("thresholds", {})
            .get("drop_below", None)
        )

        lines = [
            f"Filtering strictness: {strictness}",
        ]
        if drop_below is not None:
            lines.append(f"Drop-below threshold override: {drop_below:.2f} — filter ONLY items with relevance_score strictly below this value; keep everything at or above it regardless of strictness preset.")

        # Budget hint for the LLM
        if target_read_minutes and len(items) > 20:
            max_items_hint = target_read_minutes * 2
            lines.append(
                f"\nBudget context: Target digest is {target_read_minutes} minutes. "
                f"With {len(items)} items, filtering should be more aggressive to stay within budget. "
                f"Aim to keep approximately {max_items_hint} items maximum "
                f"(assuming ~2 min average per item when some go to quick bites)."
            )

        lines.append(f"\nItems to evaluate ({len(items)} total):\n")

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
