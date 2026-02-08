"""Configuration module for Inbox Agent Bot."""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


@dataclass
class TelegramConfig:
    bot_token: str
    user_id: int


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


@dataclass
class ObsidianConfig:
    vault_path: Path


@dataclass
class ScheduleConfig:
    enabled: bool
    day_of_week: int  # 0=Monday, 6=Sunday
    hour: int
    minute: int
    timezone: str


@dataclass
class Config:
    telegram: TelegramConfig
    llm: LLMConfig
    obsidian: ObsidianConfig
    schedule: ScheduleConfig
    user_profile: dict
    db_path: Path


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
    if provider == "openai":
        default_fast = "gpt-4o"
        default_quality = "gpt-4o"
    else:
        default_fast = "claude-sonnet-4-5-20250929"
        default_quality = "claude-opus-4-6"

    # DB path
    db_path = Path(os.getenv("DB_PATH", str(project_root / "data" / "digest.db")))
    db_path.parent.mkdir(parents=True, exist_ok=True)

    return Config(
        telegram=TelegramConfig(
            bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            user_id=int(os.getenv("TELEGRAM_USER_ID", "0")),
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
        ),
        obsidian=ObsidianConfig(
            vault_path=Path(os.getenv("OBSIDIAN_VAULT_PATH", "/vault/life/weekly")),
        ),
        schedule=ScheduleConfig(
            enabled=os.getenv("SCHEDULE_ENABLED", "true").lower() == "true",
            day_of_week=int(os.getenv("SCHEDULE_DAY", "6")),  # Sunday
            hour=int(os.getenv("SCHEDULE_HOUR", "23")),
            minute=int(os.getenv("SCHEDULE_MINUTE", "0")),
            timezone=os.getenv("SCHEDULE_TIMEZONE", "Europe/Berlin"),
        ),
        user_profile=user_profile,
        db_path=db_path,
    )
