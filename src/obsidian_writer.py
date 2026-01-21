"""Obsidian vault writer for saving digests."""

from datetime import datetime
from pathlib import Path

from .config import ObsidianConfig


class ObsidianWriter:
    """Writer for saving markdown files to Obsidian vault."""

    def __init__(self, config: ObsidianConfig):
        """Initialize the Obsidian writer.

        Args:
            config: Obsidian vault configuration.
        """
        self.vault_path = config.vault_path

    def _ensure_directory_exists(self) -> None:
        """Ensure the vault directory exists."""
        self.vault_path.mkdir(parents=True, exist_ok=True)

    def _generate_filename(self, date: datetime | None = None) -> str:
        """Generate filename in YYYY-WXX format.

        Args:
            date: Date to use for filename. Defaults to current date.

        Returns:
            Filename string.
        """
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
        """Save digest to Obsidian vault.

        Args:
            content: Markdown content to save.
            date: Date for filename generation. Defaults to current date.

        Returns:
            Path to the saved file.
        """
        self._ensure_directory_exists()

        filename = self._generate_filename(date)
        file_path = self.vault_path / filename

        # Add frontmatter
        frontmatter = self._generate_frontmatter(date)
        full_content = f"{frontmatter}\n{content}"

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(full_content)

        print(f"Digest saved to: {file_path}")
        return file_path

    def _generate_frontmatter(self, date: datetime | None = None) -> str:
        """Generate YAML frontmatter for the note.

        Args:
            date: Date for the frontmatter.

        Returns:
            YAML frontmatter string.
        """
        if date is None:
            date = datetime.now()

        return f"""---
created: {date.strftime("%Y-%m-%d")}
type: weekly-digest
source: inbox-agent-bot
---
"""

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
