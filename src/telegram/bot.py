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

from ..agents.clusterer import ClustererAgent
from ..agents.collector import CollectorAgent
from ..agents.editor import EditorAgent
from ..agents.researcher import ResearcherAgent
from ..agents.translator import TranslatorAgent
from ..agents.writer import WriterAgent
from ..config import Config, get_provider_defaults
from ..content.text_classifier import classify_message
from ..content.url_parser import fetch_and_extract
from ..db.database import Database
from ..db.models import Item, ItemStatus, ItemType
from ..llm.provider import create_provider, estimate_cost
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
            "/provider — Switch LLM provider\n"
            "/estimate — Estimate generation cost\n"
            "/status — Pipeline status\n"
            "/logs — Last run's log\n"
            "/cost — Token usage & cost report\n"
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

        # Determine models used from step logs
        models_used = set()
        if last_run.steps:
            models_used = {s.llm_model for s in last_run.steps}

        lines = [
            f"{icon} Last run: {last_run.week_id}",
            f"Status: {last_run.status.value}",
            f"Started: {last_run.started_at.strftime('%Y-%m-%d %H:%M')}{duration}",
            f"Provider: {self.config.llm.provider}",
        ]
        if models_used:
            lines.append(f"Models: {', '.join(sorted(models_used))}")

        lines.append("")
        lines.append(
            f"Tokens: {last_run.total_input_tokens:,} in / "
            f"{last_run.total_output_tokens:,} out"
        )
        lines.append(f"Cost: ${last_run.estimated_cost_usd:.4f}")

        # Per-agent summary from step logs
        if last_run.steps:
            agent_stats: dict[str, dict] = {}
            for step in last_run.steps:
                name = step.agent
                if name not in agent_stats:
                    agent_stats[name] = {
                        "calls": 0, "input": 0, "output": 0, "cost": 0.0
                    }
                agent_stats[name]["calls"] += 1
                agent_stats[name]["input"] += step.input_tokens
                agent_stats[name]["output"] += step.output_tokens
                agent_stats[name]["cost"] += estimate_cost(
                    step.llm_model, step.input_tokens, step.output_tokens
                )

            lines.append("\nPer-agent:")
            for agent, stats in agent_stats.items():
                call_str = f" x{stats['calls']}" if stats['calls'] > 1 else ""
                lines.append(
                    f"  {agent}{call_str}: "
                    f"{stats['input']:,}+{stats['output']:,} tok "
                    f"${stats['cost']:.4f}"
                )

        await update.message.reply_text("\n".join(lines))

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
            # Group steps by agent
            agent_groups: dict[str, list] = {}
            for step in last_run.steps:
                if step.agent not in agent_groups:
                    agent_groups[step.agent] = []
                agent_groups[step.agent].append(step)

            for agent, steps in agent_groups.items():
                model = steps[0].llm_model
                total_in = sum(s.input_tokens for s in steps)
                total_out = sum(s.output_tokens for s in steps)
                cost = sum(
                    estimate_cost(s.llm_model, s.input_tokens, s.output_tokens)
                    for s in steps
                )
                total_cost += cost

                call_str = f" x{len(steps)}" if len(steps) > 1 else ""
                lines.append(f"  {agent}{call_str} ({model})")
                lines.append(
                    f"    {total_in:,} in + {total_out:,} out = "
                    f"{total_in + total_out:,} tok"
                )
                lines.append(f"    ${cost:.4f}")

        lines.append(
            f"\nTotal: {last_run.total_input_tokens:,} in / "
            f"{last_run.total_output_tokens:,} out"
        )
        lines.append(f"Total cost: ${total_cost:.4f}")

        # History of recent runs
        recent_runs = await self.db.get_recent_runs(limit=5)
        if len(recent_runs) > 1:
            lines.append("\n📜 Recent runs:")
            for run in recent_runs:
                status_ch = "✅" if run.status.value == "COMPLETED" else "❌"
                lines.append(
                    f"  {status_ch} {run.week_id} — "
                    f"${run.estimated_cost_usd:.4f} "
                    f"({run.total_input_tokens + run.total_output_tokens:,} tok)"
                )

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

    # ── Cost Estimation ──

    async def _handle_estimate(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not update.message or not update.effective_user:
            return
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("Access denied.")
            return

        week_id = Database.current_week_id()
        items = await self.db.get_items_by_week(week_id, status=ItemStatus.COLLECTED)

        if not items:
            await update.message.reply_text(
                f"No collected items for {week_id}. Nothing to estimate."
            )
            return

        n_items = len(items)
        n_clusters = min(6, max(1, n_items // 3))

        digest_language = await self.db.get_setting("digest_language", "en")
        needs_translation = digest_language != "en"

        # Static token estimates per agent call (input, output, num_calls)
        estimates = {
            "clusterer": (1500, 500, 1),
            "researcher": (2000, 800, n_clusters),
            "writer": (3000, 1500, n_clusters),
            "editor": (1500 * n_clusters + 1000, 3000, 1),
        }
        if needs_translation:
            estimates["translator"] = (4000, 4000, 1)

        models = {
            "clusterer": self.config.llm.clusterer_model,
            "researcher": self.config.llm.researcher_model,
            "writer": self.config.llm.writer_model,
            "editor": self.config.llm.editor_model,
            "translator": self.config.llm.translator_model,
        }

        total_cost = 0.0
        lines = [
            f"📊 Cost Estimate — {week_id}\n",
            f"Items: {n_items}",
            f"Est. clusters: ~{n_clusters}",
            f"Provider: {self.config.llm.provider}",
            f"Translation: {'yes' if needs_translation else 'no'}\n",
        ]

        for agent, (inp, out, calls) in estimates.items():
            total_inp = inp * calls
            total_out = out * calls
            model = models[agent]
            cost = estimate_cost(model, total_inp, total_out)
            total_cost += cost

            call_str = f" x{calls}" if calls > 1 else ""
            lines.append(f"  {agent}{call_str}: ~${cost:.4f}")

        lines.append(f"\nEstimated total: ~${total_cost:.4f}")

        await update.message.reply_text("\n".join(lines))

    # ── Provider Selection ──

    PROVIDER_LABELS = {
        "anthropic": "Anthropic (Claude)",
        "openai": "OpenAI (GPT)",
    }

    async def _handle_provider(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not update.message or not update.effective_user:
            return
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("Access denied.")
            return

        current = self.config.llm.provider
        current_label = self.PROVIDER_LABELS.get(current, current)
        default_fast, default_quality = get_provider_defaults(current)

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🟣 Anthropic", callback_data="provider:anthropic"
                ),
                InlineKeyboardButton(
                    "🟢 OpenAI", callback_data="provider:openai"
                ),
            ]
        ])

        await update.message.reply_text(
            f"🔧 LLM Provider: {current_label}\n"
            f"Fast model: {default_fast}\n"
            f"Quality model: {default_quality}\n\n"
            f"Choose provider:",
            reply_markup=keyboard,
        )

    async def _handle_provider_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        if not query or not update.effective_user:
            return
        if not self._is_authorized(update.effective_user.id):
            await query.answer("Access denied.")
            return

        if not query.data or not query.data.startswith("provider:"):
            return

        provider_name = query.data.split(":", 1)[1]
        if provider_name not in self.PROVIDER_LABELS:
            await query.answer("Unknown provider.")
            return

        if provider_name == self.config.llm.provider:
            await query.answer("Already using this provider.")
            return

        try:
            result = await self._reinit_provider(provider_name)
            await query.answer(f"Switched to {provider_name}")
            await query.edit_message_text(f"✅ Switched to {result}")
        except ValueError as e:
            await query.answer(str(e))
            await query.edit_message_text(f"❌ {e}")

    async def _reinit_provider(self, provider_name: str) -> str:
        """Hot-reload the LLM provider and re-create all agents."""
        if provider_name in ("anthropic", "claude"):
            api_key = self.config.llm.anthropic_api_key
            provider_name = "anthropic"
        elif provider_name == "openai":
            api_key = self.config.llm.openai_api_key
        else:
            raise ValueError(f"Unknown provider: {provider_name}")

        if not api_key:
            raise ValueError(f"No API key configured for {provider_name}")

        llm = create_provider(provider_name, api_key)
        default_fast, default_quality = get_provider_defaults(provider_name)

        # Update config
        self.config.llm.provider = provider_name
        self.config.llm.collector_model = default_fast
        self.config.llm.clusterer_model = default_fast
        self.config.llm.researcher_model = default_fast
        self.config.llm.writer_model = default_quality
        self.config.llm.editor_model = default_quality
        self.config.llm.translator_model = default_fast

        # Re-create collector (used directly by bot)
        self.collector = CollectorAgent(
            llm, default_fast, self.db, self.config.user_profile
        )

        # Re-create orchestrator's agents
        self.orchestrator.clusterer = ClustererAgent(
            llm, default_fast, self.db, self.config.user_profile
        )
        self.orchestrator.researcher = ResearcherAgent(
            llm, default_fast, self.db, self.config.user_profile
        )
        self.orchestrator.writer = WriterAgent(
            llm, default_quality, self.db, self.config.user_profile
        )
        self.orchestrator.editor = EditorAgent(
            llm, default_quality, self.db, self.config.user_profile
        )
        self.orchestrator.translator = TranslatorAgent(
            llm, default_fast, self.db, self.config.user_profile
        )

        # Persist preference
        await self.db.set_setting("llm_provider", provider_name)

        label = self.PROVIDER_LABELS.get(provider_name, provider_name)
        return f"{label}\nFast: {default_fast}\nQuality: {default_quality}"

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
            BotCommand("provider", "Switch LLM provider"),
            BotCommand("estimate", "Estimate generation cost"),
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
        self.app.add_handler(CommandHandler("estimate", self._handle_estimate))
        self.app.add_handler(CommandHandler("provider", self._handle_provider))
        self.app.add_handler(
            CallbackQueryHandler(
                self._handle_provider_callback, pattern=r"^provider:"
            )
        )
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
