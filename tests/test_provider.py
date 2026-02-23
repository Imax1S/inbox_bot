"""Tests for LLMResponse dataclass — structured_output field."""

from src.llm.provider import LLMResponse


def test_llm_response_structured_output_field():
    data = {"summary": "test", "tags": ["a"]}
    resp = LLMResponse(
        content="{}",
        input_tokens=10,
        output_tokens=20,
        model="test-model",
        structured_output=data,
    )
    assert resp.structured_output == data
    assert resp.structured_output["summary"] == "test"


def test_llm_response_structured_output_default_none():
    resp = LLMResponse(
        content="hello",
        input_tokens=5,
        output_tokens=10,
        model="test-model",
    )
    assert resp.structured_output is None
