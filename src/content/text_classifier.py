"""Deterministic message classification — no LLM needed."""

import re

from ..db.models import ItemType

URL_PATTERN = re.compile(r"https?://\S+")


def classify_message(text: str) -> tuple[ItemType, str | None]:
    """Classify a user message and extract URL if present.

    Returns (item_type, extracted_url_or_none).
    """
    urls = URL_PATTERN.findall(text)
    if urls:
        return ItemType.ARTICLE, urls[0]

    # Short messages (≤10 words) without URLs are context notes
    if len(text.split()) <= 10:
        return ItemType.CONTEXT_NOTE, None

    return ItemType.TOPIC_SEED, None
