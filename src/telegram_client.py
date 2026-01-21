"""Telegram Bot client for collecting and fetching messages."""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from telegram import Bot, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import TelegramConfig


@dataclass
class Message:
    """Represents a collected message."""
    id: int
    text: str
    date: datetime


class TelegramBotClient:
    """Bot client for collecting messages from user."""

    def __init__(self, config: TelegramConfig, on_digest_request=None):
        """Initialize Telegram bot client.

        Args:
            config: Telegram bot configuration.
            on_digest_request: Callback function to generate digest.
        """
        self.config = config
        self.on_digest_request = on_digest_request
        self.messages: list[Message] = []
        self.app: Optional[Application] = None

    def _is_authorized(self, user_id: int) -> bool:
        """Check if user is authorized to use the bot."""
        return user_id == self.config.user_id

    async def _handle_message(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Handle incoming text messages - collect them."""
        if not update.message or not update.effective_user:
            return

        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("Access denied.")
            return

        # Collect the message
        msg = Message(
            id=update.message.message_id,
            text=update.message.text or "",
            date=update.message.date.replace(tzinfo=None),
        )
        self.messages.append(msg)

        await update.message.reply_text(
            f"Saved! ({len(self.messages)} messages collected)"
        )

    async def _handle_digest(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Handle /digest command - generate weekly digest."""
        if not update.message or not update.effective_user:
            return

        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("Access denied.")
            return

        if not self.messages:
            await update.message.reply_text("No messages collected yet.")
            return

        await update.message.reply_text(
            f"Generating digest from {len(self.messages)} messages..."
        )

        if self.on_digest_request:
            try:
                result = await self.on_digest_request(self.messages)
                await update.message.reply_text(f"Digest saved to: {result}")
                # Clear messages after successful digest
                self.messages.clear()
            except Exception as e:
                await update.message.reply_text(f"Error: {e}")

    async def _handle_clear(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Handle /clear command - clear collected messages."""
        if not update.message or not update.effective_user:
            return

        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("Access denied.")
            return

        count = len(self.messages)
        self.messages.clear()
        await update.message.reply_text(f"Cleared {count} messages.")

    async def _handle_status(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Handle /status command - show current status."""
        if not update.message or not update.effective_user:
            return

        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("Access denied.")
            return

        await update.message.reply_text(
            f"Messages collected: {len(self.messages)}\n"
            f"Use /digest to generate weekly digest.\n"
            f"Use /clear to clear all messages."
        )

    async def _handle_start(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Handle /start command."""
        if not update.message or not update.effective_user:
            return

        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text(
                f"Access denied. Your ID: {update.effective_user.id}"
            )
            return

        await update.message.reply_text(
            "Inbox Agent Bot\n\n"
            "Send me messages throughout the week, then use /digest to generate a summary.\n\n"
            "Commands:\n"
            "/status - Show collected messages count\n"
            "/digest - Generate weekly digest\n"
            "/clear - Clear all collected messages"
        )

    def run(self) -> None:
        """Run the bot (blocking)."""
        self.app = Application.builder().token(self.config.bot_token).build()

        # Add handlers
        self.app.add_handler(CommandHandler("start", self._handle_start))
        self.app.add_handler(CommandHandler("status", self._handle_status))
        self.app.add_handler(CommandHandler("digest", self._handle_digest))
        self.app.add_handler(CommandHandler("clear", self._handle_clear))
        self.app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message)
        )

        print("Bot started. Press Ctrl+C to stop.")
        self.app.run_polling()

    def get_messages(self) -> list[Message]:
        """Get all collected messages."""
        return self.messages.copy()
