"""Telegram bot with all commands — message collection via Collector agent + DB."""

import asyncio
import io
import json
import logging
from datetime import datetime
from uuid import uuid4

from telegram import BotCommand, Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ..agents.collector import CollectorAgent
from ..agents.profiler import ProfilerAgent
from ..config import Config
from ..content.text_classifier import classify_message
from ..content.url_parser import fetch_and_extract
from ..db.database import Database
from ..db.models import Item, ItemStatus, ItemType
from ..llm.provider import estimate_cost
from ..pipeline.orchestrator import Orchestrator
from ..pipeline.status_updater import StatusUpdater

logger = logging.getLogger(__name__)

# ConversationHandler states for /setup
SETUP_AWAITING_TEXT, SETUP_REVIEWING_AREAS, SETUP_STRICTNESS = range(3)

# Priority cycle for area weight adjustment via inline buttons
PRIORITY_CYCLE = [
    ("high", 0.90),
    ("medium", 0.70),
    ("low", 0.50),
    ("off", 0.0),
]
PRIORITY_LABELS = {
    "high": "🔴 High",
    "medium": "🟡 Medium",
    "low": "🟢 Low",
    "off": "⬜ Off",
}


def _priority_for_weight(weight: float) -> str:
    """Map a numeric weight to the closest priority label."""
    if weight >= 0.80:
        return "high"
    elif weight >= 0.60:
        return "medium"
    elif weight > 0:
        return "low"
    return "off"


def _next_priority(current: str) -> tuple[str, float]:
    """Cycle to the next priority level."""
    keys = [p[0] for p in PRIORITY_CYCLE]
    idx = keys.index(current) if current in keys else 0
    next_idx = (idx + 1) % len(keys)
    return PRIORITY_CYCLE[next_idx]


