"""RSS feed fetcher — polls feeds and adds new entries to the digest pipeline."""

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

# Poll interval in seconds (2 hours)
RSS_POLL_INTERVAL_SECONDS = 2 * 60 * 60


async def fetch_feed(url: str) -> tuple[str, list[dict]]:
    """Fetch and parse an RSS feed.

    Runs feedparser in a thread pool since it performs synchronous I/O.

    Returns (feed_title, entries) where each entry is a dict with keys:
        id, title, link, published.
    """
    loop = asyncio.get_event_loop()
    feed = await loop.run_in_executor(None, feedparser.parse, url)

    title = feed.feed.get("title", "") if feed.feed else ""

    entries = []
    for entry in feed.entries:
        entry_id = entry.get("id") or entry.get("link", "")
        entries.append({
            "id": entry_id,
            "title": entry.get("title", ""),
            "link": entry.get("link", ""),
            "published": entry.get("published", ""),
        })

    return title, entries


async def poll_all_feeds(
    db: Database,
    collector_agent: CollectorAgent,
    user_id: int,
) -> int:
    """Poll all subscribed RSS feeds and add new entries as items.

    Returns the number of new items added.
    """
    feeds = await db.get_rss_feeds()
    if not feeds:
        logger.debug("No RSS feeds to poll")
        return 0

    total_new = 0
    for feed_record in feeds:
        try:
            new_count = await _poll_single_feed(db, collector_agent, feed_record)
            total_new += new_count
        except Exception:
            logger.exception(
                "Error polling RSS feed %s (%s)",
                feed_record["title"],
                feed_record["url"],
            )

    if total_new > 0:
        logger.info("RSS polling complete: %d new items added", total_new)
    return total_new


async def _poll_single_feed(
    db: Database,
    collector_agent: CollectorAgent,
    feed_record: dict,
) -> int:
    """Poll a single RSS feed and add new entries. Returns count of new items."""
    feed_url = feed_record["url"]
    feed_id = feed_record["id"]
    last_entry_id = feed_record.get("last_entry_id")

    logger.debug("Polling RSS feed: %s (%s)", feed_record["title"], feed_url)

    title, entries = await fetch_feed(feed_url)
    if not entries:
        logger.debug("No entries found in feed: %s", feed_url)
        return 0

    new_count = 0
    latest_entry_id = None

    for entry in entries:
        entry_id = entry["id"]
        entry_link = entry["link"]

        # Stop at the last seen entry
        if entry_id == last_entry_id:
            break

        # Skip entries we've already saved (by source_url)
        if entry_link and await db.item_exists_by_source_url(entry_link):
            logger.debug("Skipping already-saved entry: %s", entry_link)
            continue

        # Track the newest entry id for checkpoint
        if latest_entry_id is None:
            latest_entry_id = entry_id

        # Fetch and extract article content
        extracted_text = None
        if entry_link:
            text, err = await fetch_and_extract(entry_link)
            if err:
                logger.warning(
                    "Failed to extract content from %s: %s", entry_link, err
                )
            extracted_text = text

        # Build raw content from entry metadata
        raw_content = entry.get("title", "")
        if entry_link:
            raw_content = f"{raw_content}\n{entry_link}" if raw_content else entry_link

        # Classify and summarize via CollectorAgent
        try:
            result = await collector_agent.process(
                raw_content=raw_content,
                extracted_text=extracted_text,
                item_type=ItemType.ARTICLE,
            )
        except Exception:
            logger.exception("Collector failed for RSS entry: %s", entry_link)
            continue

        # Save item to DB
        item = Item(
            id=str(uuid4()),
            created_at=datetime.now(),
            type=ItemType.ARTICLE,
            raw_content=raw_content,
            source_url=entry_link or None,
            extracted_text=extracted_text,
            summary=result.summary,
            tags=result.tags,
            language=result.language,
            week_id=Database.current_week_id(),
            status=ItemStatus.COLLECTED,
        )
        await db.save_item(item)
        new_count += 1
        logger.info(
            "Added RSS item: %s [%s]",
            entry.get("title", "untitled"),
            feed_record["title"],
        )

    # Update checkpoint to the newest entry we've seen
    checkpoint_id = latest_entry_id or last_entry_id or (entries[0]["id"] if entries else None)
    if checkpoint_id:
        await db.update_rss_feed_checkpoint(
            feed_id=feed_id,
            last_fetched_at=datetime.now(timezone.utc).isoformat(),
            last_entry_id=checkpoint_id,
        )

    return new_count
