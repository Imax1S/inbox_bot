"""LLM provider abstraction with token tracking for Anthropic and OpenAI."""

import asyncio
import json
import logging
import random
from dataclasses import dataclass, field
from typing import Protocol

logger = logging.getLogger(__name__)


LLM_MAX_ATTEMPTS = 3
LLM_BACKOFF_BASE_SECONDS = 2.0


def _is_transient_anthropic_error(exc: BaseException) -> bool:
    try:
        import anthropic
    except ImportError:
        return False
    transient = (
        getattr(anthropic, "APIConnectionError", Exception),
        getattr(anthropic, "APITimeoutError", Exception),
        getattr(anthropic, "RateLimitError", Exception),
        getattr(anthropic, "InternalServerError", Exception),
    )
    if isinstance(exc, transient):
        return True
    api_status = getattr(anthropic, "APIStatusError", None)
    if api_status is not None and isinstance(exc, api_status):
        status = getattr(exc, "status_code", None)
        return status is None or status >= 500 or status == 429
    return False


def _is_transient_openai_error(exc: BaseException) -> bool:
    try:
        import openai
    except ImportError:
        return False
    transient_names = (
        "APIConnectionError",
        "APITimeoutError",
        "RateLimitError",
        "InternalServerError",
    )
    for name in transient_names:
        cls = getattr(openai, name, None)
        if cls is not None and isinstance(exc, cls):
            return True
    api_status = getattr(openai, "APIStatusError", None)
    if api_status is not None and isinstance(exc, api_status):
        status = getattr(exc, "status_code", None)
        return status is None or status >= 500 or status == 429
    return False


async def _retry_llm_call(op_name: str, func, is_transient):
    """Retry an async LLM call on transient errors with exponential backoff."""
    last_exc: BaseException | None = None
    for attempt in range(1, LLM_MAX_ATTEMPTS + 1):
        try:
            return await func()
        except Exception as exc:
            last_exc = exc
            if attempt >= LLM_MAX_ATTEMPTS or not is_transient(exc):
                raise
            delay = LLM_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            delay += random.uniform(0, delay * 0.25)
            logger.warning(
                "%s transient error on attempt %d/%d: %s — retrying in %.1fs",
                op_name,
                attempt,
                LLM_MAX_ATTEMPTS,
                exc,
                delay,
            )
            await asyncio.sleep(delay)
    assert last_exc is not None
    raise last_exc


def _extract_text_from_content(blocks) -> str:
    """Extract concatenated text from Anthropic response content blocks.

    Only picks blocks of type 'text' with a non-None .text attribute.
    Raises RuntimeError if nothing textual is present.
    """
    parts: list[str] = []
    for block in blocks or []:
        if getattr(block, "type", None) != "text":
            continue
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    if not parts:
        raise RuntimeError("empty LLM response (no text blocks)")
    return "".join(parts) if len(parts) == 1 else "\n".join(parts)


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

    async def generate_with_search(
        self,
        model: str,
        system_prompt: str,
        user_message: str,
        max_tokens: int = 1024,
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
        async def _call() -> LLMResponse:
            async with self.client.messages.stream(
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=system_prompt,
                messages=[{"role": "user", "content": user_message}],
            ) as stream:
                response = await stream.get_final_message()
            return LLMResponse(
                content=_extract_text_from_content(response.content),
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                model=model,
            )

        return await _retry_llm_call(
            f"anthropic.generate({model})", _call, _is_transient_anthropic_error
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
        async def _call() -> LLMResponse:
            async with self.client.messages.stream(
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
            ) as stream:
                response = await stream.get_final_message()
            structured = None
            for block in response.content or []:
                if getattr(block, "type", None) == "tool_use":
                    structured = block.input
                    break
            return LLMResponse(
                content=json.dumps(structured) if structured else "",
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                model=model,
                structured_output=structured,
            )

        return await _retry_llm_call(
            f"anthropic.generate_structured({model})",
            _call,
            _is_transient_anthropic_error,
        )

    async def generate_with_search(
        self,
        model: str,
        system_prompt: str,
        user_message: str,
        max_tokens: int = 1024,
        temperature: float = 0.7,
    ) -> LLMResponse:
        async def _call() -> LLMResponse:
            async with self.client.messages.stream(
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=system_prompt,
                tools=[{"type": "web_search_20260209", "name": "web_search"}],
                messages=[{"role": "user", "content": user_message}],
            ) as stream:
                response = await stream.get_final_message()
            # Text blocks may be interleaved with tool_use/tool_result; keep only text.
            parts = [
                block.text
                for block in (response.content or [])
                if getattr(block, "type", None) == "text"
                and getattr(block, "text", None)
            ]
            return LLMResponse(
                content="\n".join(parts),
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                model=model,
            )

        return await _retry_llm_call(
            f"anthropic.generate_with_search({model})",
            _call,
            _is_transient_anthropic_error,
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
        async def _call() -> LLMResponse:
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

        return await _retry_llm_call(
            f"openai.generate({model})", _call, _is_transient_openai_error
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
        async def _call() -> LLMResponse:
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

        return await _retry_llm_call(
            f"openai.generate_structured({model})",
            _call,
            _is_transient_openai_error,
        )

    async def generate_with_search(
        self,
        model: str,
        system_prompt: str,
        user_message: str,
        max_tokens: int = 1024,
        temperature: float = 0.7,
    ) -> LLMResponse:
        logger.warning(
            "OpenAI provider does not support web_search tool — falling back to generate()"
        )
        return await self.generate(model, system_prompt, user_message, max_tokens, temperature)


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
