"""Weekly digest scheduler — auto-triggers pipeline on configured day/time."""

import datetime
import logging

from telegram.ext import Application, ContextTypes

from ..agents.collector import CollectorAgent
from ..config import ScheduleConfig
from ..db.database import Database
from ..pipeline.orchestrator import Orchestrator
from ..pipeline.status_updater import StatusUpdater
from ..rss_fetcher import PollResult, RSSFetcher, RSS_POLL_INTERVAL_SECONDS

logger = logging.getLogger(__name__)


def setup_schedule(
    app: Application,
    config: ScheduleConfig,
    orchestrator: Orchestrator,
    chat_ids: list[int],
) -> None:
    """Set up the weekly digest generation schedule."""
    if not config.enabled:
        logger.info("Scheduled digest generation is disabled")
        return
    if not chat_ids:
        logger.warning("No authorized Telegram user IDs configured; scheduler disabled")
        return

    try:
        import pytz
        tz = pytz.timezone(config.timezone)
    except ImportError:
        import zoneinfo
        tz = zoneinfo.ZoneInfo(config.timezone)

    trigger_time = datetime.time(
        hour=config.hour,
        minute=config.minute,
        tzinfo=tz,
    )

    async def scheduled_generate(context: ContextTypes.DEFAULT_TYPE) -> None:
        """Callback for scheduled digest generation."""
        week_id = Database.current_week_id()
        logger.info("Scheduled generation triggered for %s", week_id)

        # Reuse one status thread for progress while broadcasting final output.
        status_updater = StatusUpdater(context.bot, chat_ids[0])
        try:
            result = await orchestrator.run(week_id, status_updater)
            if result:
                for chat_id in chat_ids:
                    with open(result, "rb") as f:
                        await context.bot.send_document(
                            chat_id=chat_id,
                            document=f,
                            filename=f"digest-{week_id}.md",
                            caption=f"📖 Your weekly digest for {week_id} is ready!",
                        )
            else:
                for chat_id in chat_ids:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"No items collected for {week_id}. Skipping digest.",
                    )
        except Exception as e:
            logger.exception("Scheduled generation failed: %s", e)
            for chat_id in chat_ids:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"❌ Scheduled digest generation failed: {e}",
                )

    # Schedule using python-telegram-bot's JobQueue
    # days is a tuple of integers: (0=Monday, ..., 6=Sunday)
    app.job_queue.run_daily(
        callback=scheduled_generate,
        time=trigger_time,
        days=(config.day_of_week,),
        name="weekly_digest",
    )

    day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    day_name = day_names[config.day_of_week]
    logger.info(
        "Scheduled digest generation: %s at %02d:%02d %s",
        day_name,
        config.hour,
        config.minute,
        config.timezone,
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
