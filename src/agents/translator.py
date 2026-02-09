"""Translator agent — translates the final magazine into the user's chosen language."""

import json
import logging

from ..db.database import Database
from ..llm.provider import LLMProvider
from .base import BaseAgent

logger = logging.getLogger(__name__)


class TranslatorAgent(BaseAgent):
    prompt_file = "translator.txt"
    agent_name = "translator"

    def __init__(
        self,
        llm: LLMProvider,
        model: str,
        db: Database,
        user_profile: dict,
    ):
        super().__init__(llm, model, db)
        self.user_profile = user_profile
        self._prompt_template = self._format_prompt(
            user_profile_json=json.dumps(user_profile, ensure_ascii=False, indent=2)
        )

    async def process(
        self,
        magazine: str,
        target_language: str,
        run_id: str | None = None,
    ) -> str:
        """Translate the magazine into the target language."""
        user_message = (
            f"## Target language: {target_language}\n\n"
            f"## Magazine to translate:\n\n{magazine}"
        )

        response = await self._call_llm(
            user_message=user_message,
            run_id=run_id,
            max_tokens=16384,
            temperature=0.3,
        )

        return response.content
