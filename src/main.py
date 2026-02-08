"""Main entry point for the Weekly Digest Bot."""

import asyncio
import logging
import sys

from .agents.clusterer import ClustererAgent
from .agents.collector import CollectorAgent
from .agents.editor import EditorAgent
from .agents.researcher import ResearcherAgent
from .agents.writer import WriterAgent
from .config import load_config
from .db.database import Database
from .llm.provider import create_provider
from .obsidian_writer import ObsidianWriter
from .pipeline.orchestrator import Orchestrator
from .pipeline.scheduler import setup_schedule
from .telegram.bot import DigestBot


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


async def init_db(db: Database) -> None:
    await db.init()


def main() -> None:
    print("=" * 50)
    print("Weekly Digest Bot")
    print("=" * 50)

    # Load config
    config = load_config()
    logger.info("Provider: %s", config.llm.provider)
    logger.info("Vault: %s", config.obsidian.vault_path)
    logger.info("DB: %s", config.db_path)

    # Initialize database
    db = Database(config.db_path)
    asyncio.get_event_loop().run_until_complete(init_db(db))
    logger.info("Database initialized")

    # Create LLM provider
    if config.llm.provider in ("anthropic", "claude"):
        llm = create_provider("anthropic", config.llm.anthropic_api_key)
    elif config.llm.provider == "openai":
        llm = create_provider("openai", config.llm.openai_api_key)
    else:
        raise ValueError(f"Unknown LLM provider: {config.llm.provider}")

    # Create agents
    collector = CollectorAgent(
        llm, config.llm.collector_model, db, config.user_profile
    )
    clusterer = ClustererAgent(
        llm, config.llm.clusterer_model, db, config.user_profile
    )
    researcher = ResearcherAgent(
        llm, config.llm.researcher_model, db, config.user_profile
    )
    writer = WriterAgent(
        llm, config.llm.writer_model, db, config.user_profile
    )
    editor = EditorAgent(
        llm, config.llm.editor_model, db, config.user_profile
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
        obsidian_writer=obsidian,
    )

    # Create bot
    bot = DigestBot(
        config=config,
        db=db,
        collector=collector,
        orchestrator=orchestrator,
    )

    # Build the application
    app = bot.build()

    # Set up scheduled generation
    setup_schedule(
        app=app,
        config=config.schedule,
        orchestrator=orchestrator,
        chat_id=config.telegram.user_id,
    )

    # Run
    logger.info("Starting bot...")
    print("Send messages to collect, /generate to create digest.")
    print("=" * 50)
    app.run_polling()


if __name__ == "__main__":
    main()
