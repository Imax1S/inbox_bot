"""RSS feed fetcher — polls subscribed feeds and adds new entries as items."""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

import feedparser

from .agents.collector import CollectorAgent
from .content.url_parser import fetch_and_extract
from .db.database import Database
from .db.models import Item, ItemStatus, ItemType

logger = logging.getLogger(__name__)


@dataclass
class PollResult:
    """Result of polling all RSS feeds."""

    total_added: int = 0
    items: list[dict] = field(default_factory=list)  # {feed_title, title, link}

# How often to poll feeds (in seconds): every 2 hours
RSS_POLL_INTERVAL_SECONDS = 2 * 60 * 60


class RSSFetcher:
    """Fetches and processes RSS feeds, adding new entries to the digest pipeline."""

    async def fetch_feed(self, url: str) -> tuple[str, list[dict]]:
        """Fetch and parse an RSS feed.

        Runs feedparser in a thread pool since it performs synchronous I/O.

        Returns (feed_title, entries) where each entry is a dict with
        keys: title, link, id (guid or link).
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
                "title": entry.get("title", ""),
                "link": entry.get("link", ""),
                "id": entry_id,
            })

        return feed_title, entries

    async def poll_all_feeds(
        self,
        db: Database,
        collector_agent: CollectorAgent,
        user_id: int,
    ) -> PollResult:
        """Poll all subscribed feeds and add new entries as items.

        Returns a PollResult with count and details of added items.
        """
        feeds = await db.get_rss_feeds()
        if not feeds:
            logger.debug("No RSS feeds configured, skipping poll")
            return PollResult()

        poll_result = PollResult()

        for feed_record in feeds:
            feed_id = feed_record["id"]
            feed_url = feed_record["url"]
            last_entry_id = feed_record["last_entry_id"]

            try:
                feed_title, entries = await self.fetch_feed(feed_url)
            except Exception as e:
                logger.warning(
                    "Failed to fetch RSS feed %s (%s): %s",
                    feed_record["title"] or feed_url,
                    feed_url,
                    e,
                )
                continue

            if not entries:
                logger.debug("No entries in feed %s", feed_url)
                continue

            # Find new entries: stop at the last seen entry
            new_entries = []
            for entry in entries:
                if entry["id"] == last_entry_id:
                    break
                new_entries.append(entry)

            if not new_entries:
                logger.debug(
                    "No new entries in feed %s (last_entry_id=%s)",
                    feed_url,
                    last_entry_id,
                )
                # Still update last_fetched_at
                await db.update_rss_feed_checkpoint(
                    feed_id,
                    last_fetched_at=datetime.now(timezone.utc).isoformat(),
                    last_entry_id=last_entry_id or "",
                )
                continue

            # Process new entries (oldest first for chronological order)
            added_count = 0
            for entry in reversed(new_entries):
                entry_link = entry["link"]
                if not entry_link:
                    continue

                # Skip if this URL is already in the items table
                if await db.item_exists_by_source_url(entry_link):
                    logger.debug(
                        "Skipping duplicate RSS entry: %s", entry_link
                    )
                    continue

                # Fetch article content
                extracted_text, fetch_error = await fetch_and_extract(entry_link)
                if fetch_error:
                    logger.warning(
                        "RSS fetch issue for %s: %s", entry_link, fetch_error
                    )

                # Build raw_content from entry title + link
                raw_content = entry["title"]
                if entry_link:
                    raw_content = f"{raw_content}\n{entry_link}" if raw_content else entry_link

                # Run collector agent
                try:
                    result = await collector_agent.process(
                        raw_content=raw_content,
                        extracted_text=extracted_text,
                        item_type=ItemType.ARTICLE,
                    )
                except Exception as e:
                    logger.error(
                        "Collector agent failed for RSS entry %s: %s",
                        entry_link,
                        e,
                    )
                    result = None

                # Save item
                week_id = Database.current_week_id()
                item = Item(
                    id=str(uuid4()),
                    created_at=datetime.now(),
                    type=ItemType.ARTICLE,
                    raw_content=raw_content,
                    source_url=entry_link,
                    extracted_text=extracted_text,
                    summary=result.summary if result else raw_content[:200],
                    tags=result.tags if result else [],
                    language=result.language if result else "en",
                    week_id=week_id,
                    status=ItemStatus.COLLECTED,
                )
                await db.save_item(item)
                added_count += 1
                poll_result.items.append({
                    "feed_title": feed_record["title"] or feed_url,
                    "title": entry["title"],
                    "link": entry_link,
                })

                logger.info(
                    "Added RSS item: %s from %s",
                    entry["title"][:60],
                    feed_record["title"] or feed_url,
                )

            # Update checkpoint to the newest entry
            newest_entry_id = entries[0]["id"] if entries else last_entry_id or ""
            await db.update_rss_feed_checkpoint(
                feed_id,
                last_fetched_at=datetime.now(timezone.utc).isoformat(),
                last_entry_id=newest_entry_id,
            )

            poll_result.total_added += added_count
            logger.info(
                "RSS feed %s: added %d new item(s)",
                feed_record["title"] or feed_url,
                added_count,
            )

        return poll_result
