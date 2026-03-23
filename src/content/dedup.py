"""Pre-filter deduplication using Jaccard similarity on item summaries."""

from __future__ import annotations

import logging

from ..db.models import Item

logger = logging.getLogger(__name__)


def _tokenize(text: str) -> set[str]:
    """Lowercase word tokens from a string."""
    return set(text.lower().split())


def find_near_duplicates(
    items: list[Item],
    threshold: float = 0.6,
) -> list[tuple[str, str]]:
    """Return pairs of (duplicate_id, keep_id) based on summary similarity.

    Keeps the item with longer extracted_text (richer content).
    Uses Jaccard similarity on lowercased word tokens of summaries.
    """
    if len(items) < 2:
        return []

    # Pre-compute token sets
    token_sets: list[tuple[Item, set[str]]] = []
    for item in items:
        tokens = _tokenize(item.summary) if item.summary else set()
        token_sets.append((item, tokens))

    duplicates: list[tuple[str, str]] = []
    seen_as_dup: set[str] = set()

    for i in range(len(token_sets)):
        if token_sets[i][0].id in seen_as_dup:
            continue
        for j in range(i + 1, len(token_sets)):
            if token_sets[j][0].id in seen_as_dup:
                continue

            item_a, tokens_a = token_sets[i]
            item_b, tokens_b = token_sets[j]

            if not tokens_a or not tokens_b:
                continue

            intersection = len(tokens_a & tokens_b)
            union = len(tokens_a | tokens_b)
            similarity = intersection / union if union > 0 else 0.0

            if similarity >= threshold:
                # Keep the item with richer content
                len_a = len(item_a.extracted_text or "")
                len_b = len(item_b.extracted_text or "")

                if len_a >= len_b:
                    duplicates.append((item_b.id, item_a.id))
                    seen_as_dup.add(item_b.id)
                else:
                    duplicates.append((item_a.id, item_b.id))
                    seen_as_dup.add(item_a.id)
                    break  # item_a is a dup, skip remaining comparisons for it

    return duplicates