class DigestBot:
    def __init__(
        self,
        config: Config,
        db: Database,
        collector: CollectorAgent,
        orchestrator: Orchestrator,
        profiler: ProfilerAgent | None = None,
    ):
        self.config = config
        self.db = db
        self.collector = collector
        self.orchestrator = orchestrator
        self.profiler = profiler
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
            "/setup — Configure your interest profile\n"
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

    # ── Profile Setup (/setup) ──

    async def _handle_setup(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Entry point for /setup — ask user to describe themselves."""
        if not update.message or not update.effective_user:
            return ConversationHandler.END
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("Access denied.")
            return ConversationHandler.END

        if not self.profiler:
            await update.message.reply_text(
                "Profile setup is not available (profiler agent not configured)."
            )
            return ConversationHandler.END

        await update.message.reply_text(
            "🔧 *Profile Setup*\n\n"
            "Tell me about yourself in free form:\n"
            "— What do you do? What's your field?\n"
            "— What topics do you follow?\n"
            "— What are your goals for content consumption?\n"
            "— Anything you explicitly DON'T want?\n\n"
            "Just write naturally, I'll extract your interests.\n\n"
            "Send /cancel to abort.",
            parse_mode="Markdown",
        )
        return SETUP_AWAITING_TEXT

    async def _handle_setup_text(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Process the user's free-form description with ProfilerAgent."""
        if not update.message or not update.effective_user:
            return ConversationHandler.END

        text = update.message.text or ""
        if not text.strip():
            await update.message.reply_text("Please send a text description.")
            return SETUP_AWAITING_TEXT

        await update.message.reply_text("🔍 Analyzing your interests...")

        try:
            extracted = await self.profiler.extract_interests(text)
        except Exception as e:
            logger.error("Profiler extraction failed: %s", e)
            await update.message.reply_text(
                f"❌ Failed to analyze your description: {e}\n"
                "Please try again or /cancel."
            )
            return SETUP_AWAITING_TEXT

        areas = extracted.get("interest_areas", [])
        if not areas:
            await update.message.reply_text(
                "Couldn't extract any interest areas. "
                "Please describe your interests in more detail, or /cancel."
            )
            return SETUP_AWAITING_TEXT

        # Store extraction data and original text in user_data for later steps
        context.user_data["setup_extracted"] = extracted
        context.user_data["setup_user_text"] = text
        # Initialize area priorities from extracted weights
        area_priorities = []
        for area in areas:
            priority = _priority_for_weight(area.get("weight", 0.7))
            area_priorities.append({
                **area,
                "priority": priority,
                "weight": area.get("weight", 0.7),
            })
        context.user_data["setup_areas"] = area_priorities

        # Show areas for review
        await self._send_areas_review(update, context)
        return SETUP_REVIEWING_AREAS

    async def _send_areas_review(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Send the areas review message with inline buttons."""
        areas = context.user_data.get("setup_areas", [])

        lines = ["📋 *Extracted interest areas:*\n"]
        buttons = []
        for i, area in enumerate(areas):
            name = area.get("name", area.get("id", f"Area {i+1}"))
            priority = area.get("priority", "medium")
            label = PRIORITY_LABELS.get(priority, priority)
            lines.append(f"{i+1}. {name} — {label}")
            buttons.append([
                InlineKeyboardButton(
                    f"{name}: {label}",
                    callback_data=f"setup_area:{i}",
                )
            ])

        lines.append("\nTap an area to change its priority.")
        lines.append("When done, tap *Confirm*.")

        buttons.append([
            InlineKeyboardButton("✅ Confirm", callback_data="setup_confirm_areas"),
        ])

        keyboard = InlineKeyboardMarkup(buttons)

        # Send or edit the message
        msg = update.callback_query.message if update.callback_query else update.message
        if update.callback_query:
            try:
                await update.callback_query.edit_message_text(
                    "\n".join(lines),
                    reply_markup=keyboard,
                    parse_mode="Markdown",
                )
            except Exception:
                await msg.reply_text(
                    "\n".join(lines),
                    reply_markup=keyboard,
                    parse_mode="Markdown",
                )
        else:
            await msg.reply_text(
                "\n".join(lines),
                reply_markup=keyboard,
                parse_mode="Markdown",
            )

    async def _handle_setup_area_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Handle area priority toggle or confirm button."""
        query = update.callback_query
        if not query or not query.data:
            return SETUP_REVIEWING_AREAS

        await query.answer()

        if query.data == "setup_confirm_areas":
            # Move to strictness selection
            return await self._ask_strictness(update, context)

        # Toggle area priority
        if query.data.startswith("setup_area:"):
            idx_str = query.data.split(":", 1)[1]
            try:
                idx = int(idx_str)
            except ValueError:
                return SETUP_REVIEWING_AREAS

            areas = context.user_data.get("setup_areas", [])
            if 0 <= idx < len(areas):
                current_priority = areas[idx].get("priority", "medium")
                new_priority, new_weight = _next_priority(current_priority)
                areas[idx]["priority"] = new_priority
                areas[idx]["weight"] = new_weight

            # Re-render the review message
            await self._send_areas_review(update, context)

        return SETUP_REVIEWING_AREAS

    async def _ask_strictness(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Ask user to choose filtering strictness."""
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🔒 Strict",
                    callback_data="setup_strict:strict",
                ),
            ],
            [
                InlineKeyboardButton(
                    "⚖️ Moderate (recommended)",
                    callback_data="setup_strict:moderate",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🔓 Relaxed",
                    callback_data="setup_strict:relaxed",
                ),
            ],
        ])

        query = update.callback_query
        if query:
            await query.edit_message_text(
                "🎚 *Filtering strictness*\n\n"
                "How aggressively should I filter irrelevant content?\n\n"
                "🔒 *Strict* — only content closely matching your interests\n"
                "⚖️ *Moderate* — balanced filtering, slight tangents OK\n"
                "🔓 *Relaxed* — keep most content, only remove obvious noise",
                reply_markup=keyboard,
                parse_mode="Markdown",
            )
        return SETUP_STRICTNESS

    async def _handle_setup_strictness(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Handle strictness selection and build the final profile."""
        query = update.callback_query
        if not query or not query.data:
            return SETUP_STRICTNESS

        strictness = query.data.split(":", 1)[1]
        await query.answer(f"Strictness: {strictness}")

        # Build the profile
        extracted = context.user_data.get("setup_extracted", {})
        areas = context.user_data.get("setup_areas", [])

        # Filter out "off" areas and prepare confirmed list
        confirmed_areas = [a for a in areas if a.get("weight", 0) > 0]

        profile = ProfilerAgent.build_profile(
            extracted=extracted,
            confirmed_areas=confirmed_areas,
            strictness=strictness,
        )

        # Save to database
        await self.db.set_setting("user_profile", json.dumps(profile, ensure_ascii=False))
        await self.db.set_setting("filtering_strictness", strictness)

        # Update the filter agent's profile if orchestrator has one
        if hasattr(self.orchestrator, 'filter_agent') and self.orchestrator.filter_agent:
            self.orchestrator.filter_agent.update_profile(profile)

        # Summary for user
        area_count = len(confirmed_areas)
        area_names = [a.get("name", a.get("id", "?")) for a in confirmed_areas]
        strictness_labels = {
            "strict": "🔒 Strict",
            "moderate": "⚖️ Moderate",
            "relaxed": "🔓 Relaxed",
        }

        await query.edit_message_text(
            f"✅ *Profile saved!*\n\n"
            f"Interest areas ({area_count}):\n"
            + "\n".join(f"  • {name}" for name in area_names)
            + f"\n\nFiltering: {strictness_labels.get(strictness, strictness)}\n\n"
            "Your digest will now be filtered based on this profile. "
            "Run /setup again anytime to reconfigure.",
            parse_mode="Markdown",
        )

        # Cleanup user_data
        context.user_data.pop("setup_extracted", None)
        context.user_data.pop("setup_user_text", None)
        context.user_data.pop("setup_areas", None)

        return ConversationHandler.END

    async def _handle_setup_cancel(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Cancel the setup conversation."""
        if update.message:
            await update.message.reply_text("Setup cancelled.")
        # Cleanup
        context.user_data.pop("setup_extracted", None)
        context.user_data.pop("setup_user_text", None)
        context.user_data.pop("setup_areas", None)
        return ConversationHandler.END

    # ── Bot Setup ──

    @staticmethod
    async def _post_init(application: Application) -> None:
        """Set bot commands so they appear in Telegram's command menu."""
        await application.bot.set_my_commands([
            BotCommand("start", "Show welcome message & help"),
            BotCommand("generate", "Generate weekly digest now"),
            BotCommand("items", "List this week's collected items"),
            BotCommand("delete", "Remove an item by ID"),
            BotCommand("setup", "Configure your interest profile"),
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

        # Setup conversation handler (must be before the catch-all message handler)
        setup_conv = ConversationHandler(
            entry_points=[CommandHandler("setup", self._handle_setup)],
            states={
                SETUP_AWAITING_TEXT: [
                    MessageHandler(
                        filters.TEXT & ~filters.COMMAND,
                        self._handle_setup_text,
                    ),
                ],
                SETUP_REVIEWING_AREAS: [
                    CallbackQueryHandler(
                        self._handle_setup_area_callback,
                        pattern=r"^setup_(area|confirm)",
                    ),
                ],
                SETUP_STRICTNESS: [
                    CallbackQueryHandler(
                        self._handle_setup_strictness,
                        pattern=r"^setup_strict:",
                    ),
                ],
            },
            fallbacks=[CommandHandler("cancel", self._handle_setup_cancel)],
        )
        self.app.add_handler(setup_conv)

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
