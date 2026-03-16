"""LLM provider abstraction with token tracking for Anthropic and OpenAI."""

import json
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class LLMResponse:
    content: str
    input_tokens: int
    output_tokens: int
    model: str
    structured_output: dict | None = field(default=None)


class LLMProvider(Protocol):
    async def generate(
        self,
        model: str,
        system_prompt: str,
        user_message: str,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse: ...

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
    ) -> LLMResponse: ...


class AnthropicProvider:
    def __init__(self, api_key: str):
        from anthropic import AsyncAnthropic

        self.client = AsyncAnthropic(api_key=api_key)

    async def generate(
        self,
        model: str,
        system_prompt: str,
        user_message: str,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        response = await self.client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )
        return LLMResponse(
            content=response.content[0].text,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
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
        response = await self.client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
            tools=[{
                "name": tool_name,
                "description": tool_description,
                "input_schema": output_schema,
            }],
            tool_choice={"type": "tool", "name": tool_name},
        )
        structured = None
        for block in response.content:
            if block.type == "tool_use":
                structured = block.input
                break
        return LLMResponse(
            content=json.dumps(structured) if structured else "",
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            model=model,
            structured_output=structured,
        )


class OpenAIProvider:
    def __init__(self, api_key: str):
        from openai import AsyncOpenAI

        self.client = AsyncOpenAI(api_key=api_key)

    async def generate(
        self,
        model: str,
        system_prompt: str,
        user_message: str,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        response = await self.client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
        )
        choice = response.choices[0]
        usage = response.usage
        return LLMResponse(
            content=choice.message.content or "",
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
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
        response = await self.client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            tools=[{
                "type": "function",
                "function": {
                    "name": tool_name,
                    "description": tool_description,
                    "parameters": output_schema,
                },
            }],
            tool_choice={"type": "function", "function": {"name": tool_name}},
        )
        choice = response.choices[0]
        usage = response.usage
        structured = None
        if choice.message.tool_calls:
            structured = json.loads(choice.message.tool_calls[0].function.arguments)
        return LLMResponse(
            content=json.dumps(structured) if structured else "",
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
            model=model,
            structured_output=structured,
        )


def create_provider(provider_name: str, api_key: str) -> LLMProvider:
    """Create an LLM provider by name."""
    if provider_name in ("anthropic", "claude"):
        return AnthropicProvider(api_key)
    elif provider_name == "openai":
        return OpenAIProvider(api_key)
    else:
        raise ValueError(f"Unknown LLM provider: {provider_name}")


# Pricing per 1M tokens (USD) — used for cost estimation
PRICING = {
    # Anthropic
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "claude-sonnet-4-5-20250929": {"input": 3.0, "output": 15.0},
    "claude-opus-4-6": {"input": 15.0, "output": 75.0},
    "claude-haiku-3-5-20241022": {"input": 0.80, "output": 4.0},
    # OpenAI
    "gpt-4o": {"input": 2.5, "output": 10.0},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4.1": {"input": 2.0, "output": 8.0},
    "gpt-4.1-mini": {"input": 0.40, "output": 1.60},
    "gpt-4.1-nano": {"input": 0.10, "output": 0.40},
    "gpt-5": {"input": 1.25, "output": 10.0},
}


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate cost in USD for a given model and token counts."""
    prices = PRICING.get(model, {"input": 5.0, "output": 15.0})
    return (
        input_tokens * prices["input"] / 1_000_000
        + output_tokens * prices["output"] / 1_000_000
    )
