"""Tests for RSS feed functionality — database CRUD and RSSFetcher."""

from unittest.mock import patch, MagicMock

import pytest

from src.db.database import Database
from src.db.models import ItemType
from src.rss_fetcher import RSSFetcher
from tests.conftest import MockLLMProvider


# ── Database CRUD ──


@pytest.mark.asyncio
async def test_add_rss_feed(mock_db):
    feed_id = await mock_db.add_rss_feed("https://example.com/feed.xml", "Example Feed")
    assert feed_id is not None
    assert feed_id > 0


@pytest.mark.asyncio
async def test_get_rss_feeds_empty(mock_db):
    feeds = await mock_db.get_rss_feeds()
    assert feeds == []


@pytest.mark.asyncio
async def test_get_rss_feeds(mock_db):
    await mock_db.add_rss_feed("https://example.com/feed1.xml", "Feed One")
    await mock_db.add_rss_feed("https://example.com/feed2.xml", "Feed Two")

    feeds = await mock_db.get_rss_feeds()
    assert len(feeds) == 2
    assert feeds[0]["url"] == "https://example.com/feed1.xml"
    assert feeds[0]["title"] == "Feed One"
    assert feeds[1]["url"] == "https://example.com/feed2.xml"
    assert feeds[1]["title"] == "Feed Two"


@pytest.mark.asyncio
async def test_add_duplicate_rss_feed(mock_db):
    await mock_db.add_rss_feed("https://example.com/feed.xml", "Feed")
    with pytest.raises(Exception):  # UNIQUE constraint violation
        await mock_db.add_rss_feed("https://example.com/feed.xml", "Feed Duplicate")


@pytest.mark.asyncio
async def test_remove_rss_feed(mock_db):
    feed_id = await mock_db.add_rss_feed("https://example.com/feed.xml", "Feed")

    result = await mock_db.remove_rss_feed(feed_id)
    assert result is True

    feeds = await mock_db.get_rss_feeds()
    assert len(feeds) == 0


@pytest.mark.asyncio
async def test_remove_nonexistent_feed(mock_db):
    result = await mock_db.remove_rss_feed(999)
    assert result is False


@pytest.mark.asyncio
async def test_update_rss_feed_checkpoint(mock_db):
    feed_id = await mock_db.add_rss_feed("https://example.com/feed.xml", "Feed")

    await mock_db.update_rss_feed_checkpoint(
        feed_id, "2026-03-03T12:00:00+00:00", "entry-123"
    )

    feeds = await mock_db.get_rss_feeds()
    assert len(feeds) == 1
    assert feeds[0]["last_fetched_at"] == "2026-03-03T12:00:00+00:00"
    assert feeds[0]["last_entry_id"] == "entry-123"


@pytest.mark.asyncio
async def test_item_exists_by_source_url(mock_db, sample_items):
    # No items yet
    assert await mock_db.item_exists_by_source_url("https://example.com/article") is False

    # Save an item with source_url
    item = sample_items[0]
    item.source_url = "https://example.com/article"
    await mock_db.save_item(item)

    assert await mock_db.item_exists_by_source_url("https://example.com/article") is True
    assert await mock_db.item_exists_by_source_url("https://other.com/article") is False


# ── RSSFetcher.fetch_feed ──


def _make_mock_feed(title="Test Feed", entries=None):
    """Create a mock feedparser result."""
    mock = MagicMock()
    mock.bozo = False
    mock.feed.get = lambda key, default="": {"title": title}.get(key, default)
    if entries is None:
        entries = [
            {"id": "entry-1", "title": "First Post", "link": "https://example.com/1", "summary": "Summary 1"},
            {"id": "entry-2", "title": "Second Post", "link": "https://example.com/2", "summary": "Summary 2"},
        ]
    mock_entries = []
    for e in entries:
        entry_mock = MagicMock()
        entry_mock.get = lambda key, default="", _e=e: _e.get(key, default)
        mock_entries.append(entry_mock)
    mock.entries = mock_entries
    return mock


@pytest.mark.asyncio
async def test_fetch_feed_success():
    mock_feed = _make_mock_feed(title="My Blog")

    with patch("src.rss_fetcher.feedparser.parse", return_value=mock_feed):
        title, entries = await RSSFetcher.fetch_feed("https://example.com/feed.xml")

    assert title == "My Blog"
    assert len(entries) == 2
    assert entries[0]["id"] == "entry-1"
    assert entries[0]["title"] == "First Post"
    assert entries[0]["link"] == "https://example.com/1"
    assert entries[1]["id"] == "entry-2"


@pytest.mark.asyncio
async def test_fetch_feed_bozo_with_no_entries():
    mock_feed = MagicMock()
    mock_feed.bozo = True
    mock_feed.bozo_exception = Exception("Malformed XML")
    mock_feed.entries = []

    with patch("src.rss_fetcher.feedparser.parse", return_value=mock_feed):
        with pytest.raises(ValueError, match="Failed to parse feed"):
            await RSSFetcher.fetch_feed("https://example.com/bad-feed")


@pytest.mark.asyncio
async def test_fetch_feed_bozo_with_entries():
    """Bozo feeds with entries should still return data (lenient parsing)."""
    mock_feed = _make_mock_feed(title="Partial Feed", entries=[
        {"id": "e1", "title": "Post", "link": "https://example.com/p1", "summary": "S"},
    ])
    mock_feed.bozo = True
    mock_feed.bozo_exception = Exception("Some warning")

    with patch("src.rss_fetcher.feedparser.parse", return_value=mock_feed):
        title, entries = await RSSFetcher.fetch_feed("https://example.com/partial")

    assert title == "Partial Feed"
    assert len(entries) == 1
