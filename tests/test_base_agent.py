"""Tests for BaseAgent._call_llm_structured via a minimal DummyAgent."""

import pytest

from src.agents.base import BaseAgent
from tests.conftest import MockLLMProvider


class DummyAgent(BaseAgent):
    prompt_file = ""
    agent_name = "dummy"


@pytest.mark.asyncio
async def test_call_llm_structured_returns_dict(mock_db):
    expected = {"key": "value", "count": 42}
    provider = MockLLMProvider(structured_response=expected)
    agent = DummyAgent(llm=provider, model="test-model", db=mock_db)

    result = await agent._call_llm_structured(
        user_message="test",
        tool_name="test_tool",
        tool_description="A test tool",
        output_schema={"type": "object"},
    )
    assert result == expected
    assert len(provider.calls) == 1
    assert provider.calls[0]["method"] == "generate_structured"


@pytest.mark.asyncio
async def test_call_llm_structured_logs_step(mock_db):
    provider = MockLLMProvider(structured_response={"ok": True})
    agent = DummyAgent(llm=provider, model="test-model", db=mock_db)

    run_id = "test-run-123"
    await agent._call_llm_structured(
        user_message="test",
        tool_name="test_tool",
        tool_description="A test tool",
        output_schema={"type": "object"},
        run_id=run_id,
    )

    # Verify step was logged by querying the DB directly
    import aiosqlite

    async with aiosqlite.connect(mock_db.db_path) as db:
        async with db.execute(
            "SELECT * FROM step_logs WHERE run_id = ?", (run_id,)
        ) as cursor:
            rows = await cursor.fetchall()
            assert len(rows) == 1


@pytest.mark.asyncio
async def test_call_llm_structured_raises_on_none_output(mock_db):
    provider = MockLLMProvider(structured_response=None)
    agent = DummyAgent(llm=provider, model="test-model", db=mock_db)

    with pytest.raises(ValueError, match="no structured output"):
        await agent._call_llm_structured(
            user_message="test",
            tool_name="test_tool",
            tool_description="A test tool",
            output_schema={"type": "object"},
        )


@pytest.mark.asyncio
async def test_call_llm_structured_raises_propagates(mock_db):
    provider = MockLLMProvider(should_raise=RuntimeError("API down"))
    agent = DummyAgent(llm=provider, model="test-model", db=mock_db)

    run_id = "test-run-fail"
    with pytest.raises(RuntimeError, match="API down"):
        await agent._call_llm_structured(
            user_message="test",
            tool_name="test_tool",
            tool_description="A test tool",
            output_schema={"type": "object"},
            run_id=run_id,
        )

    # Verify failure was logged
    import aiosqlite

    async with aiosqlite.connect(mock_db.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM step_logs WHERE run_id = ?", (run_id,)
        ) as cursor:
            rows = await cursor.fetchall()
            assert len(rows) == 1
            assert rows[0]["status"] == "failed"
            assert "API down" in rows[0]["error"]
