"""Main entry point for the Weekly Digest Bot."""

import asyncio
import logging
import sys

from .agents.clusterer import ClustererAgent
from .agents.collector import CollectorAgent
from .agents.editor import EditorAgent
from .agents.filter import FilterAgent
from .agents.profiler import ProfilerAgent
from .agents.researcher import ResearcherAgent
from .agents.translator import TranslatorAgent
from .agents.writer import WriterAgent
from .config import get_provider_defaults, get_user_profile, load_config
from .db.database import Database
from .llm.provider import create_provider
from .obsidian_writer import ObsidianWriter
from .pipeline.orchestrator import Orchestrator
from .pipeline.scheduler import setup_rss_schedule, setup_schedule
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

    # Create Obsidian writer
    obsidian = ObsidianWriter(config.obsidian)

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
    )

    # Create bot
    bot = DigestBot(
        config=config,
        db=db,
        collector=collector,
        orchestrator=orchestrator,
        profiler=profiler,
    )

    # Build the application
    app = bot.build()

    # Set up scheduled generation
    setup_schedule(
        app=app,
        config=config.schedule,
        orchestrator=orchestrator,
        chat_ids=config.telegram.user_ids,
    )

    # Set up RSS feed polling
    setup_rss_schedule(
        app=app,
        db=db,
        collector=collector,
        chat_ids=config.telegram.user_ids,
    )

    # Run
    logger.info("Starting bot...")
    print("Send messages to collect, /generate to create digest.")
    print("=" * 50)
    app.run_polling()


if __name__ == "__main__":
    main()
