"""Obsidian vault writer for saving digests."""

import logging
from datetime import datetime
from pathlib import Path

from .config import ObsidianConfig

logger = logging.getLogger(__name__)


class ObsidianWriter:
    """Writer for saving markdown files to Obsidian vault."""

    def __init__(self, config: ObsidianConfig):
        self.vault_path = config.vault_path

    def _ensure_directory_exists(self) -> None:
        self.vault_path.mkdir(parents=True, exist_ok=True)

    def _generate_filename(self, date: datetime | None = None) -> str:
        if date is None:
            date = datetime.now()
        year = date.year
        week = date.isocalendar()[1]
        return f"{year}-W{week:02d}.md"

    def save_digest(
        self,
        content: str,
        date: datetime | None = None,
    ) -> Path:
        self._ensure_directory_exists()

        filename = self._generate_filename(date)
        file_path = self.vault_path / filename

        frontmatter = self._generate_frontmatter(date)
        full_content = f"{frontmatter}\n{content}"

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(full_content)

        logger.info("Digest saved to: %s", file_path)
        return file_path

    def _generate_frontmatter(self, date: datetime | None = None) -> str:
        if date is None:
            date = datetime.now()
        iso = date.isocalendar()
        return (
            "---\n"
            f"created: {date.strftime('%Y-%m-%d')}\n"
            f"week: {iso.year}-W{iso.week:02d}\n"
            "type: weekly-digest\n"
            "source: inbox-agent-bot\n"
            "pipeline: multi-agent\n"
            "---\n"
        )

    def digest_exists(self, date: datetime | None = None) -> bool:
        """Check if a digest already exists for the given week.

        Args:
            date: Date to check. Defaults to current date.

        Returns:
            True if digest exists, False otherwise.
        """
        filename = self._generate_filename(date)
        file_path = self.vault_path / filename
        return file_path.exists()

    def get_digest_path(self, date: datetime | None = None) -> Path:
        """Get the path for a digest file.

        Args:
            date: Date for the digest.

        Returns:
            Path to the digest file.
        """
        filename = self._generate_filename(date)
        return self.vault_path / filename
