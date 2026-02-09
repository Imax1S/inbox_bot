"""Telegram bot with all commands — message collection via Collector agent + DB."""

import asyncio
import io
import logging
from datetime import datetime
from uuid import uuid4

from telegram import BotCommand, Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ..agents.collector import CollectorAgent
from ..config import Config
from ..content.text_classifier import classify_message
from ..content.url_parser import fetch_and_extract
from ..db.database import Database
from ..db.models import Item, ItemStatus, ItemType
from ..llm.provider import estimate_cost
from ..pipeline.orchestrator import Orchestrator
from ..pipeline.status_updater import StatusUpdater

logger = logging.getLogger(__name__)


class DigestBot:
    def __init__(
        self,
        config: Config,
        db: Database,
        collector: CollectorAgent,
        orchestrator: Orchestrator,
    ):
        self.config = config
        self.db = db
        self.collector = collector
        self.orchestrator = orchestrator
        self.app: Application | None = None
        self._generating = False

    def _is_authorized(self, user_id: int) -> bool:
        return user_id == self.config.telegram.user_id

    # ── Message Handler ──

    async def _handle_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not update.message or not update.effective_user:
            return
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("Access denied.")
            return

        text = update.message.text or ""
        if not text.strip():
            return

        # Classify the message
        item_type, url = classify_message(text)

        # Fetch URL content if it's an article
        extracted_text = None
        fetch_error = None
        if item_type == ItemType.ARTICLE and url:
            await update.message.reply_text("🔗 Fetching article...")
            extracted_text, fetch_error = await fetch_and_extract(url)
            if fetch_error:
                logger.warning("URL fetch issue for %s: %s", url, fetch_error)

        # Run collector agent for summary + tags
        try:
            result = await self.collector.process(
                raw_content=text,
                extracted_text=extracted_text,
                item_type=item_type,
            )
        except Exception as e:
            logger.error("Collector agent failed: %s", e)
            result = None

        # Build and save item
        week_id = Database.current_week_id()
        item = Item(
            id=str(uuid4()),
            created_at=datetime.now(),
            type=item_type,
            raw_content=text,
            source_url=url,
            extracted_text=extracted_text,
            summary=result.summary if result else text[:200],
            tags=result.tags if result else [],
            language=result.language if result else "ru",
            week_id=week_id,
            status=ItemStatus.COLLECTED,
        )
        await self.db.save_item(item)

        # Send confirmation
        type_icon = {"ARTICLE": "📄", "TOPIC_SEED": "💡", "CONTEXT_NOTE": "📝"}
        icon = type_icon.get(item_type.value, "📌")
        tags_str = item.tags_str() if item.tags else "no tags"

        reply = f"{icon} Saved: \"{item.summary[:100]}\"\nTags: {tags_str}"
        if fetch_error:
            reply += f"\n⚠️ {fetch_error}"

        count = await self.db.count_items_by_week(week_id)
        reply += f"\n\n📊 {count} items this week"

        await update.message.reply_text(reply)

    # ── Commands ──

    async def _handle_start(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not update.message or not update.effective_user:
            return
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text(
                f"Access denied. Your ID: {update.effective_user.id}"
            )
            return

        await update.message.reply_text(
            "📖 Weekly Digest Bot\n\n"
            "Send me articles (URLs), questions, topics, and random thoughts "
            "throughout the week. On Sunday night (or when you use /generate), "
            "I'll process everything into a polished weekly magazine.\n\n"
            "Commands:\n"
            "/generate — Generate digest now\n"
            "/items — List this week's items\n"
            "/delete <id> — Remove an item\n"
            "/language — Choose digest language\n"
            "/status — Pipeline status\n"
            "/logs — Last run's log\n"
            "/cost — Token usage & cost\n"
            "/week — Current week info\n"
        )

    async def _handle_generate(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not update.message or not update.effective_user:
            return
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("Access denied.")
            return

        if self._generating:
            await update.message.reply_text("⏳ Generation already in progress.")
            return

        week_id = Database.current_week_id()
        items = await self.db.get_items_by_week(week_id, status=ItemStatus.COLLECTED)

        if not items:
            await update.message.reply_text(
                f"No items collected for {week_id}. Send me some content first!"
            )
            return

        self._generating = True
        status_updater = StatusUpdater(context.bot, update.effective_chat.id)

        try:
            result = await self.orchestrator.run(week_id, status_updater)
            if result:
                try:
                    with open(result, "rb") as f:
                        await context.bot.send_document(
                            chat_id=update.effective_chat.id,
                            document=f,
                            filename=f"digest-{week_id}.md",
                            caption=f"📖 Your weekly digest is ready!",
                        )
                except Exception as e:
                    logger.error("Failed to send document: %s", e)
                    await update.message.reply_text(
                        f"✅ Digest generated and saved to: {result}"
                    )
        except Exception as e:
            await update.message.reply_text(f"❌ Generation failed: {e}")
        finally:
            self._generating = False

    async def _handle_items(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not update.message or not update.effective_user:
            return
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("Access denied.")
            return

        week_id = Database.current_week_id()
        items = await self.db.get_items_by_week(week_id)

        if not items:
            await update.message.reply_text(f"No items for {week_id}.")
            return

        type_icon = {"ARTICLE": "📄", "TOPIC_SEED": "💡", "CONTEXT_NOTE": "📝"}
        lines = [f"📋 Items for {week_id} ({len(items)} total):\n"]
        for item in items:
            icon = type_icon.get(item.type.value, "📌")
            status_icon = "✅" if item.status == ItemStatus.PUBLISHED else "📥"
            lines.append(
                f"{status_icon} {icon} [{item.short_id()}] {item.summary[:60]}"
            )
            if item.tags:
                lines.append(f"   {item.tags_str()}")

        await update.message.reply_text("\n".join(lines))

    async def _handle_delete(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not update.message or not update.effective_user:
            return
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("Access denied.")
            return

        args = context.args
        if not args:
            await update.message.reply_text("Usage: /delete <item_id>")
            return

        short_id = args[0]
        item = await self.db.find_item_by_short_id(short_id)
        if not item:
            await update.message.reply_text(f"Item not found: {short_id}")
            return

        await self.db.delete_item(item.id)
        await update.message.reply_text(
            f"🗑 Deleted: [{item.short_id()}] {item.summary[:60]}"
        )

    async def _handle_status(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not update.message or not update.effective_user:
            return
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("Access denied.")
            return

        if self._generating:
            await update.message.reply_text("🔄 Generation in progress...")
            return

        last_run = await self.db.get_last_run()
        if not last_run:
            await update.message.reply_text("No pipeline runs yet.")
            return

        duration = ""
        if last_run.finished_at and last_run.started_at:
            dur = last_run.finished_at - last_run.started_at
            duration = f"\nDuration: {dur.total_seconds():.0f}s"

        status_icon = {
            "RUNNING": "🔄",
            "COMPLETED": "✅",
            "FAILED": "❌",
        }
        icon = status_icon.get(last_run.status.value, "❓")

        await update.message.reply_text(
            f"{icon} Last run: {last_run.week_id}\n"
            f"Status: {last_run.status.value}\n"
            f"Started: {last_run.started_at.strftime('%Y-%m-%d %H:%M')}"
            f"{duration}\n"
            f"Tokens: {last_run.total_input_tokens:,} in / "
            f"{last_run.total_output_tokens:,} out\n"
            f"Cost: ${last_run.estimated_cost_usd:.4f}"
        )

    async def _handle_logs(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not update.message or not update.effective_user:
            return
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("Access denied.")
            return

        last_run = await self.db.get_last_run()
        if not last_run or not last_run.steps:
            await update.message.reply_text("No logs available.")
            return

        lines = [
            f"Pipeline Run: {last_run.week_id}",
            f"Status: {last_run.status.value}",
            f"Started: {last_run.started_at.isoformat()}",
            f"Finished: {last_run.finished_at.isoformat() if last_run.finished_at else 'N/A'}",
            "",
            "Steps:",
            "-" * 40,
        ]

        for step in last_run.steps:
            duration = ""
            if step.finished_at and step.started_at:
                dur = (step.finished_at - step.started_at).total_seconds()
                duration = f" ({dur:.1f}s)"

            lines.append(
                f"[{step.status}] {step.agent} — {step.llm_model}{duration}"
            )
            lines.append(
                f"  Tokens: {step.input_tokens:,} in / {step.output_tokens:,} out"
            )
            if step.error:
                lines.append(f"  Error: {step.error}")
            if step.details:
                lines.append(f"  Details: {step.details}")
            lines.append("")

        log_text = "\n".join(lines)

        # Send as file if too long
        if len(log_text) > 3000:
            buf = io.BytesIO(log_text.encode("utf-8"))
            buf.name = f"logs-{last_run.week_id}.txt"
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=buf,
                caption=f"Logs for {last_run.week_id}",
            )
        else:
            await update.message.reply_text(f"```\n{log_text}\n```", parse_mode="Markdown")

    async def _handle_cost(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not update.message or not update.effective_user:
            return
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("Access denied.")
            return

        last_run = await self.db.get_last_run()
        if not last_run:
            await update.message.reply_text("No runs yet.")
            return

        lines = [f"💰 Cost Report — {last_run.week_id}\n"]
        total_cost = 0.0

        if last_run.steps:
            for step in last_run.steps:
                cost = estimate_cost(
                    step.llm_model, step.input_tokens, step.output_tokens
                )
                total_cost += cost
                lines.append(
                    f"  {step.agent} ({step.llm_model}): "
                    f"{step.input_tokens + step.output_tokens:,} tokens — "
                    f"${cost:.4f}"
                )

        lines.append(f"\nTotal tokens: {last_run.total_input_tokens + last_run.total_output_tokens:,}")
        lines.append(f"Total cost: ${total_cost:.4f}")

        await update.message.reply_text("\n".join(lines))

    async def _handle_week(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not update.message or not update.effective_user:
            return
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("Access denied.")
            return

        week_id = Database.current_week_id()
        count = await self.db.count_items_by_week(week_id)
        items = await self.db.get_items_by_week(week_id)

        type_counts = {}
        for item in items:
            type_counts[item.type.value] = type_counts.get(item.type.value, 0) + 1

        lines = [
            f"📅 Current week: {week_id}",
            f"📊 Items collected: {count}",
        ]
        if type_counts:
            lines.append("")
            if "ARTICLE" in type_counts:
                lines.append(f"  📄 Articles: {type_counts['ARTICLE']}")
            if "TOPIC_SEED" in type_counts:
                lines.append(f"  💡 Topics: {type_counts['TOPIC_SEED']}")
            if "CONTEXT_NOTE" in type_counts:
                lines.append(f"  📝 Notes: {type_counts['CONTEXT_NOTE']}")

        await update.message.reply_text("\n".join(lines))

    # ── Language Selection ──

    LANGUAGE_LABELS = {
        "ru": "🇷🇺 Русский",
        "en": "🇬🇧 English",
    }

    async def _handle_language(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not update.message or not update.effective_user:
            return
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("Access denied.")
            return

        current = await self.db.get_setting("digest_language", "ru")
        current_label = self.LANGUAGE_LABELS.get(current, current)

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🇷🇺 Русский", callback_data="lang:ru"),
                InlineKeyboardButton("🇬🇧 English", callback_data="lang:en"),
            ]
        ])

        await update.message.reply_text(
            f"🌐 Digest language: {current_label}\n\nChoose magazine language:",
            reply_markup=keyboard,
        )

    async def _handle_language_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        if not query or not update.effective_user:
            return
        if not self._is_authorized(update.effective_user.id):
            await query.answer("Access denied.")
            return

        if not query.data or not query.data.startswith("lang:"):
            return

        lang = query.data.split(":", 1)[1]
        if lang not in self.LANGUAGE_LABELS:
            await query.answer("Unknown language.")
            return

        await self.db.set_setting("digest_language", lang)
        label = self.LANGUAGE_LABELS[lang]
        await query.answer(f"Language set to {label}")
        await query.edit_message_text(f"✅ Digest language set to {label}")

    # ── Bot Setup ──

    @staticmethod
    async def _post_init(application: Application) -> None:
        """Set bot commands so they appear in Telegram's command menu."""
        await application.bot.set_my_commands([
            BotCommand("start", "Show welcome message & help"),
            BotCommand("generate", "Generate weekly digest now"),
            BotCommand("items", "List this week's collected items"),
            BotCommand("delete", "Remove an item by ID"),
            BotCommand("language", "Choose digest language (RU/EN)"),
            BotCommand("status", "Show last pipeline run status"),
            BotCommand("logs", "Show last pipeline run logs"),
            BotCommand("cost", "Show token usage & cost report"),
            BotCommand("week", "Current week info & stats"),
        ])

    def build(self) -> Application:
        """Build and return the Telegram Application."""
        self.app = (
            Application.builder()
            .token(self.config.telegram.bot_token)
            .post_init(self._post_init)
            .build()
        )

        self.app.add_handler(CommandHandler("start", self._handle_start))
        self.app.add_handler(CommandHandler("generate", self._handle_generate))
        self.app.add_handler(CommandHandler("items", self._handle_items))
        self.app.add_handler(CommandHandler("delete", self._handle_delete))
        self.app.add_handler(CommandHandler("status", self._handle_status))
        self.app.add_handler(CommandHandler("logs", self._handle_logs))
        self.app.add_handler(CommandHandler("cost", self._handle_cost))
        self.app.add_handler(CommandHandler("week", self._handle_week))
        self.app.add_handler(CommandHandler("language", self._handle_language))
        self.app.add_handler(CommandHandler("lang", self._handle_language))
        self.app.add_handler(
            CallbackQueryHandler(self._handle_language_callback, pattern=r"^lang:")
        )
        # Also keep /digest as an alias for /generate
        self.app.add_handler(CommandHandler("digest", self._handle_generate))
        self.app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message)
        )

        return self.app

    def run(self) -> None:
        """Build and run the bot (blocking)."""
        app = self.build()
        print("Bot started. Press Ctrl+C to stop.")
        app.run_polling()
