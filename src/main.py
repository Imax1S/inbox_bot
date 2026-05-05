"""Main entry point for the Weekly Digest Bot."""

import asyncio
import logging
import sys
import tempfile
from pathlib import Path

from .agents.clusterer import ClustererAgent
from .agents.collector import CollectorAgent
from .agents.editor import EditorAgent
from .agents.filter import FilterAgent
from .agents.profiler import ProfilerAgent
from .agents.researcher import ResearcherAgent
from .agents.translator import TranslatorAgent
from .agents.writer import WriterAgent
from .config import ObsidianConfig, get_provider_defaults, get_user_profile, load_config
from .db.database import Database
from .llm.dry_run import DryRunLLMProvider
from .llm.provider import create_provider
from .obsidian_writer import ObsidianWriter
from .pipeline.orchestrator import Orchestrator
from .pipeline.scheduler import (
    setup_channel_schedule,
    setup_rss_schedule,
    setup_schedule,
)
from .telegram.channel_reader import ChannelReader
from .telegraph_writer import TelegraphWriter
from .smoke_test import (
    SmokeClustererAgent,
    SmokeEditorAgent,
    SmokeFilterAgent,
    SmokeResearcherAgent,
    SmokeTranslatorAgent,
    SmokeWriterAgent,
    get_smoke_models,
)
from .telegram.bot import DigestBot


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


async def init_db_and_settings(db: Database, config, file_profile: dict) -> tuple[str, dict]:
    """Initialize DB, resolve provider from settings, load user profile.

    Returns (provider_name, user_profile).
    """
    await db.init()

    # Resolve provider from saved settings
    saved_provider = await db.get_setting("llm_provider")
    if saved_provider and saved_provider != config.llm.provider:
        logger.info(
            "Overriding provider from DB setting: %s -> %s",
            config.llm.provider,
            saved_provider,
        )
        config.llm.provider = saved_provider
        default_fast, default_quality = get_provider_defaults(saved_provider)
        config.llm.collector_model = default_fast
        config.llm.clusterer_model = default_fast
        config.llm.researcher_model = default_fast
        config.llm.writer_model = default_quality
        config.llm.editor_model = default_quality
        config.llm.translator_model = default_fast

    # Load user profile (DB > file fallback)
    user_profile = await get_user_profile(db, file_profile)

    # Cadence override from DB (set via /cadence)
    stored_cadence = await db.get_setting("schedule_interval_days")
    if stored_cadence:
        try:
            config.schedule.interval_days = max(1, int(stored_cadence))
            logger.info(
                "Using cadence from DB: every %d day(s)", config.schedule.interval_days
            )
        except ValueError:
            logger.warning("Invalid schedule_interval_days in DB: %r", stored_cadence)

    return config.llm.provider, user_profile


