"""Telegram bot with all commands — message collection via Collector agent + DB."""

import asyncio
import html
import io
import json
import logging
import re
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

from ..agents.clusterer import ClustererAgent
from ..agents.collector import CollectorAgent
from ..agents.editor import EditorAgent
from ..agents.filter import FilterAgent
from ..agents.profiler import ProfilerAgent
from ..agents.researcher import ResearcherAgent
from ..agents.translator import TranslatorAgent
from ..agents.writer import WriterAgent
from ..config import Config, get_provider_defaults
from ..content.text_classifier import classify_message
from ..content.url_parser import fetch_and_extract
from ..db.database import Database
from ..db.models import Cluster, DigestResult, Item, ItemStatus, ItemType
from ..demo_content import build_demo_items
from ..llm.provider import create_provider, estimate_cost
from ..pipeline.orchestrator import Orchestrator
from ..pipeline.status_updater import StatusUpdater
from ..rss_fetcher import RSSFetcher
from ..smoke_test import (
    SMOKE_ITEM_LIMIT_DEFAULT,
    SMOKE_ITEM_LIMIT_MAX,
    SmokeClustererAgent,
    SmokeEditorAgent,
    SmokeFilterAgent,
    SmokeResearcherAgent,
    SmokeTranslatorAgent,
    SmokeWriterAgent,
    get_smoke_models,
    prepare_smoke_test_items,
)

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
        dry_run_orchestrator: Orchestrator | None = None,
        smoke_test_orchestrator: Orchestrator | None = None,
    ):
        self.config = config
        self.db = db
        self.collector = collector
        self.orchestrator = orchestrator
        self.profiler = profiler
        self.dry_run_orchestrator = dry_run_orchestrator
        self.smoke_test_orchestrator = smoke_test_orchestrator
        self.app: Application | None = None
        self._generating = False
        self._dry_running = False
        self._smoke_testing = False
        self._feedback_map: dict[str, dict] = {}

    def _is_authorized(self, user_id: int) -> bool:
        return user_id in self.config.telegram.user_ids

    async def _load_profile(self) -> dict | None:
        """Load user profile from DB, return parsed dict or None."""
        raw = await self.db.get_setting("user_profile")
        if not raw:
            return None
        return json.loads(raw)

    async def _save_profile(self, profile: dict) -> None:
        """Save profile to DB and refresh filter agent prompt."""
        self.config.user_profile = profile
        await self.db.set_setting(
            "user_profile", json.dumps(profile, ensure_ascii=False)
        )
        agents = [self.collector]
        for orchestrator in [
            self.orchestrator,
            self.dry_run_orchestrator,
            self.smoke_test_orchestrator,
        ]:
            if not orchestrator:
                continue
            agents.extend([
                getattr(orchestrator, "clusterer", None),
                getattr(orchestrator, "researcher", None),
                getattr(orchestrator, "writer", None),
                getattr(orchestrator, "editor", None),
                getattr(orchestrator, "translator", None),
                getattr(orchestrator, "filter_agent", None),
            ])
        for agent in agents:
            if agent and hasattr(agent, "update_profile"):
                agent.update_profile(profile)

    @staticmethod
    def _normalize_topic_token(value: str) -> str:
        normalized = re.sub(r"[_\W]+", " ", value.casefold(), flags=re.UNICODE)
        return " ".join(normalized.split())

    def _truncate_preview(self, text: str, max_len: int = 300) -> str:
        text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
        text = re.sub(r"`([^`]*)`", r"\1", text)
        text = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", text)
        text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
        text = re.sub(r"^#+\s*", "", text, flags=re.MULTILINE)
        text = re.sub(r"[>*_~]", " ", text)
        text = " ".join(text.split()).strip()

        if not text:
            return "No preview available."
        if len(text) <= max_len:
            return text

        candidate = text[: max_len + 1]
        sentence_endings = [candidate.rfind(marker) for marker in (". ", "! ", "? ")]
        boundary = max(sentence_endings)
        if boundary >= max_len // 2:
            return candidate[: boundary + 1].strip()

        word_boundary = candidate.rfind(" ")
        if word_boundary > 0:
            candidate = candidate[:word_boundary]
        else:
            candidate = candidate[:max_len]
        return candidate.rstrip(" ,;:-") + "..."

    def _map_cluster_to_areas(
        self,
        cluster: Cluster,
        items: list[Item],
        interest_areas: list[dict],
    ) -> list[str]:
        item_lookup = {item.id: item for item in items}
        cluster_tags: set[str] = set()
        for item_id in cluster.item_ids:
            item = item_lookup.get(item_id)
            if not item:
                continue
            for tag in item.tags:
                normalized_tag = self._normalize_topic_token(tag)
                if normalized_tag:
                    cluster_tags.add(normalized_tag)
        if not cluster_tags or not interest_areas:
            return []

        matched_area_ids: list[str] = []
        for area in interest_areas:
            area_id = area.get("id")
            if not area_id:
                continue

            candidates = {
                self._normalize_topic_token(str(candidate))
                for candidate in [
                    area.get("name", ""),
                    area_id,
                    *area.get("keywords", []),
                ]
                if candidate
            }
            if not candidates:
                continue

            is_match = False
            for tag in cluster_tags:
                tag_tokens = set(tag.split())
                for candidate in candidates:
                    candidate_tokens = set(candidate.split())
                    if (
                        tag == candidate
                        or tag_tokens.issubset(candidate_tokens)
                        or candidate_tokens.issubset(tag_tokens)
                    ):
                        is_match = True
                        break
                if is_match:
                    matched_area_ids.append(area_id)
                    break

        return matched_area_ids

    async def _send_feedback_messages(
        self,
        chat_id: int,
        context: ContextTypes.DEFAULT_TYPE,
        result: DigestResult,
        profile: dict,
    ) -> None:
        interest_areas = profile.get("interest_areas", [])
        sorted_clusters = sorted(result.clusters, key=lambda cluster: cluster.priority)

        for cluster in sorted_clusters:
            token = uuid4().hex[:6]
            while token in self._feedback_map:
                token = uuid4().hex[:6]

            matched_area_ids = self._map_cluster_to_areas(
                cluster,
                result.items,
                interest_areas,
            )
            week_id = result.items[0].week_id if result.items else Database.current_week_id()
            self._feedback_map[token] = {
                "week_id": week_id,
                "cluster_id": cluster.id,
                "cluster_title": cluster.title,
                "matched_area_ids": matched_area_ids,
            }

            preview_source = (
                result.articles.get(cluster.id)
                or cluster.editorial_angle
                or cluster.title
            )
            preview = self._truncate_preview(preview_source)
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("👍", callback_data=f"fb:L:{token}"),
                    InlineKeyboardButton("👎", callback_data=f"fb:D:{token}"),
                ]
            ])

            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"<b>{html.escape(cluster.title)}</b>\n\n"
                        f"{html.escape(preview)}\n\n"
                        "Was this article useful?"
                    ),
                    parse_mode="HTML",
                    reply_markup=keyboard,
                    disable_web_page_preview=True,
                )
            except Exception as e:
                logger.warning(
                    "Failed to send feedback preview for cluster %s: %s",
                    cluster.id,
                    e,
                )
                self._feedback_map.pop(token, None)
            await asyncio.sleep(0.3)

        quick_bite_ids = set(result.quick_bites_item_ids)
        quick_bites_items = [
            item for item in result.items if item.id in quick_bite_ids
        ]
        if quick_bites_items:
            lines = ["<b>Quick Bites</b>", ""]
            for item in quick_bites_items:
                lines.append(
                    f"• {html.escape(self._truncate_preview(item.summary, max_len=180))}"
                )
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="\n".join(lines),
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception as e:
                logger.warning("Failed to send quick bites preview: %s", e)

    async def _handle_feedback_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        if not query or not query.data or not update.effective_user:
            return
        if not self._is_authorized(update.effective_user.id):
            await query.answer("Access denied.")
            return

        match = re.match(r"^fb:([LD]):([0-9a-f]{6})$", query.data)
        if not match:
            await query.answer("Invalid feedback.")
            return

        direction, token = match.groups()
        payload = self._feedback_map.pop(token, None)
        if not payload:
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            await query.answer("Feedback expired.")
            return

        feedback = "like" if direction == "L" else "dislike"
        delta = 0.03 if direction == "L" else -0.03

        await self.db.save_article_feedback(
            week_id=payload["week_id"],
            cluster_id=payload["cluster_id"],
            cluster_title=payload["cluster_title"],
            feedback=feedback,
            matched_area_ids=payload["matched_area_ids"],
        )

        profile = await self._load_profile() or self.config.user_profile or {}
        area_ids = set(payload["matched_area_ids"])
        if area_ids and profile.get("interest_areas"):
            changed = False
            for area in profile["interest_areas"]:
                if area.get("id") not in area_ids:
                    continue
                old_weight = float(area.get("weight", 0.0))
                new_weight = round(min(1.0, max(0.05, old_weight + delta)), 2)
                if new_weight != old_weight:
                    area["weight"] = new_weight
                    changed = True
            if changed:
                await self._save_profile(profile)

        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

        await query.answer("👍 Noted!" if direction == "L" else "👎 Noted!")

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

        summary_preview = item.summary[:100] + "..." if len(item.summary) > 100 else item.summary
        reply = f"{icon} Saved: \"{summary_preview}\"\nTags: {tags_str}"
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
            "/regenerate [week] — Re-generate from existing items\n"
            "/smoketest [n|demo] — Real API, low-cost smoke test\n"
            "/items — List this week's items\n"
            "/delete <id> — Remove an item\n"
            "/setup — Configure your interest profile\n"
            "/settings — View your interest profile\n"
            "/topic — Manage interest areas\n"
            "/block — Add blocked topic\n"
            "/unblock — Remove blocked topic\n"
            "/threshold [preset] — View/set filtering strictness\n"
            "/demo — Seed demo items for dry-run testing\n"
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
                    with open(result.file_path, "rb") as f:
                        await context.bot.send_document(
                            chat_id=update.effective_chat.id,
                            document=f,
                            filename=f"digest-{week_id}.md",
                            caption=f"📖 Your weekly digest is ready!",
                        )
                    profile = await self._load_profile() or self.config.user_profile or {}
                    await self._send_feedback_messages(
                        update.effective_chat.id,
                        context,
                        result,
                        profile,
                    )
                except Exception as e:
                    logger.error("Failed to send document: %s", e)
                    await update.message.reply_text(
                        f"✅ Digest generated and saved to: {result.file_path}"
                    )
        except Exception as e:
            await update.message.reply_text(f"❌ Generation failed: {e}")
        finally:
            self._generating = False

    async def _handle_dryrun(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Run the full pipeline with a mock LLM — no API calls, no token spend."""
        if not update.message or not update.effective_user:
            return
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("Access denied.")
            return

        if not self.dry_run_orchestrator:
            await update.message.reply_text("❌ Dry-run orchestrator not configured.")
            return

        if self._generating or self._dry_running:
            await update.message.reply_text("⏳ Generation already in progress.")
            return

        week_id = Database.current_week_id()
        items = await self.db.get_items_by_week(week_id, status=ItemStatus.COLLECTED)

        if not items:
            await update.message.reply_text(
                f"No collected items for {week_id}. Send some content first, "
                f"or use /demo to seed mock items."
            )
            return

        self._dry_running = True
        await update.message.reply_text(
            f"🧪 *Dry run* — running pipeline on {len(items)} items with mock LLM (no API calls)…",
            parse_mode="Markdown",
        )
        status_updater = StatusUpdater(context.bot, update.effective_chat.id)

        try:
            result = await self.dry_run_orchestrator.run(week_id, status_updater)
            if result:
                try:
                    with open(result.file_path, "rb") as f:
                        await context.bot.send_document(
                            chat_id=update.effective_chat.id,
                            document=f,
                            filename=f"dryrun-{week_id}.md",
                            caption="🧪 Dry-run digest (mock LLM — structure only, no real content)",
                        )
                except Exception as e:
                    logger.error("Failed to send dry-run document: %s", e)
                    await update.message.reply_text(
                        f"✅ Dry-run complete. Saved to: {result.file_path}"
                    )
            else:
                await update.message.reply_text("⚠️ Dry run produced no output (all items filtered?).")
        except Exception as e:
            await update.message.reply_text(f"❌ Dry run failed: {e}")
        finally:
            self._dry_running = False

    async def _handle_smoketest(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Run a minimal-cost real-API pipeline for prompt tuning."""
        if not update.message or not update.effective_user:
            return
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("Access denied.")
            return

        if not self.smoke_test_orchestrator:
            await update.message.reply_text("❌ Smoke-test orchestrator not configured.")
            return

        if self._generating or self._dry_running or self._smoke_testing:
            await update.message.reply_text("⏳ Generation already in progress.")
            return

        week_id = Database.current_week_id()
        args = [arg.lower() for arg in (context.args or [])]
        use_demo_items = "demo" in args

        item_limit = SMOKE_ITEM_LIMIT_DEFAULT
        for arg in args:
            if arg.isdigit():
                item_limit = max(1, min(int(arg), SMOKE_ITEM_LIMIT_MAX))
                break

        if use_demo_items:
            source_items = build_demo_items(week_id)
            source_label = "demo items"
        else:
            source_items = await self.db.get_items_by_week(
                week_id,
                status=ItemStatus.COLLECTED,
            )
            source_label = "collected items"

        if not source_items:
            await update.message.reply_text(
                f"No collected items for {week_id}.\n"
                f"Send a few real items first, or use `/smoketest demo`.",
                parse_mode="Markdown",
            )
            return

        smoke_items = prepare_smoke_test_items(source_items, limit=item_limit)
        if not smoke_items:
            await update.message.reply_text("No items available for smoke test.")
            return

        self._smoke_testing = True
        status_updater = StatusUpdater(context.bot, update.effective_chat.id)
        provider = self.config.llm.provider

        await update.message.reply_text(
            f"🧪 Real smoke test — running the full pipeline on {len(smoke_items)} {source_label}\n"
            f"Provider: {provider}\n"
            f"Translation: off\n"
            "Mode: low-cost models + capped outputs + no publish side effects"
        )

        try:
            result = await self.smoke_test_orchestrator.run(
                week_id,
                status_updater,
                items_override=smoke_items,
                digest_language_override="en",
            )
            if result:
                try:
                    with open(result.file_path, "rb") as f:
                        await context.bot.send_document(
                            chat_id=update.effective_chat.id,
                            document=f,
                            filename=f"smoketest-{week_id}.md",
                            caption="🧪 Real-API smoke-test digest",
                        )
                except Exception as e:
                    logger.error("Failed to send smoke-test document: %s", e)
                    await update.message.reply_text(
                        f"✅ Smoke test complete. Saved to: {result.file_path}"
                    )

                last_run = await self.db.get_last_run(week_id)
                if last_run:
                    await update.message.reply_text(
                        f"Tokens: {last_run.total_input_tokens:,} in / "
                        f"{last_run.total_output_tokens:,} out\n"
                        f"Estimated cost: ${last_run.estimated_cost_usd:.4f}"
                    )
            else:
                await update.message.reply_text("⚠️ Smoke test produced no output.")
        except Exception as e:
            await update.message.reply_text(f"❌ Smoke test failed: {e}")
        finally:
            self._smoke_testing = False

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

    async def _handle_demo(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not update.message or not update.effective_user:
            return
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("Access denied.")
            return

        week_id = Database.current_week_id()
        args = [arg.lower() for arg in (context.args or [])]
        existing_items = await self.db.get_items_by_week(week_id)
        demo_items = [
            item for item in existing_items if item.raw_content.startswith("[DEMO]")
        ]

        if args and args[0] in {"clear", "reset"}:
            for item in demo_items:
                await self.db.delete_item(item.id)
            if args[0] == "clear":
                await update.message.reply_text(
                    f"Removed {len(demo_items)} demo item(s) from {week_id}."
                )
                return
            existing_items = [
                item for item in existing_items if not item.raw_content.startswith("[DEMO]")
            ]
        elif demo_items:
            await update.message.reply_text(
                f"{len(demo_items)} demo item(s) already exist for {week_id}.\n"
                "Use /demo reset to replace them or /demo clear to remove them."
            )
            return

        seeded_items = build_demo_items(week_id)
        for item in seeded_items:
            await self.db.save_item(item)

        total_count = len(existing_items) + len(seeded_items)
        await update.message.reply_text(
            f"Seeded {len(seeded_items)} demo item(s) for {week_id}.\n\n"
            "Next:\n"
            "/items — inspect seeded content\n"
            "/dryrun — run the full pipeline with zero API spend\n"
            "/demo clear — remove demo items\n\n"
            f"Current week total: {total_count} item(s)"
        )

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
        if self.orchestrator.filter_agent:
            self.orchestrator.filter_agent = FilterAgent(
                llm, default_fast, self.db, self.config.user_profile
            )

        if self.smoke_test_orchestrator:
            smoke_fast_model, smoke_quality_model = get_smoke_models(provider_name)
            self.smoke_test_orchestrator.clusterer = SmokeClustererAgent(
                llm, smoke_fast_model, self.db, self.config.user_profile
            )
            self.smoke_test_orchestrator.researcher = SmokeResearcherAgent(
                llm, smoke_quality_model, self.db, self.config.user_profile
            )
            self.smoke_test_orchestrator.writer = SmokeWriterAgent(
                llm, smoke_quality_model, self.db, self.config.user_profile
            )
            self.smoke_test_orchestrator.editor = SmokeEditorAgent(
                llm, smoke_quality_model, self.db, self.config.user_profile
            )
            self.smoke_test_orchestrator.translator = SmokeTranslatorAgent(
                llm, smoke_fast_model, self.db, self.config.user_profile
            )
            if self.smoke_test_orchestrator.filter_agent:
                self.smoke_test_orchestrator.filter_agent = SmokeFilterAgent(
                    llm, smoke_fast_model, self.db, self.config.user_profile
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

    # ── Settings overview (/settings) ──

    async def _handle_settings(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not update.message or not update.effective_user:
            return
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("Access denied.")
            return

        profile_json = await self.db.get_setting("user_profile")
        if not profile_json:
            await update.message.reply_text(
                "No profile configured yet. Run /setup first."
            )
            return

        profile = json.loads(profile_json)
        areas = profile.get("interest_areas", [])

        if not areas:
            await update.message.reply_text(
                "Profile exists but has no interest areas. Run /setup to configure."
            )
            return

        user = profile.get("user", {})
        name = user.get("preferred_name", "")
        strictness = profile.get("strictness",
                      profile.get("filtering_strictness", "moderate"))
        blocked = profile.get("blocked_topics", [])

        lines = ["⚙️ *Your Profile*\n"]
        if name:
            lines.append(f"👤 {name}\n")

        lines.append("📌 *Interest areas:*")
        for area in areas:
            area_name = area.get("name", area.get("id", "unknown"))
            weight = area.get("weight", 0.0)
            priority = _priority_for_weight(weight)
            label = PRIORITY_LABELS.get(priority, "")
            # Escape underscores for Markdown
            display_name = area_name.replace("_", "\\_")
            lines.append(f"  {label} {display_name} ({weight:.2f})")

        if blocked:
            blocked_str = ", ".join(blocked[:10])
            if len(blocked) > 10:
                blocked_str += f" (+{len(blocked) - 10} more)"
            lines.append(f"\n🚫 *Blocked:* {blocked_str}")

        strictness_labels = {
            "strict": "🔒 Strict",
            "moderate": "⚖️ Moderate",
            "relaxed": "🔓 Relaxed",
        }
        lines.append(f"\n{strictness_labels.get(strictness, strictness)}")
        lines.append(
            "\n/topic — manage areas  •  /block — manage blocked\n"
            "/threshold — strictness  •  /setup — full reconfigure"
        )

        await update.message.reply_text(
            "\n".join(lines), parse_mode="Markdown"
        )

    # ── Filter Strictness (/threshold) ──

    async def _handle_threshold(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not update.message or not update.effective_user:
            return
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("Access denied.")
            return

        profile_json = await self.db.get_setting("user_profile")
        if not profile_json:
            await update.message.reply_text(
                "No profile configured. Run /setup first."
            )
            return

        from ..profile_defaults import get_scoring_thresholds

        profile = json.loads(profile_json)
        current_strictness = profile.get("strictness",
                              profile.get("filtering_strictness", "moderate"))

        args = context.args
        if not args:
            thresholds = get_scoring_thresholds(current_strictness)
            await update.message.reply_text(
                f"⚖️ *Filtering strictness*: {current_strictness}\n"
                f"Drop\\-below threshold: `{thresholds['drop_below']:.2f}`\n\n"
                f"Usage: `/threshold strict|moderate|relaxed`\n\n"
                f"Presets:\n"
                f"  🔒 strict — drop below 0\\.40\n"
                f"  ⚖️ moderate — drop below 0\\.25\n"
                f"  🔓 relaxed — drop below 0\\.15",
                parse_mode="MarkdownV2",
            )
            return

        new_strictness = args[0].lower()
        if new_strictness not in ("strict", "moderate", "relaxed"):
            await update.message.reply_text(
                "Unknown preset. Use: strict, moderate, or relaxed."
            )
            return

        profile["strictness"] = new_strictness
        # Clean up legacy keys if present
        profile.pop("filtering_strictness", None)
        profile.pop("scoring_hints_for_python", None)

        await self._save_profile(profile)

        thresholds = get_scoring_thresholds(new_strictness)
        await update.message.reply_text(
            f"✅ Strictness updated: {current_strictness} → {new_strictness}\n"
            f"Drop-below threshold: {thresholds['drop_below']:.2f}",
        )

    # ── Topic management (/topic) ──

    async def _handle_topic(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not update.message or not update.effective_user:
            return
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("Access denied.")
            return

        profile = await self._load_profile()
        if not profile:
            await update.message.reply_text(
                "No profile configured. Run /setup first."
            )
            return

        args = context.args or []
        areas = profile.get("interest_areas", [])

        # /topic — list areas
        if not args:
            if not areas:
                await update.message.reply_text(
                    "No interest areas configured. Use /topic add <name> or /setup."
                )
                return
            lines = ["📌 *Interest areas:*\n"]
            for area in areas:
                name = area.get("name", area.get("id", "?"))
                weight = area.get("weight", 0.0)
                priority = _priority_for_weight(weight)
                label = PRIORITY_LABELS.get(priority, "")
                display = name.replace("_", "\\_")
                kw = area.get("keywords", [])
                kw_str = ", ".join(kw[:5])
                if len(kw) > 5:
                    kw_str += f" (+{len(kw) - 5})"
                lines.append(f"{label} *{display}* ({weight:.2f})")
                if kw_str:
                    lines.append(f"  _{kw_str}_")
            lines.append(
                "\n`/topic add <name>` — add area\n"
                "`/topic remove <name>` — remove area\n"
                "`/topic keywords <name> add|remove <kw>` — edit keywords"
            )
            await update.message.reply_text(
                "\n".join(lines), parse_mode="Markdown"
            )
            return

        action = args[0].lower()

        # /topic add <name> [weight]
        if action == "add":
            if len(args) < 2:
                await update.message.reply_text(
                    "Usage: /topic add <name> [weight]\n"
                    "Example: /topic add Cybersecurity 0.80"
                )
                return
            # Name can be multi-word: everything between action and optional trailing float
            raw_parts = args[1:]
            weight = 0.70  # default medium
            # Check if last arg is a float
            try:
                weight = float(raw_parts[-1])
                weight = max(0.0, min(1.0, weight))
                name_parts = raw_parts[:-1]
            except ValueError:
                name_parts = raw_parts

            if not name_parts:
                await update.message.reply_text("Please provide a topic name.")
                return

            area_name = " ".join(name_parts)
            area_id = area_name.lower().replace(" ", "_").replace("&", "and")

            # Check for duplicate
            if any(a.get("id") == area_id for a in areas):
                await update.message.reply_text(
                    f"Area '{area_name}' already exists. Use /topic remove first."
                )
                return

            areas.append({
                "id": area_id,
                "name": area_name,
                "weight": weight,
                "keywords": [],
            })
            profile["interest_areas"] = areas
            await self._save_profile(profile)

            priority = _priority_for_weight(weight)
            label = PRIORITY_LABELS.get(priority, "")
            await update.message.reply_text(
                f"✅ Added: {label} {area_name} ({weight:.2f})\n\n"
                f"Add keywords: /topic keywords {area_name} add <keyword>"
            )
            return

        # /topic remove <name>
        if action in ("remove", "rm", "delete"):
            if len(args) < 2:
                await update.message.reply_text("Usage: /topic remove <name>")
                return
            search = " ".join(args[1:]).lower()
            match = next(
                (a for a in areas
                 if a.get("id", "").lower() == search
                 or a.get("name", "").lower() == search),
                None,
            )
            if not match:
                await update.message.reply_text(
                    f"Area '{search}' not found. Use /topic to see the list."
                )
                return
            areas.remove(match)
            profile["interest_areas"] = areas
            await self._save_profile(profile)
            await update.message.reply_text(
                f"✅ Removed: {match.get('name', match.get('id'))}"
            )
            return

        # /topic weight <name> <value>
        if action == "weight":
            if len(args) < 3:
                await update.message.reply_text(
                    "Usage: /topic weight <name> <value>\n"
                    "Example: /topic weight AI 0.95"
                )
                return
            try:
                new_weight = float(args[-1])
                new_weight = max(0.0, min(1.0, new_weight))
            except ValueError:
                await update.message.reply_text("Weight must be a number 0.0–1.0.")
                return
            search = " ".join(args[1:-1]).lower()
            match = next(
                (a for a in areas
                 if a.get("id", "").lower() == search
                 or a.get("name", "").lower() == search),
                None,
            )
            if not match:
                await update.message.reply_text(
                    f"Area '{search}' not found. Use /topic to see the list."
                )
                return
            old_weight = match.get("weight", 0.0)
            match["weight"] = new_weight
            await self._save_profile(profile)
            label = PRIORITY_LABELS.get(_priority_for_weight(new_weight), "")
            await update.message.reply_text(
                f"✅ {match['name']}: {old_weight:.2f} → {label} {new_weight:.2f}"
            )
            return

        # /topic keywords <name> add|remove <keyword>
        if action in ("keywords", "kw"):
            if len(args) < 4:
                await update.message.reply_text(
                    "Usage: /topic keywords <name> add|remove <keyword>\n"
                    "Example: /topic keywords AI add transformers"
                )
                return
            # Find the area name — it's between "keywords" and "add"/"remove"
            # Look for add/remove in args to split
            kw_action_idx = None
            for i, a in enumerate(args[1:], 1):
                if a.lower() in ("add", "remove", "rm"):
                    kw_action_idx = i
                    break
            if kw_action_idx is None or kw_action_idx < 2 or kw_action_idx >= len(args) - 1:
                await update.message.reply_text(
                    "Usage: /topic keywords <name> add|remove <keyword>"
                )
                return
            search = " ".join(args[1:kw_action_idx]).lower()
            kw_action = args[kw_action_idx].lower()
            keyword = " ".join(args[kw_action_idx + 1:])

            match = next(
                (a for a in areas
                 if a.get("id", "").lower() == search
                 or a.get("name", "").lower() == search),
                None,
            )
            if not match:
                await update.message.reply_text(
                    f"Area '{search}' not found. Use /topic to see the list."
                )
                return

            kws = match.get("keywords", [])
            if kw_action == "add":
                if keyword.lower() in [k.lower() for k in kws]:
                    await update.message.reply_text(
                        f"Keyword '{keyword}' already exists in {match['name']}."
                    )
                    return
                kws.append(keyword)
                match["keywords"] = kws
                await self._save_profile(profile)
                await update.message.reply_text(
                    f"✅ Added keyword '{keyword}' to {match['name']}"
                )
            else:  # remove
                original_len = len(kws)
                kws = [k for k in kws if k.lower() != keyword.lower()]
                if len(kws) == original_len:
                    await update.message.reply_text(
                        f"Keyword '{keyword}' not found in {match['name']}."
                    )
                    return
                match["keywords"] = kws
                await self._save_profile(profile)
                await update.message.reply_text(
                    f"✅ Removed keyword '{keyword}' from {match['name']}"
                )
            return

        await update.message.reply_text(
            "Unknown action. Use: /topic, /topic add, /topic remove, "
            "/topic weight, /topic keywords"
        )

    # ── Block/Unblock topics (/block, /unblock) ──

    async def _handle_block(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not update.message or not update.effective_user:
            return
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("Access denied.")
            return

        profile = await self._load_profile()
        if not profile:
            await update.message.reply_text(
                "No profile configured. Run /setup first."
            )
            return

        args = context.args or []
        if not args:
            blocked = profile.get("blocked_topics", [])
            if blocked:
                lines = ["🚫 *Blocked topics:*\n"]
                for t in blocked:
                    lines.append(f"• {t}")
                lines.append("\n`/block <topic>` — add\n`/unblock <topic>` — remove")
                await update.message.reply_text(
                    "\n".join(lines), parse_mode="Markdown"
                )
            else:
                await update.message.reply_text(
                    "No blocked topics. Use /block <topic> to add one."
                )
            return

        topic = " ".join(args)
        blocked = profile.get("blocked_topics", [])

        if topic.lower() in [b.lower() for b in blocked]:
            await update.message.reply_text(f"'{topic}' is already blocked.")
            return

        blocked.append(topic)
        profile["blocked_topics"] = blocked
        await self._save_profile(profile)
        await update.message.reply_text(f"✅ Blocked: {topic}")

    async def _handle_unblock(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not update.message or not update.effective_user:
            return
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("Access denied.")
            return

        profile = await self._load_profile()
        if not profile:
            await update.message.reply_text(
                "No profile configured. Run /setup first."
            )
            return

        args = context.args or []
        if not args:
            await update.message.reply_text("Usage: /unblock <topic>")
            return

        topic = " ".join(args)
        blocked = profile.get("blocked_topics", [])
        original_len = len(blocked)
        blocked = [b for b in blocked if b.lower() != topic.lower()]

        if len(blocked) == original_len:
            await update.message.reply_text(
                f"'{topic}' is not in the blocked list. Use /block to see all."
            )
            return

        profile["blocked_topics"] = blocked
        await self._save_profile(profile)
        await update.message.reply_text(f"✅ Unblocked: {topic}")

    # ── Regenerate digest (/regenerate) ──

    async def _handle_regenerate(
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

        args = context.args
        if args:
            week_id = args[0]
            import re as _re
            if not _re.match(r"^\d{4}-W\d{2}$", week_id):
                await update.message.reply_text(
                    "Invalid week format. Use YYYY-Www, e.g. `2026-W09`",
                    parse_mode="Markdown",
                )
                return
        else:
            week_id = Database.current_week_id()

        all_items = await self.db.get_items_by_week(week_id)
        if not all_items:
            await update.message.reply_text(f"No items found for {week_id}.")
            return

        published_items = [i for i in all_items if i.status == ItemStatus.PUBLISHED]
        collected_items = [i for i in all_items if i.status == ItemStatus.COLLECTED]

        if not published_items and not collected_items:
            await update.message.reply_text(f"No items found for {week_id}.")
            return

        if published_items:
            await self.db.update_items_status(
                [i.id for i in published_items], ItemStatus.COLLECTED
            )

        total = len(all_items)
        reset_count = len(published_items)
        await update.message.reply_text(
            f"♻️ Regenerating digest for {week_id}\n"
            f"Total items: {total}"
            + (f" ({reset_count} reset from PUBLISHED)" if reset_count else "")
        )

        self._generating = True
        status_updater = StatusUpdater(context.bot, update.effective_chat.id)

        try:
            result = await self.orchestrator.run(week_id, status_updater)
            if result:
                try:
                    with open(result.file_path, "rb") as f:
                        await context.bot.send_document(
                            chat_id=update.effective_chat.id,
                            document=f,
                            filename=f"digest-{week_id}.md",
                            caption="📖 Your weekly digest is ready!",
                        )
                    profile = await self._load_profile() or self.config.user_profile or {}
                    await self._send_feedback_messages(
                        update.effective_chat.id,
                        context,
                        result,
                        profile,
                    )
                except Exception as e:
                    logger.error("Failed to send document: %s", e)
                    await update.message.reply_text(
                        f"✅ Digest generated and saved to: {result.file_path}"
                    )
        except Exception as e:
            await update.message.reply_text(f"❌ Generation failed: {e}")
        finally:
            self._generating = False

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

        await self._save_profile(profile)

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

    # ── RSS Commands ──

    async def _handle_rss(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not update.message or not update.effective_user:
            return
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("Access denied.")
            return

        args = context.args or []
        if not args:
            await update.message.reply_text(
                "Usage:\n"
                "/rss add <url> — Subscribe to an RSS feed\n"
                "/rss list — Show subscribed feeds\n"
                "/rss remove <number> — Unsubscribe by number from list"
            )
            return

        subcommand = args[0].lower()

        if subcommand == "add":
            await self._rss_add(update, args[1:])
        elif subcommand == "list":
            await self._rss_list(update)
        elif subcommand == "remove":
            await self._rss_remove(update, args[1:])
        else:
            await update.message.reply_text(
                f"Unknown subcommand: {subcommand}\n"
                "Use /rss add, /rss list, or /rss remove."
            )

    async def _rss_add(self, update: Update, args: list[str]) -> None:
        if not args:
            await update.message.reply_text("Usage: /rss add <url>")
            return

        url = args[0].strip()

        # Basic URL validation
        if not url.startswith(("http://", "https://")):
            await update.message.reply_text("Please provide a valid URL starting with http:// or https://")
            return

        # Check for duplicate
        feeds = await self.db.get_rss_feeds()
        if any(f["url"] == url for f in feeds):
            await update.message.reply_text("This feed is already subscribed.")
            return

        # Try to fetch the feed to validate it
        await update.message.reply_text("🔗 Fetching feed...")
        fetcher = RSSFetcher()
        try:
            feed_title, entries = await fetcher.fetch_feed(url)
        except Exception as e:
            await update.message.reply_text(
                f"Could not fetch or parse the feed:\n{e}"
            )
            return

        # Save to database
        try:
            await self.db.add_rss_feed(url, title=feed_title)
        except Exception as e:
            logger.error("Failed to save RSS feed: %s", e)
            await update.message.reply_text(f"Failed to save feed: {e}")
            return

        await update.message.reply_text(
            f"✅ Subscribed to: {feed_title}\n"
            f"URL: {url}\n"
            f"Found {len(entries)} entries in feed."
        )

    async def _rss_list(self, update: Update) -> None:
        feeds = await self.db.get_rss_feeds()
        if not feeds:
            await update.message.reply_text("No RSS feeds subscribed. Use /rss add <url> to add one.")
            return

        lines = [f"📡 RSS Feeds ({len(feeds)}):\n"]
        for i, feed in enumerate(feeds, 1):
            title = feed["title"] or "Untitled"
            lines.append(f"{i}. {title}\n   {feed['url']}")

        await update.message.reply_text("\n".join(lines))

    async def _rss_remove(self, update: Update, args: list[str]) -> None:
        if not args:
            await update.message.reply_text("Usage: /rss remove <number>\nUse /rss list to see feed numbers.")
            return

        try:
            number = int(args[0])
        except ValueError:
            await update.message.reply_text("Please provide a valid number. Use /rss list to see feed numbers.")
            return

        feeds = await self.db.get_rss_feeds()
        if number < 1 or number > len(feeds):
            await update.message.reply_text(
                f"Invalid number. Use a number between 1 and {len(feeds)}.\n"
                "Use /rss list to see feed numbers."
            )
            return

        feed = feeds[number - 1]
        await self.db.remove_rss_feed(feed["id"])
        title = feed["title"] or "Untitled"
        await update.message.reply_text(f"🗑 Removed feed: {title}\n{feed['url']}")

    # ── Bot Setup ──

    @staticmethod
    async def _post_init(application: Application) -> None:
        """Set bot commands so they appear in Telegram's command menu."""
        await application.bot.set_my_commands([
            BotCommand("start", "Show welcome message & help"),
            BotCommand("generate", "Generate weekly digest now"),
            BotCommand("dryrun", "Test pipeline without spending tokens (mock LLM)"),
            BotCommand("smoketest", "Run a real low-cost pipeline smoke test"),
            BotCommand("regenerate", "Re-generate digest from existing items"),
            BotCommand("demo", "Seed mock items for dry-run testing"),
            BotCommand("items", "List this week's collected items"),
            BotCommand("delete", "Remove an item by ID"),
            BotCommand("setup", "Configure your interest profile"),
            BotCommand("settings", "View your interest profile"),
            BotCommand("topic", "Manage interest areas (add/remove/weight)"),
            BotCommand("block", "Block a topic from digests"),
            BotCommand("unblock", "Unblock a topic"),
            BotCommand("threshold", "View/set filtering strictness"),
            BotCommand("language", "Choose digest language (RU/EN)"),
            BotCommand("provider", "Switch LLM provider"),
            BotCommand("estimate", "Estimate generation cost"),
            BotCommand("status", "Show last pipeline run status"),
            BotCommand("logs", "Show last pipeline run logs"),
            BotCommand("cost", "Show token usage & cost report"),
            BotCommand("week", "Current week info & stats"),
            BotCommand("rss", "Manage RSS feed subscriptions"),
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
        self.app.add_handler(CommandHandler("dryrun", self._handle_dryrun))
        self.app.add_handler(CommandHandler("smoketest", self._handle_smoketest))
        self.app.add_handler(CommandHandler("demo", self._handle_demo))
        self.app.add_handler(CommandHandler("regenerate", self._handle_regenerate))
        self.app.add_handler(CommandHandler("settings", self._handle_settings))
        self.app.add_handler(CommandHandler("topic", self._handle_topic))
        self.app.add_handler(CommandHandler("block", self._handle_block))
        self.app.add_handler(CommandHandler("unblock", self._handle_unblock))
        self.app.add_handler(CommandHandler("threshold", self._handle_threshold))
        self.app.add_handler(CommandHandler("items", self._handle_items))
        self.app.add_handler(CommandHandler("delete", self._handle_delete))
        self.app.add_handler(CommandHandler("status", self._handle_status))
        self.app.add_handler(CommandHandler("logs", self._handle_logs))
        self.app.add_handler(CommandHandler("cost", self._handle_cost))
        self.app.add_handler(CommandHandler("week", self._handle_week))
        self.app.add_handler(CommandHandler("estimate", self._handle_estimate))
        self.app.add_handler(CommandHandler("provider", self._handle_provider))
        self.app.add_handler(
            CallbackQueryHandler(self._handle_feedback_callback, pattern=r"^fb:")
        )
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
        self.app.add_handler(CommandHandler("rss", self._handle_rss))
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
