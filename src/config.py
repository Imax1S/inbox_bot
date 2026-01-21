"""Configuration module for Inbox Agent Bot."""

import json
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass
class TelegramConfig:
    """Telegram Bot API configuration."""
    bot_token: str
    user_id: int  # Your Telegram user ID (for access control)


@dataclass
class LLMConfig:
    """LLM configuration."""
    provider: str  # "claude" or "openai"
    api_key: str
    model: str | None = None  # Optional model override


@dataclass
class ObsidianConfig:
    """Obsidian vault configuration."""
    vault_path: Path


@dataclass
class UserProfile:
    """User profile data from user_profile.json."""
    name: str
    interests: list[str]
    goals: list[str]
    preferred_language: str
    note_style: str


@dataclass
class Config:
    """Main configuration container."""
    telegram: TelegramConfig
    llm: LLMConfig
    obsidian: ObsidianConfig
    user_profile: UserProfile


def load_user_profile(profile_path: Path) -> UserProfile:
    """Load user profile from JSON file."""
    with open(profile_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    return UserProfile(
        name=data.get("name", "User"),
        interests=data.get("interests", []),
        goals=data.get("goals", []),
        preferred_language=data.get("preferred_language", "en"),
        note_style=data.get("note_style", "concise"),
    )


def load_config() -> Config:
    """Load configuration from environment and files."""
    # Load .env file
    load_dotenv()

    # Determine project root
    project_root = Path(__file__).parent.parent

    # Load user profile
    profile_path = project_root / "user_profile.json"
    user_profile = load_user_profile(profile_path)

    # Determine LLM provider and API key
    llm_provider = os.getenv("LLM_PROVIDER", "claude").lower()

    # Get API key based on provider
    if llm_provider == "openai":
        api_key = os.getenv("OPENAI_API_KEY", "")
    else:  # Default to claude
        api_key = os.getenv("ANTHROPIC_API_KEY", "")

    # Build config
    return Config(
        telegram=TelegramConfig(
            bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            user_id=int(os.getenv("TELEGRAM_USER_ID", "0")),
        ),
        llm=LLMConfig(
            provider=llm_provider,
            api_key=api_key,
            model=os.getenv("LLM_MODEL"),  # Optional model override
        ),
        obsidian=ObsidianConfig(
            vault_path=Path(os.getenv("OBSIDIAN_VAULT_PATH", "/vault/life/weekly")),
        ),
        user_profile=user_profile,
    )