def main() -> None:
    print("=" * 50)
    print("Weekly Digest Bot")
    print("=" * 50)

    # Load config
    config = load_config()
    logger.info("Vault: %s", config.obsidian.vault_path)
    logger.info("DB: %s", config.db_path)

    # Initialize database, resolve provider, load user profile
    db = Database(config.db_path)
    provider, user_profile = asyncio.get_event_loop().run_until_complete(
        init_db_and_settings(db, config, config.user_profile)
    )
    logger.info("Provider: %s", provider)
    logger.info("Database initialized")
    logger.info(
        "User profile loaded: %d interest areas",
        len(user_profile.get("interest_areas", [])),
    )

    # Create LLM provider
    if config.llm.provider in ("anthropic", "claude"):
        llm = create_provider("anthropic", config.llm.anthropic_api_key)
    elif config.llm.provider == "openai":
        llm = create_provider("openai", config.llm.openai_api_key)
    else:
        raise ValueError(f"Unknown LLM provider: {config.llm.provider}")

    # Create agents
    collector = CollectorAgent(
        llm, config.llm.collector_model, db, user_profile
    )
    clusterer = ClustererAgent(
        llm, config.llm.clusterer_model, db, user_profile
    )
    researcher = ResearcherAgent(
        llm, config.llm.researcher_model, db, user_profile
    )
    writer = WriterAgent(
        llm, config.llm.writer_model, db, user_profile
    )
    editor = EditorAgent(
        llm, config.llm.editor_model, db, user_profile
    )
    translator = TranslatorAgent(
        llm, config.llm.translator_model, db, user_profile
    )
    profiler = ProfilerAgent(
        llm, config.llm.profiler_model, db
    )
    filter_agent = FilterAgent(
        llm, config.llm.filter_model, db, user_profile
    )

    # Create Obsidian writer + Telegraph publisher
    obsidian = ObsidianWriter(config.obsidian)
    telegraph_writer = TelegraphWriter()

    # Create orchestrator
    orchestrator = Orchestrator(
        db=db,
        clusterer=clusterer,
        researcher=researcher,
        writer=writer,
        editor=editor,
        translator=translator,
        obsidian_writer=obsidian,
        filter_agent=filter_agent,
        telegraph_writer=telegraph_writer,
    )

    # Create dry-run orchestrator (uses mock LLM, saves to temp dir)
    dry_run_llm = DryRunLLMProvider()
    dry_run_dir = Path(tempfile.gettempdir()) / "inbox_bot_dryrun"
    dry_run_obsidian = ObsidianWriter(ObsidianConfig(vault_path=dry_run_dir))
    dry_run_orchestrator = Orchestrator(
        db=db,
        clusterer=ClustererAgent(dry_run_llm, config.llm.clusterer_model, db, user_profile),
        researcher=ResearcherAgent(dry_run_llm, config.llm.researcher_model, db, user_profile),
        writer=WriterAgent(dry_run_llm, config.llm.writer_model, db, user_profile),
        editor=EditorAgent(dry_run_llm, config.llm.editor_model, db, user_profile),
        translator=TranslatorAgent(dry_run_llm, config.llm.translator_model, db, user_profile),
        obsidian_writer=dry_run_obsidian,
        filter_agent=FilterAgent(dry_run_llm, config.llm.filter_model, db, user_profile),
        dry_run=True,
    )

    smoke_fast_model, smoke_quality_model = get_smoke_models(config.llm.provider)
    smoke_dir = Path(tempfile.gettempdir()) / "inbox_bot_smoketest"
    smoke_obsidian = ObsidianWriter(ObsidianConfig(vault_path=smoke_dir))
    smoke_test_orchestrator = Orchestrator(
        db=db,
        clusterer=SmokeClustererAgent(llm, smoke_fast_model, db, user_profile),
        researcher=SmokeResearcherAgent(llm, smoke_quality_model, db, user_profile),
        writer=SmokeWriterAgent(llm, smoke_quality_model, db, user_profile),
        editor=SmokeEditorAgent(llm, smoke_quality_model, db, user_profile),
        translator=SmokeTranslatorAgent(llm, smoke_fast_model, db, user_profile),
        obsidian_writer=smoke_obsidian,
        filter_agent=SmokeFilterAgent(llm, smoke_fast_model, db, user_profile),
        dry_run=True,
    )

    # Create bot
    bot = DigestBot(
        config=config,
        db=db,
        collector=collector,
        orchestrator=orchestrator,
        profiler=profiler,
        dry_run_orchestrator=dry_run_orchestrator,
        smoke_test_orchestrator=smoke_test_orchestrator,
    )

    # Build the application
    app = bot.build()

    # Set up scheduled generation
    setup_schedule(
        app=app,
        config=config.schedule,
        orchestrator=orchestrator,
        chat_ids=config.telegram.user_ids,
        digest_bot=bot,
    )

    # Set up RSS feed polling
    setup_rss_schedule(
        app=app,
        db=db,
        collector=collector,
        chat_ids=config.telegram.user_ids,
    )

    # Set up Telegram channel polling (disabled unless Telethon env is configured)
    if config.telethon.api_id > 0 and config.telethon.api_hash:
        channel_reader = ChannelReader(
            api_id=config.telethon.api_id,
            api_hash=config.telethon.api_hash,
            session_path=config.telethon.session_path,
        )
        bot.channel_reader = channel_reader
        setup_channel_schedule(
            app=app,
            db=db,
            collector=collector,
            channel_reader=channel_reader,
            chat_ids=config.telegram.user_ids,
        )
    else:
        logger.info(
            "Telegram channel ingestion disabled: "
            "TELETHON_API_ID/HASH not set in .env"
        )

    # Run
    logger.info("Starting bot...")
    print("Send messages to collect, /generate to create digest.")
    print("=" * 50)
    app.run_polling()


if __name__ == "__main__":
    main()
