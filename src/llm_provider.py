"""LLM provider abstraction for different AI models."""

from abc import ABC, abstractmethod
from typing import Protocol


class LLMProvider(Protocol):
    """Protocol for LLM providers."""

    async def generate(self, system_prompt: str, user_message: str) -> str:
        """Generate response from the LLM.

        Args:
            system_prompt: System prompt to set context.
            user_message: User message to respond to.

        Returns:
            Generated response text.
        """
        ...


class ClaudeProvider:
    """Claude (Anthropic) LLM provider."""

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-20250514"):
        """Initialize Claude provider.

        Args:
            api_key: Anthropic API key.
            model: Model identifier.
        """
        from langchain_anthropic import ChatAnthropic
        from langchain_core.messages import HumanMessage, SystemMessage

        self.model = model
        self.llm = ChatAnthropic(
            model=model,
            api_key=api_key,
            max_tokens=4096,
        )
        self._SystemMessage = SystemMessage
        self._HumanMessage = HumanMessage

    async def generate(self, system_prompt: str, user_message: str) -> str:
        """Generate response using Claude."""
        response = await self.llm.ainvoke([
            self._SystemMessage(content=system_prompt),
            self._HumanMessage(content=user_message),
        ])
        return response.content


class OpenAIProvider:
    """OpenAI GPT LLM provider."""

    def __init__(self, api_key: str, model: str = "gpt-4o"):
        """Initialize OpenAI provider.

        Args:
            api_key: OpenAI API key.
            model: Model identifier (gpt-4o, gpt-4-turbo, gpt-3.5-turbo, etc).
        """
        from langchain_openai import ChatOpenAI
        from langchain_core.messages import HumanMessage, SystemMessage

        self.model = model
        self.llm = ChatOpenAI(
            model=model,
            api_key=api_key,
            max_tokens=4096,
        )
        self._SystemMessage = SystemMessage
        self._HumanMessage = HumanMessage

    async def generate(self, system_prompt: str, user_message: str) -> str:
        """Generate response using OpenAI."""
        response = await self.llm.ainvoke([
            self._SystemMessage(content=system_prompt),
            self._HumanMessage(content=user_message),
        ])
        return response.content


def create_llm_provider(
    provider_name: str,
    api_key: str,
    model: str | None = None,
) -> LLMProvider:
    """Factory function to create LLM provider.

    Args:
        provider_name: Provider name ('claude' or 'openai').
        api_key: API key for the provider.
        model: Optional model override.

    Returns:
        LLM provider instance.

    Raises:
        ValueError: If provider name is not supported.
    """
    provider_name = provider_name.lower()

    if provider_name == "claude":
        default_model = "claude-sonnet-4-20250514"
        return ClaudeProvider(api_key, model or default_model)
    elif provider_name == "openai":
        default_model = "gpt-4o"
        return OpenAIProvider(api_key, model or default_model)
    else:
        raise ValueError(
            f"Unsupported provider: {provider_name}. "
            f"Supported: 'claude', 'openai'"
        )
