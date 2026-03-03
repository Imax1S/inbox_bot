"""RSS feed fetcher — polls subscribed feeds and adds new entries to the digest."""

import asyncio
import logging
from datetime import datetime, timezone
from uuid import uuid4

import feedparser

from .agents.collector import CollectorAgent
from .content.url_parser import fetch_and_extract
from .db.database import Database
from .db.models import Item, ItemStatus, ItemType

logger = logging.getLogger(__name__)

# How often to poll feeds (in seconds). 2 hours = 7200 seconds.
RSS_POLL_INTERVAL_SECONDS = 2 * 60 * 60


class RSSFetcher:
    """Fetches and processes RSS feeds."""

    @staticmethod
    async def fetch_feed(url: str) -> tuple[str, list[dict]]:
        """Fetch and parse an RSS feed.

        Runs feedparser in a thread pool since it is synchronous.

        Returns (feed_title, entries) where each entry is a dict with
        keys: id, title, link, summary.
        """
        loop = asyncio.get_event_loop()
        feed = await loop.run_in_executor(None, feedparser.parse, url)

        if feed.bozo and not feed.entries:
            raise ValueError(
                f"Failed to parse feed: {feed.bozo_exception}"
            )

        feed_title = feed.feed.get("title", url)
        entries = []
        for entry in feed.entries:
            entry_id = entry.get("id") or entry.get("link", "")
            entries.append({
                "id": entry_id,
                "title": entry.get("title", ""),
                "link": entry.get("link", ""),
                "summary": entry.get("summary", ""),
            })

        return feed_title, entries

    @staticmethod
    async def poll_all_feeds(
        db: Database,
        collector_agent: CollectorAgent,
        user_id: int,
    ) -> int:
        """Poll all subscribed feeds and add new entries as items.

        Returns the number of new items added.
        """
        feeds = await db.get_rss_feeds()
        if not feeds:
            logger.debug("No RSS feeds configured, skipping poll")
            return 0

        total_new = 0

        for feed_record in feeds:
            feed_id = feed_record["id"]
            feed_url = feed_record["url"]
            last_entry_id = feed_record["last_entry_id"]

            try:
                feed_title, entries = await RSSFetcher.fetch_feed(feed_url)
            except Exception as e:
                logger.warning("Failed to fetch feed %s: %s", feed_url, e)
                continue

            if not entries:
                logger.debug("No entries in feed %s", feed_url)
                continue

            # Process entries newest-first; stop when we hit the last seen entry
            new_entries = []
            for entry in entries:
                if entry["id"] == last_entry_id:
                    break
                new_entries.append(entry)

            if not new_entries:
                logger.debug("No new entries in feed %s", feed_title)
                # Update last_fetched_at even if no new entries
                await db.update_rss_feed_checkpoint(
                    feed_id,
                    datetime.now(timezone.utc).isoformat(),
                    last_entry_id or (entries[0]["id"] if entries else ""),
                )
                continue

            # Process in chronological order (oldest first)
            new_entries.reverse()

            for entry in new_entries:
                entry_link = entry["link"]

                # Skip if this URL is already in the items table
                if entry_link and await db.item_exists_by_source_url(entry_link):
                    logger.debug("Skipping duplicate entry: %s", entry_link)
                    continue

                # Fetch article content
                extracted_text = None
                if entry_link:
                    extracted_text, fetch_err = await fetch_and_extract(entry_link)
                    if fetch_err:
                        logger.warning(
                            "RSS article fetch issue for %s: %s",
                            entry_link, fetch_err,
                        )

                # Build raw content from entry metadata
                raw_parts = []
                if entry["title"]:
                    raw_parts.append(entry["title"])
                if entry_link:
                    raw_parts.append(entry_link)
                raw_content = "\n".join(raw_parts) if raw_parts else entry["summary"]

                # Run collector agent
                try:
                    result = await collector_agent.process(
                        raw_content=raw_content,
                        extracted_text=extracted_text,
                        item_type=ItemType.ARTICLE,
                    )
                except Exception as e:
                    logger.warning("Collector failed for RSS entry %s: %s", entry_link, e)
                    result = None

                # Save item
                week_id = Database.current_week_id()
                item = Item(
                    id=str(uuid4()),
                    created_at=datetime.now(),
                    type=ItemType.ARTICLE,
                    raw_content=raw_content,
                    source_url=entry_link or None,
                    extracted_text=extracted_text,
                    summary=result.summary if result else raw_content[:200],
                    tags=result.tags if result else [],
                    language=result.language if result else "en",
                    week_id=week_id,
                    status=ItemStatus.COLLECTED,
                )
                await db.save_item(item)
                total_new += 1
                logger.info(
                    "Added RSS item: [%s] %s",
                    feed_title, entry["title"],
                )

            # Update checkpoint to the newest entry we saw
            newest_entry_id = entries[0]["id"] if entries else last_entry_id
            await db.update_rss_feed_checkpoint(
                feed_id,
                datetime.now(timezone.utc).isoformat(),
                newest_entry_id or "",
            )
            logger.info(
                "Polled feed %s: %d new items", feed_title, len(new_entries),
            )

        logger.info("RSS poll complete: %d new items total", total_new)
        return total_new
