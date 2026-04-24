"""Weekly digest scheduler — auto-triggers pipeline on configured day/time."""

import datetime
import logging
from typing import TYPE_CHECKING

from telegram.ext import Application, ContextTypes

from ..agents.collector import CollectorAgent
from ..config import ScheduleConfig
from ..db.database import Database
from ..pipeline.orchestrator import Orchestrator
from ..pipeline.status_updater import StatusUpdater
from ..rss_fetcher import PollResult, RSSFetcher, RSS_POLL_INTERVAL_SECONDS
from ..telegram.channel_reader import (
    ChannelReader,
    CHANNEL_POLL_INTERVAL_SECONDS,
)

if TYPE_CHECKING:
    from ..telegram.bot import DigestBot

logger = logging.getLogger(__name__)


def _resolve_timezone(name: str):
    try:
        import pytz
        return pytz.timezone(name)
    except ImportError:
        import zoneinfo
        return zoneinfo.ZoneInfo(name)


def _next_trigger_seconds(hour: int, minute: int, tz) -> float:
    """Seconds from now until the next occurrence of hour:minute in tz."""
    now = datetime.datetime.now(tz)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += datetime.timedelta(days=1)
    return (target - now).total_seconds()


def setup_schedule(
    app: Application,
    config: ScheduleConfig,
    orchestrator: Orchestrator,
    chat_ids: list[int],
    digest_bot: "DigestBot | None" = None,
) -> None:
    """Set up the recurring digest generation schedule (every N days)."""
    if not config.enabled:
        logger.info("Scheduled digest generation is disabled")
        return
    if not chat_ids:
        logger.warning("No authorized Telegram user IDs configured; scheduler disabled")
        return

    tz = _resolve_timezone(config.timezone)
    interval_seconds = max(1, config.interval_days) * 24 * 3600
    first_delay = _next_trigger_seconds(config.hour, config.minute, tz)

    async def scheduled_generate(context: ContextTypes.DEFAULT_TYPE) -> None:
        """Callback for scheduled digest generation."""
        period_id = Database.current_period_id()
        logger.info("Scheduled generation triggered for period %s", period_id)

        # Reuse one status thread for progress while broadcasting final output.
        status_updater = StatusUpdater(context.bot, chat_ids[0])
        try:
            result = await orchestrator.run(period_id, status_updater)
            if result:
                profile = None
                if digest_bot:
                    profile = await digest_bot._load_profile() or digest_bot.config.user_profile or {}
                for chat_id in chat_ids:
                    if getattr(result, "telegraph_url", None):
                        await context.bot.send_message(
                            chat_id=chat_id,
                            text=f"📖 Digest {period_id}: {result.telegraph_url}",
                            disable_web_page_preview=False,
                        )
                    with open(result.file_path, "rb") as f:
                        await context.bot.send_document(
                            chat_id=chat_id,
                            document=f,
                            filename=f"digest-{period_id}.md",
                            caption=f"📖 Digest for {period_id} is ready!",
                        )
                    if digest_bot and profile is not None:
                        await digest_bot._send_feedback_messages(
                            chat_id=chat_id,
                            context=context,
                            result=result,
                            profile=profile,
                        )
            else:
                for chat_id in chat_ids:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"No items collected for {period_id}. Skipping digest.",
                    )
        except Exception as e:
            logger.exception("Scheduled generation failed: %s", e)
            for chat_id in chat_ids:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"❌ Scheduled digest generation failed: {e}",
                )

    app.job_queue.run_repeating(
        callback=scheduled_generate,
        interval=interval_seconds,
        first=first_delay,
        name="digest_schedule",
    )

    logger.info(
        "Scheduled digest generation: every %d day(s), first at %02d:%02d %s (in %.1f min)",
        config.interval_days,
        config.hour,
        config.minute,
        config.timezone,
        first_delay / 60.0,
    )


