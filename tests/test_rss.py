"""Tests for RSS feed functionality — database CRUD and feed fetching."""

from unittest.mock import MagicMock, patch

import pytest

from src.db.database import Database
from src.rss_fetcher import fetch_feed


# ── Database CRUD ──


@pytest.mark.asyncio
async def test_add_rss_feed(mock_db):
    feed_id = await mock_db.add_rss_feed("https://example.com/feed.xml", "Example Blog")
    assert feed_id is not None
    assert feed_id > 0


@pytest.mark.asyncio
async def test_get_rss_feeds(mock_db):
    await mock_db.add_rss_feed("https://example.com/feed1.xml", "Feed One")
    await mock_db.add_rss_feed("https://example.com/feed2.xml", "Feed Two")

    feeds = await mock_db.get_rss_feeds()
    assert len(feeds) == 2
    assert feeds[0]["title"] == "Feed One"
    assert feeds[0]["url"] == "https://example.com/feed1.xml"
    assert feeds[1]["title"] == "Feed Two"
    assert feeds[1]["url"] == "https://example.com/feed2.xml"


@pytest.mark.asyncio
async def test_get_rss_feeds_empty(mock_db):
    feeds = await mock_db.get_rss_feeds()
    assert feeds == []


@pytest.mark.asyncio
async def test_remove_rss_feed(mock_db):
    feed_id = await mock_db.add_rss_feed("https://example.com/feed.xml", "Test Feed")
    removed = await mock_db.remove_rss_feed(feed_id)
    assert removed is True

    feeds = await mock_db.get_rss_feeds()
    assert len(feeds) == 0


@pytest.mark.asyncio
async def test_remove_rss_feed_nonexistent(mock_db):
    removed = await mock_db.remove_rss_feed(999)
    assert removed is False


@pytest.mark.asyncio
async def test_update_rss_feed_checkpoint(mock_db):
    feed_id = await mock_db.add_rss_feed("https://example.com/feed.xml", "Test Feed")
    await mock_db.update_rss_feed_checkpoint(
        feed_id=feed_id,
        last_fetched_at="2026-03-03T12:00:00+00:00",
        last_entry_id="entry-42",
    )

    feeds = await mock_db.get_rss_feeds()
    assert len(feeds) == 1
    assert feeds[0]["last_fetched_at"] == "2026-03-03T12:00:00+00:00"
    assert feeds[0]["last_entry_id"] == "entry-42"


@pytest.mark.asyncio
async def test_add_duplicate_rss_feed(mock_db):
    await mock_db.add_rss_feed("https://example.com/feed.xml", "Feed One")
    with pytest.raises(Exception):
        await mock_db.add_rss_feed("https://example.com/feed.xml", "Feed Duplicate")


@pytest.mark.asyncio
async def test_item_exists_by_source_url(mock_db):
    # No items yet
    exists = await mock_db.item_exists_by_source_url("https://example.com/article")
    assert exists is False


# ── fetch_feed (mocked feedparser) ──


@pytest.mark.asyncio
async def test_fetch_feed_success():
    mock_feed = MagicMock()
    mock_feed.feed.get = lambda key, default="": {
        "title": "Test Blog",
    }.get(key, default)

    mock_entry = MagicMock()
    mock_entry.get = lambda key, default="": {
        "id": "entry-1",
        "title": "First Post",
        "link": "https://example.com/post-1",
        "published": "2026-03-01",
    }.get(key, default)
    mock_feed.entries = [mock_entry]

    with patch("src.rss_fetcher.feedparser.parse", return_value=mock_feed):
        title, entries = await fetch_feed("https://example.com/feed.xml")

    assert title == "Test Blog"
    assert len(entries) == 1
    assert entries[0]["id"] == "entry-1"
    assert entries[0]["title"] == "First Post"
    assert entries[0]["link"] == "https://example.com/post-1"


@pytest.mark.asyncio
async def test_fetch_feed_empty():
    mock_feed = MagicMock()
    mock_feed.feed.get = lambda key, default="": default
    mock_feed.entries = []

    with patch("src.rss_fetcher.feedparser.parse", return_value=mock_feed):
        title, entries = await fetch_feed("https://example.com/empty-feed.xml")

    assert title == ""
    assert entries == []
