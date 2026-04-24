"""Configuration module for Inbox Agent Bot."""

import json
import logging
import os
from dataclasses import dataclass, field  # noqa: F401
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)


@dataclass
class TelegramConfig:
    bot_token: str
    user_ids: list[int]


@dataclass
class LLMConfig:
    provider: str  # "anthropic" or "openai"
    anthropic_api_key: str
    openai_api_key: str
    collector_model: str
    clusterer_model: str
    researcher_model: str
    writer_model: str
    editor_model: str
    translator_model: str
    profiler_model: str
    filter_model: str


@dataclass
class ObsidianConfig:
    vault_path: Path


@dataclass
class ScheduleConfig:
    enabled: bool = True
    interval_days: int = 2  # digest cadence, e.g. 2 = every 2 days
    day_of_week: int = 6  # legacy; unused when interval_days is set
    hour: int = 23
    minute: int = 0
    timezone: str = "Europe/Berlin"


@dataclass
class TelethonConfig:
    """Credentials for read-only Telegram channel ingestion via Telethon.

    All fields may be empty — if so, channel ingestion is disabled.
    Channels that get polled are restricted to the allowlist stored in the
    telegram_channels table; the Telethon client is never used to read
    anything outside that allowlist.
    """
    api_id: int
    api_hash: str
    session_path: Path


@dataclass
class Config:
    telegram: TelegramConfig
    llm: LLMConfig
    obsidian: ObsidianConfig
    schedule: ScheduleConfig
    user_profile: dict
    db_path: Path
    telethon: TelethonConfig = field(
        default_factory=lambda: TelethonConfig(
            api_id=0, api_hash="", session_path=Path("data/telethon.session")
        )
    )


def get_provider_defaults(provider: str) -> tuple[str, str]:
    """Return (default_fast, default_quality) model names for a provider."""
    if provider == "openai":
        return ("gpt-4.1-mini", "gpt-4.1")
    else:  # anthropic
        return ("claude-sonnet-4-6", "claude-sonnet-4-6")


def _parse_telegram_user_ids() -> list[int]:
    """Parse authorized Telegram user IDs from env.

    Requires TELEGRAM_USER_IDS as a comma-separated list of integers.
    """
    raw_ids = os.getenv("TELEGRAM_USER_IDS", "").strip()
    if not raw_ids:
        raise ValueError("TELEGRAM_USER_IDS is required")

    parts = [p.strip() for p in raw_ids.split(",") if p.strip()]
    try:
        parsed = [int(p) for p in parts]
    except ValueError as exc:
        raise ValueError("TELEGRAM_USER_IDS must contain comma-separated integers") from exc

    # Preserve order while removing duplicates.
    unique_ids: list[int] = []
    for user_id in parsed:
        if user_id not in unique_ids:
            unique_ids.append(user_id)

    return unique_ids


def load_config() -> Config:
    """Load configuration from environment variables and files."""
    load_dotenv()

    project_root = Path(__file__).parent.parent

    # Load user profile as raw dict
    profile_path = project_root / "user_profile.json"
    user_profile = {}
    if profile_path.exists():
        with open(profile_path, "r", encoding="utf-8") as f:
            user_profile = json.load(f)

    # LLM provider
    provider = os.getenv("LLM_PROVIDER", "anthropic").lower()

    # Default models per provider
    default_fast, default_quality = get_provider_defaults(provider)

    # DB path
    db_path = Path(os.getenv("DB_PATH", str(project_root / "data" / "digest.db")))
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Telethon (optional — empty means channel ingestion disabled)
    raw_api_id = os.getenv("TELETHON_API_ID", "").strip()
    try:
        api_id = int(raw_api_id) if raw_api_id else 0
    except ValueError:
        logger.warning("TELETHON_API_ID is not an integer; disabling channel ingestion")
        api_id = 0
    telethon = TelethonConfig(
        api_id=api_id,
        api_hash=os.getenv("TELETHON_API_HASH", "").strip(),
        session_path=Path(
            os.getenv(
                "TELETHON_SESSION_PATH",
                str(project_root / "data" / "telethon.session"),
            )
        ),
    )

    return Config(
        telegram=TelegramConfig(
            bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            user_ids=_parse_telegram_user_ids(),
        ),
        llm=LLMConfig(
            provider=provider,
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
            openai_api_key=os.getenv("OPENAI_API_KEY", ""),
            collector_model=os.getenv("COLLECTOR_MODEL", default_fast),
            clusterer_model=os.getenv("CLUSTERER_MODEL", default_fast),
            researcher_model=os.getenv("RESEARCHER_MODEL", default_fast),
            writer_model=os.getenv("WRITER_MODEL", default_quality),
            editor_model=os.getenv("EDITOR_MODEL", default_quality),
            translator_model=os.getenv("TRANSLATOR_MODEL", default_fast),
            profiler_model=os.getenv("PROFILER_MODEL", default_fast),
            filter_model=os.getenv("FILTER_MODEL", default_fast),
        ),
        obsidian=ObsidianConfig(
            vault_path=Path(os.getenv("OBSIDIAN_VAULT_PATH", "/vault/life/weekly")),
        ),
        schedule=ScheduleConfig(
            enabled=os.getenv("SCHEDULE_ENABLED", "true").lower() == "true",
            interval_days=max(1, int(os.getenv("SCHEDULE_INTERVAL_DAYS", "2"))),
            day_of_week=int(os.getenv("SCHEDULE_DAY", "6")),  # legacy, unused
            hour=int(os.getenv("SCHEDULE_HOUR", "23")),
            minute=int(os.getenv("SCHEDULE_MINUTE", "0")),
            timezone=os.getenv("SCHEDULE_TIMEZONE", "Europe/Berlin"),
        ),
        telethon=telethon,
        user_profile=user_profile,
        db_path=db_path,
    )


async def get_user_profile(db, file_profile: dict) -> dict:
    """Load user profile from DB settings, falling back to file-based profile.

    Priority: DB (set via /setup) > user_profile.json > empty dict.
    """
    try:
        stored = await db.get_setting("user_profile")
        if stored:
            profile = json.loads(stored)
            logger.info("Loaded user profile from database")
            return profile
    except (json.JSONDecodeError, Exception) as e:
        logger.warning("Failed to load profile from DB: %s — using file fallback", e)

    if file_profile:
        logger.info("Using user profile from user_profile.json")
        return file_profile

    logger.warning("No user profile found — using empty profile")
    return {}