def setup_rss_schedule(
    app: Application,
    db: Database,
    collector: CollectorAgent,
    chat_ids: list[int],
) -> None:
    """Set up periodic RSS feed polling."""
    if not chat_ids:
        logger.warning("No authorized Telegram user IDs configured; RSS scheduler disabled")
        return

    rss_fetcher = RSSFetcher()

    async def poll_rss_feeds(context: ContextTypes.DEFAULT_TYPE) -> None:
        """Callback for periodic RSS feed polling."""
        logger.info("RSS poll triggered")
        try:
            poll_result = await rss_fetcher.poll_all_feeds(
                db=db,
                collector_agent=collector,
                user_id=chat_ids[0],
            )
            if poll_result.total_added > 0:
                logger.info("RSS poll added %d new item(s)", poll_result.total_added)
                await _notify_rss_updates(context, chat_ids, poll_result)
        except Exception as e:
            logger.exception("RSS poll failed: %s", e)

    app.job_queue.run_repeating(
        callback=poll_rss_feeds,
        interval=RSS_POLL_INTERVAL_SECONDS,
        first=60,  # first poll 60 seconds after startup
        name="rss_poll",
    )

    logger.info(
        "RSS feed polling scheduled: every %d seconds",
        RSS_POLL_INTERVAL_SECONDS,
    )


def setup_channel_schedule(
    app: Application,
    db: Database,
    collector: CollectorAgent,
    channel_reader: ChannelReader,
    chat_ids: list[int],
) -> None:
    """Set up periodic Telegram channel polling via Telethon (allowlist-only)."""
    if not chat_ids:
        logger.warning(
            "No authorized Telegram user IDs configured; channel scheduler disabled"
        )
        return
    if not channel_reader.is_configured():
        logger.info(
            "Telegram channel ingestion disabled: "
            "set TELETHON_API_ID/HASH and run `python -m src.scripts.telethon_login`"
        )
        return

    async def poll_channels(context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.info("Channel poll triggered")
        try:
            result = await channel_reader.poll_channels(
                db=db, collector_agent=collector
            )
        except Exception as e:
            logger.exception("Channel poll failed: %s", e)
            return
        if result.total_added > 0:
            logger.info("Channel poll added %d new item(s)", result.total_added)
            await _notify_channel_updates(context, chat_ids, result)

    app.job_queue.run_repeating(
        callback=poll_channels,
        interval=CHANNEL_POLL_INTERVAL_SECONDS,
        first=90,
        name="channel_poll",
    )
    logger.info(
        "Telegram channel polling scheduled: every %d seconds",
        CHANNEL_POLL_INTERVAL_SECONDS,
    )


async def _notify_channel_updates(
    context: ContextTypes.DEFAULT_TYPE,
    chat_ids: list[int],
    poll_result,
) -> None:
    """Send Telegram notification about new channel posts."""
    by_channel: dict[str, list[dict]] = {}
    for item in poll_result.items:
        by_channel.setdefault(item["channel"], []).append(item)

    lines = [f"📡 <b>{poll_result.total_added} new channel post(s)</b>\n"]
    for channel, items in by_channel.items():
        lines.append(f"<b>{channel}</b>")
        for item in items:
            title = item["title"] or item["link"]
            lines.append(f"  • <a href=\"{item['link']}\">{title}</a>")
        lines.append("")

    text = "\n".join(lines).strip()
    for chat_id in chat_ids:
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception as e:
            logger.warning("Failed to send channel notification to %s: %s", chat_id, e)


async def _notify_rss_updates(
    context: ContextTypes.DEFAULT_TYPE,
    chat_ids: list[int],
    poll_result: PollResult,
) -> None:
    """Send Telegram notification about new RSS items."""
    # Group items by feed
    by_feed: dict[str, list[dict]] = {}
    for item in poll_result.items:
        by_feed.setdefault(item["feed_title"], []).append(item)

    lines = [f"📡 <b>{poll_result.total_added} new RSS article(s)</b>\n"]
    for feed_title, items in by_feed.items():
        lines.append(f"<b>{feed_title}</b>")
        for item in items:
            title = item["title"] or item["link"]
            lines.append(f"  • <a href=\"{item['link']}\">{title}</a>")
        lines.append("")

    text = "\n".join(lines).strip()

    for chat_id in chat_ids:
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception as e:
            logger.warning("Failed to send RSS notification to %s: %s", chat_id, e)
