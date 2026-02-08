"""LLM Agent for processing messages."""

from datetime import datetime
from pathlib import Path

from .config import LLMConfig, UserProfile
from .llm_provider import LLMProvider, create_llm_provider
from .telegram_client import Message


class DigestAgent:
    """Agent for generating weekly digests using LLM."""

    def __init__(self, config: LLMConfig, user_profile: UserProfile):
        """Initialize the digest agent.

        Args:
            config: LLM configuration.
            user_profile: User profile data.
        """
        self.config = config
        self.user_profile = user_profile
        self.llm: LLMProvider = create_llm_provider(
            provider_name=config.provider,
            api_key=config.api_key,
            model=config.model,
        )
        self.system_prompt = self._load_system_prompt()

    def _load_system_prompt(self) -> str:
        """Load and format the system prompt."""
        prompt_path = Path(__file__).parent.parent / "prompts" / "system_prompt.txt"

        with open(prompt_path, "r", encoding="utf-8") as f:
            template = f.read()

        # Generate category suggestions based on interests
        category_suggestions = "\n".join(
            f"- {interest}" for interest in self.user_profile.interests
        )

        # Format the prompt with user profile data
        return template.format(
            user_name=self.user_profile.name,
            user_interests=", ".join(self.user_profile.interests),
            user_goals=", ".join(self.user_profile.goals),
            preferred_language=self.user_profile.preferred_language,
            note_style=self.user_profile.note_style,
            category_suggestions=category_suggestions,
        )

    def _format_messages_for_prompt(self, messages: list[Message]) -> str:
        """Format messages into a string for the LLM.

        Args:
            messages: List of Telegram messages.

        Returns:
            Formatted string of messages.
        """
        formatted = []
        for msg in messages:
            date_str = msg.date.strftime("%Y-%m-%d %H:%M")
            formatted.append(f"[{date_str}] {msg.text}")

        return "\n\n---\n\n".join(formatted)

    def _get_week_date_range(self, messages: list[Message]) -> str:
        """Get the date range string for the week.

        Args:
            messages: List of messages to determine date range.

        Returns:
            Formatted date range string.
        """
        if not messages:
            now = datetime.now()
            return now.strftime("%Y-W%W")

        dates = [msg.date for msg in messages]
        min_date = min(dates)
        max_date = max(dates)

        return f"{min_date.strftime('%d.%m')} - {max_date.strftime('%d.%m.%Y')}"

    async def generate_digest(self, messages: list[Message]) -> str:
        """Generate a weekly digest from messages.

        Args:
            messages: List of Telegram messages to process.

        Returns:
            Generated digest in Markdown format.
        """
        if not messages:
            return (
                "# thought mapper — Weekly Digest\n\n"
                "На этой неделе нет сохранённых сообщений для дайджеста."
            )

        # Format messages for the prompt
        messages_text = self._format_messages_for_prompt(messages)
        week_range = self._get_week_date_range(messages)

        # Create the user message
        user_message = f"""Собери дайджест «thought mapper — Weekly Digest» по {len(messages)} сохранённым сообщениям.

Неделя: {week_range}

## Сообщения:

{messages_text}

---

Требования:
- Используй все материалы недели, ничего не выбрасывай; объединяй похожие сообщения в один смысловой блок.
- Решай, что раскрывать подробнее, опираясь на профиль пользователя из системного промпта (интересы, цели, стиль). Более релевантные темы делай развернутыми, второстепенные — краткими заметками.
- Каждый смысловой блок (даже из коротких сообщений) разворачивай минимум в 2–3 абзаца: 1) контекст/тезис, 2) разбор с деталями/примерами/рисками, 3) вывод/применение/следующие шаги. Не переписывай дословно, добавляй анализ и связи.
- Добавляй навигацию и ссылки-наверх через Markdown anchors.
- Для коротких knowledge queries (1–5 слов) давай объяснение на 2–4 предложения: причина → механизм → следствие/применение.
- Добавляй оценку времени чтения для каждого блока; изображения опциональны через Markdown.
- Язык — русский, кроме оригинальных цитат/терминов."""

        # Call the LLM
        response = await self.llm.generate(
            system_prompt=self.system_prompt,
            user_message=user_message,
        )

        return response
