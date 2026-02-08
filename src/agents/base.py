"""Base agent infrastructure — prompt loading, LLM calling, step logging."""

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from ..db.database import Database
from ..db.models import StepLog
from ..llm.provider import LLMProvider, LLMResponse

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent.parent.parent / "prompts"


class BaseAgent:
    """Base class for all pipeline agents."""

    prompt_file: str = ""  # Override in subclass
    agent_name: str = ""  # Override in subclass

    def __init__(self, llm: LLMProvider, model: str, db: Database):
        self.llm = llm
        self.model = model
        self.db = db
        self._prompt_template = self._load_prompt()

    def _load_prompt(self) -> str:
        if not self.prompt_file:
            return ""
        path = PROMPTS_DIR / self.prompt_file
        if not path.exists():
            logger.warning("Prompt file not found: %s", path)
            return ""
        return path.read_text(encoding="utf-8")

    def _format_prompt(self, **kwargs) -> str:
        """Format the prompt template with provided variables."""
        prompt = self._prompt_template
        for key, value in kwargs.items():
            prompt = prompt.replace(f"{{{key}}}", str(value))
        return prompt

    async def _call_llm(
        self,
        user_message: str,
        run_id: str | None = None,
        system_prompt: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        """Call the LLM and optionally log the step."""
        if system_prompt is None:
            system_prompt = self._prompt_template

        step_id = str(uuid4())
        started_at = datetime.now()

        try:
            response = await self.llm.generate(
                model=self.model,
                system_prompt=system_prompt,
                user_message=user_message,
                max_tokens=max_tokens,
                temperature=temperature,
            )

            if run_id:
                await self._log_step(
                    step_id=step_id,
                    run_id=run_id,
                    started_at=started_at,
                    response=response,
                    status="completed",
                )

            return response

        except Exception as e:
            if run_id:
                await self._log_step(
                    step_id=step_id,
                    run_id=run_id,
                    started_at=started_at,
                    response=None,
                    status="failed",
                    error=str(e),
                )
            raise

    async def _log_step(
        self,
        step_id: str,
        run_id: str,
        started_at: datetime,
        response: LLMResponse | None,
        status: str,
        error: str | None = None,
        details: str = "",
    ) -> None:
        step = StepLog(
            id=step_id,
            run_id=run_id,
            agent=self.agent_name,
            started_at=started_at,
            finished_at=datetime.now(),
            status=status,
            input_tokens=response.input_tokens if response else 0,
            output_tokens=response.output_tokens if response else 0,
            llm_model=response.model if response else self.model,
            details=details,
            error=error,
        )
        try:
            await self.db.save_step_log(step)
        except Exception as e:
            logger.error("Failed to save step log: %s", e)

    @staticmethod
    def _extract_json(text: str) -> dict:
        """Extract JSON from LLM response that may contain markdown fences."""
        # Try to find JSON in code blocks first
        match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
        if match:
            return json.loads(match.group(1).strip())
        # Try parsing the whole text
        return json.loads(text.strip())
