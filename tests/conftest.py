"""Shared test fixtures — MockLLMProvider, mock DB, sample data."""

from __future__ import annotations

from datetime import datetime

import pytest
import pytest_asyncio

from src.db.database import Database
from src.db.models import Item, ItemStatus, ItemType
from src.llm.provider import LLMResponse


class MockLLMProvider:
    """Fake LLM provider for tests.

    Args:
        structured_response: Dict returned by generate_structured().
        text_response: String returned by generate().
        should_raise: If set, both methods raise this exception.
    """

    def __init__(
        self,
        structured_response: dict | None = None,
        text_response: str = "",
        should_raise: Exception | None = None,
    ):
        self.structured_response = structured_response
        self.text_response = text_response
        self.should_raise = should_raise
        self.calls: list[dict] = []

    async def generate(
        self,
        model: str,
        system_prompt: str,
        user_message: str,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        self.calls.append({
            "method": "generate",
            "model": model,
            "user_message": user_message,
        })
        if self.should_raise:
            raise self.should_raise
        return LLMResponse(
            content=self.text_response,
            input_tokens=10,
            output_tokens=20,
            model=model,
        )

    async def generate_structured(
        self,
        model: str,
        system_prompt: str,
        user_message: str,
        tool_name: str,
        tool_description: str,
        output_schema: dict,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        self.calls.append({
            "method": "generate_structured",
            "model": model,
            "tool_name": tool_name,
            "user_message": user_message,
        })
        if self.should_raise:
            raise self.should_raise
        return LLMResponse(
            content="{}",
            input_tokens=10,
            output_tokens=20,
            model=model,
            structured_output=self.structured_response,
        )


@pytest_asyncio.fixture
async def mock_db(tmp_path):
    """In-memory SQLite database, initialized with schema."""
    db = Database(str(tmp_path / "test.db"))
    await db.init()
    return db


@pytest.fixture
def sample_user_profile():
    return {
        "user": {"preferred_name": "Test", "primary_language": "en"},
        "interest_areas": [
            {"id": "ai", "weight": 0.8, "keywords": ["machine learning", "llm"]},
        ],
        "filtering_strictness": "moderate",
    }


@pytest.fixture
def sample_items():
    now = datetime.now()
    return [
        Item(
            id="item-1",
            created_at=now,
            type=ItemType.ARTICLE,
            raw_content="Article about AI safety",
            summary="AI safety overview",
            tags=["ai", "safety"],
            language="en",
            week_id="2026-W08",
            status=ItemStatus.COLLECTED,
        ),
        Item(
            id="item-2",
            created_at=now,
            type=ItemType.TOPIC_SEED,
            raw_content="Interesting note on system design",
            summary="System design patterns",
            tags=["engineering", "design"],
            language="en",
            week_id="2026-W08",
            status=ItemStatus.COLLECTED,
        ),
        Item(
            id="item-3",
            created_at=now,
            type=ItemType.CONTEXT_NOTE,
            raw_content="Random thought about productivity",
            summary="Productivity tip",
            tags=["productivity"],
            language="en",
            week_id="2026-W08",
            status=ItemStatus.COLLECTED,
        ),
    ]
