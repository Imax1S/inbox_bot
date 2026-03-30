"""Low-cost real-API smoke-test helpers."""

from dataclasses import replace

from .agents.clusterer import ClustererAgent
from .agents.editor import EditorAgent
from .agents.filter import FilterAgent
from .agents.researcher import ResearcherAgent
from .agents.translator import TranslatorAgent
from .agents.writer import WriterAgent
from .db.models import Item

SMOKE_ITEM_LIMIT_DEFAULT = 2
SMOKE_ITEM_LIMIT_MAX = 3
SMOKE_RAW_CONTENT_MAX_CHARS = 600
SMOKE_EXTRACTED_TEXT_MAX_CHARS = 1200
SMOKE_SUMMARY_MAX_CHARS = 220


def get_smoke_models(provider: str) -> tuple[str, str]:
    """Return (fast_model, quality_model) for low-cost real smoke tests."""
    if provider == "openai":
        return ("gpt-4.1-nano", "gpt-4.1-mini")
    return ("claude-haiku-4-5-20251001", "claude-haiku-4-5-20251001")


def prepare_smoke_test_items(items: list[Item], limit: int = SMOKE_ITEM_LIMIT_DEFAULT) -> list[Item]:
    """Pick a small, cheap subset of items and truncate verbose fields."""
    if not items:
        return []

    bounded_limit = max(1, min(limit, SMOKE_ITEM_LIMIT_MAX))
    selected = sorted(
        items,
        key=lambda item: (
            len(item.extracted_text or item.raw_content or ""),
            item.created_at,
        ),
    )[:bounded_limit]

    prepared: list[Item] = []
    for item in selected:
        raw_content = item.raw_content[:SMOKE_RAW_CONTENT_MAX_CHARS]
        if len(item.raw_content) > SMOKE_RAW_CONTENT_MAX_CHARS:
            raw_content += "\n[...truncated for smoke test]"

        extracted_text = item.extracted_text
        if extracted_text and len(extracted_text) > SMOKE_EXTRACTED_TEXT_MAX_CHARS:
            extracted_text = (
                extracted_text[:SMOKE_EXTRACTED_TEXT_MAX_CHARS]
                + "\n[...truncated for smoke test]"
            )

        summary = item.summary[:SMOKE_SUMMARY_MAX_CHARS]
        prepared.append(
            replace(
                item,
                raw_content=raw_content,
                extracted_text=extracted_text,
                summary=summary,
            )
        )

    return prepared


class SmokeFilterAgent(FilterAgent):
    MAX_TOKENS = 1024


class SmokeClustererAgent(ClustererAgent):
    MAX_TOKENS = 1536


class SmokeResearcherAgent(ResearcherAgent):
    MAX_TOKENS = 768


class SmokeWriterAgent(WriterAgent):
    MIN_MAX_TOKENS = 768
    MAX_MAX_TOKENS = 1024


class SmokeEditorAgent(EditorAgent):
    MAX_TOKENS = 1536


class SmokeTranslatorAgent(TranslatorAgent):
    MAX_TOKENS = 1536
