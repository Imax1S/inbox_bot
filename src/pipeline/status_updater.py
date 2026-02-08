"""Telegram status message updater — edits a single message with pipeline progress."""

import asyncio
import logging
import time

from telegram import Bot

logger = logging.getLogger(__name__)

STEP_NAMES = ["Clustering", "Researching", "Writing", "Assembling"]
STEP_ICONS = {"done": "✅", "active": "🔄", "pending": "⬜"}

# Minimum interval between message edits (Telegram rate limit protection)
MIN_EDIT_INTERVAL = 2.0


class StatusUpdater:
    def __init__(self, bot: Bot, chat_id: int):
        self.bot = bot
        self.chat_id = chat_id
        self.message_id: int | None = None
        self.week_id: str = ""
        self.item_count: int = 0
        self.current_step: int = -1
        self.detail: str = ""
        self._last_edit_time: float = 0

    async def start(self, week_id: str, item_count: int) -> None:
        """Send the initial status message."""
        self.week_id = week_id
        self.item_count = item_count
        self.current_step = -1
        self.detail = ""

        text = self._render()
        try:
            msg = await self.bot.send_message(
                chat_id=self.chat_id,
                text=text,
            )
            self.message_id = msg.message_id
        except Exception as e:
            logger.error("Failed to send status message: %s", e)

    async def update(self, step: int, detail: str = "") -> None:
        """Update the status message with current step progress."""
        self.current_step = step
        self.detail = detail
        await self._edit_message()

    async def finish(self, result_path: str | None = None) -> None:
        """Mark the pipeline as complete."""
        self.current_step = len(STEP_NAMES)
        self.detail = (
            f"Saved to: {result_path}" if result_path else "Complete"
        )
        await self._edit_message()

    async def fail(self, error: str) -> None:
        """Mark the pipeline as failed."""
        self.detail = f"❌ Error: {error}"
        await self._edit_message()

    def _render(self) -> str:
        lines = [
            f"📰 Generating Weekly Digest ({self.week_id})",
            f"{self.item_count} items to process",
            "",
        ]

        # Progress bar
        total = len(STEP_NAMES)
        filled = min(self.current_step + 1, total) if self.current_step >= 0 else 0
        bar = "▰" * filled + "▱" * (total - filled)
        if 0 <= self.current_step < total:
            lines.append(f"{bar} Step {self.current_step + 1}/{total}: {STEP_NAMES[self.current_step]}")
        elif self.current_step >= total:
            lines.append(f"{bar} Complete!")
        else:
            lines.append(f"{bar} Starting...")

        lines.append("")

        # Step status list
        for i, name in enumerate(STEP_NAMES):
            if i < self.current_step:
                icon = STEP_ICONS["done"]
            elif i == self.current_step:
                icon = STEP_ICONS["active"]
            else:
                icon = STEP_ICONS["pending"]
            lines.append(f"{icon} {name}")

        # Detail line
        if self.detail:
            lines.append(f"\n{self.detail}")

        return "\n".join(lines)

    async def _edit_message(self) -> None:
        """Edit the status message with rate limit protection."""
        if self.message_id is None:
            return

        # Rate limit: don't edit more often than MIN_EDIT_INTERVAL
        now = time.monotonic()
        elapsed = now - self._last_edit_time
        if elapsed < MIN_EDIT_INTERVAL:
            await asyncio.sleep(MIN_EDIT_INTERVAL - elapsed)

        text = self._render()
        try:
            await self.bot.edit_message_text(
                chat_id=self.chat_id,
                message_id=self.message_id,
                text=text,
            )
            self._last_edit_time = time.monotonic()
        except Exception as e:
            logger.warning("Failed to edit status message: %s — sending new", e)
            try:
                msg = await self.bot.send_message(
                    chat_id=self.chat_id,
                    text=text,
                )
                self.message_id = msg.message_id
                self._last_edit_time = time.monotonic()
            except Exception as e2:
                logger.error("Failed to send fallback status message: %s", e2)
