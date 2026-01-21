"""Main entry point for Inbox Agent Bot."""

from pathlib import Path

from .config import load_config
from .llm_agent import DigestAgent
from .obsidian_writer import ObsidianWriter
from .telegram_client import TelegramBotClient, Message


def main() -> None:
    """Main entry point - run the Telegram bot."""
    print("=" * 50)
    print("Inbox Agent Bot")
    print("=" * 50)

    # Load configuration
    print("\nLoading configuration...")
    config = load_config()
    print(f"  User: {config.user_profile.name}")
    print(f"  Language: {config.user_profile.preferred_language}")
    print(f"  Vault: {config.obsidian.vault_path}")

    # Initialize components
    agent = DigestAgent(config.llm, config.user_profile)
    writer = ObsidianWriter(config.obsidian)

    async def generate_digest(messages: list[Message]) -> Path:
        """Generate and save digest from collected messages."""
        print(f"\nGenerating digest from {len(messages)} messages...")
        digest = await agent.generate_digest(messages)
        file_path = writer.save_digest(digest)
        print(f"Digest saved to: {file_path}")
        return file_path

    # Create and run bot
    bot = TelegramBotClient(config.telegram, on_digest_request=generate_digest)

    print("\nStarting bot...")
    print("Send messages to collect them, then use /digest to generate summary.")
    print("=" * 50)

    bot.run()


if __name__ == "__main__":
    main()
