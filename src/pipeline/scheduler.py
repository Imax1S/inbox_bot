"""Weekly digest scheduler — auto-triggers pipeline on configured day/time."""

import datetime
import logging

from telegram.ext import Application, ContextTypes

from ..config import ScheduleConfig
from ..db.database import Database
from ..pipeline.orchestrator import Orchestrator
from ..pipeline.status_updater import StatusUpdater

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
