"""Tests for RSS feed functionality — database CRUD and RSSFetcher."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from src.agents.collector import CollectorAgent, CollectorResult
from src.db.database import Database
from src.db.models import ItemType
from src.rss_fetcher import RSSFetcher
from tests.conftest import MockLLMProvider


# ── Database CRUD Tests ──


@pytest.mark.asyncio
async def test_add_rss_feed(mock_db):
    feed_id = await mock_db.add_rss_feed(
        url="https://example.com/feed.xml",
        title="Example Feed",
    )
    assert feed_id is not None
    assert feed_id > 0


@pytest.mark.asyncio
async def test_add_rss_feed_duplicate(mock_db):
    await mock_db.add_rss_feed(url="https://example.com/feed.xml", title="Feed 1")
    with pytest.raises(Exception):
        await mock_db.add_rss_feed(url="https://example.com/feed.xml", title="Feed 2")


@pytest.mark.asyncio
async def test_get_rss_feeds(mock_db):
    await mock_db.add_rss_feed(url="https://example.com/feed1.xml", title="Feed 1")
    await mock_db.add_rss_feed(url="https://example.com/feed2.xml", title="Feed 2")

    feeds = await mock_db.get_rss_feeds()
    assert len(feeds) == 2
    assert feeds[0]["url"] == "https://example.com/feed1.xml"
    assert feeds[0]["title"] == "Feed 1"
    assert feeds[1]["url"] == "https://example.com/feed2.xml"
    assert feeds[1]["title"] == "Feed 2"


@pytest.mark.asyncio
async def test_get_rss_feeds_empty(mock_db):
    feeds = await mock_db.get_rss_feeds()
    assert feeds == []


@pytest.mark.asyncio
async def test_remove_rss_feed(mock_db):
    feed_id = await mock_db.add_rss_feed(
        url="https://example.com/feed.xml", title="Feed"
    )
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
    feed_id = await mock_db.add_rss_feed(
        url="https://example.com/feed.xml", title="Feed"
    )
    await mock_db.update_rss_feed_checkpoint(
        feed_id,
        last_fetched_at="2026-03-03T12:00:00+00:00",
        last_entry_id="entry-123",
    )

    feeds = await mock_db.get_rss_feeds()
    assert len(feeds) == 1
    assert feeds[0]["last_fetched_at"] == "2026-03-03T12:00:00+00:00"
    assert feeds[0]["last_entry_id"] == "entry-123"


@pytest.mark.asyncio
async def test_item_exists_by_source_url(mock_db, sample_items):
    # Save an item with a source_url
    sample_items[0].source_url = "https://example.com/article"
    await mock_db.save_item(sample_items[0])

    assert await mock_db.item_exists_by_source_url("https://example.com/article") is True
    assert await mock_db.item_exists_by_source_url("https://other.com/article") is False


# ── RSSFetcher Tests ──


def _make_mock_feed(title="Test Feed", entries=None):
    """Create a mock feedparser result."""
    feed = MagicMock()
    feed.bozo = False
    feed.feed = {"title": title}
    if entries is None:
        entries = [
            {"title": "Entry 1", "link": "https://example.com/1", "id": "entry-1"},
            {"title": "Entry 2", "link": "https://example.com/2", "id": "entry-2"},
        ]
    mock_entries = []
    for e in entries:
        mock_entry = MagicMock()
        mock_entry.get = e.get
        mock_entries.append(mock_entry)
    feed.entries = mock_entries
    return feed


@pytest.mark.asyncio
async def test_fetch_feed_success():
    fetcher = RSSFetcher()
    mock_feed = _make_mock_feed(title="My Blog", entries=[
        {"title": "Post 1", "link": "https://blog.example.com/1", "id": "guid-1"},
        {"title": "Post 2", "link": "https://blog.example.com/2", "id": "guid-2"},
    ])

    with patch("src.rss_fetcher.feedparser.parse", return_value=mock_feed):
        title, entries = await fetcher.fetch_feed("https://blog.example.com/feed")

    assert title == "My Blog"
    assert len(entries) == 2
    assert entries[0]["title"] == "Post 1"
    assert entries[0]["link"] == "https://blog.example.com/1"
    assert entries[0]["id"] == "guid-1"


@pytest.mark.asyncio
async def test_fetch_feed_bozo_with_no_entries():
    fetcher = RSSFetcher()
    mock_feed = MagicMock()
    mock_feed.bozo = True
    mock_feed.bozo_exception = Exception("not well-formed")
    mock_feed.entries = []

    with patch("src.rss_fetcher.feedparser.parse", return_value=mock_feed):
        with pytest.raises(ValueError, match="Failed to parse feed"):
            await fetcher.fetch_feed("https://invalid.example.com/feed")


@pytest.mark.asyncio
async def test_fetch_feed_bozo_with_entries():
    """Bozo feeds with entries should still return results (partial parse)."""
    fetcher = RSSFetcher()
    mock_feed = _make_mock_feed(title="Partial Feed", entries=[
        {"title": "Post 1", "link": "https://example.com/1", "id": "id-1"},
    ])
    mock_feed.bozo = True
    mock_feed.bozo_exception = Exception("minor issue")

    with patch("src.rss_fetcher.feedparser.parse", return_value=mock_feed):
        title, entries = await fetcher.fetch_feed("https://example.com/feed")

    assert title == "Partial Feed"
    assert len(entries) == 1


@pytest.mark.asyncio
async def test_poll_all_feeds_no_feeds(mock_db):
    provider = MockLLMProvider(structured_response={
        "summary": "test", "tags": ["test"], "language": "en",
    })
    collector = CollectorAgent(
        llm=provider, model="test", db=mock_db, user_profile={"interest_areas": []},
    )
    fetcher = RSSFetcher()
    result = await fetcher.poll_all_feeds(db=mock_db, collector_agent=collector, user_id=123)
    assert result.total_added == 0


@pytest.mark.asyncio
async def test_poll_all_feeds_skips_seen_entries(mock_db, sample_user_profile):
    """Entries at or after last_entry_id should be skipped."""
    provider = MockLLMProvider(structured_response={
        "summary": "test summary", "tags": ["rss"], "language": "en",
    })
    collector = CollectorAgent(
        llm=provider, model="test", db=mock_db, user_profile=sample_user_profile,
    )

    # Add feed with last_entry_id pointing to entry-1 (the first/newest entry)
    feed_id = await mock_db.add_rss_feed(
        url="https://example.com/feed.xml", title="Test Feed"
    )
    await mock_db.update_rss_feed_checkpoint(
        feed_id,
        last_fetched_at="2026-03-03T10:00:00+00:00",
        last_entry_id="entry-1",
    )

    mock_feed = _make_mock_feed(entries=[
        {"title": "Entry 1", "link": "https://example.com/1", "id": "entry-1"},
        {"title": "Entry 2", "link": "https://example.com/2", "id": "entry-2"},
    ])

    fetcher = RSSFetcher()
    with patch("src.rss_fetcher.feedparser.parse", return_value=mock_feed):
        result = await fetcher.poll_all_feeds(
            db=mock_db, collector_agent=collector, user_id=123
        )

    assert result.total_added == 0


@pytest.mark.asyncio
async def test_poll_all_feeds_adds_new_entries(mock_db, sample_user_profile):
    """New entries should be added as items."""
    provider = MockLLMProvider(structured_response={
        "summary": "RSS article summary", "tags": ["rss", "tech"], "language": "en",
    })
    collector = CollectorAgent(
        llm=provider, model="test", db=mock_db, user_profile=sample_user_profile,
    )

    # Add feed with no checkpoint (first poll)
    await mock_db.add_rss_feed(
        url="https://example.com/feed.xml", title="Test Feed"
    )

    mock_feed = _make_mock_feed(entries=[
        {"title": "New Post", "link": "https://example.com/new", "id": "entry-new"},
    ])

    fetcher = RSSFetcher()
    with patch("src.rss_fetcher.feedparser.parse", return_value=mock_feed), \
         patch("src.rss_fetcher.fetch_and_extract", new_callable=AsyncMock) as mock_extract:
        mock_extract.return_value = ("Full article text here", None)
        result = await fetcher.poll_all_feeds(
            db=mock_db, collector_agent=collector, user_id=123
        )

    assert result.total_added == 1
    assert len(result.items) == 1
    assert result.items[0]["title"] == "New Post"
    assert result.items[0]["feed_title"] == "Test Feed"

    # Verify the item was saved
    items = await mock_db.get_items_by_week()
    rss_items = [i for i in items if i.source_url == "https://example.com/new"]
    assert len(rss_items) == 1
    assert rss_items[0].summary == "RSS article summary"
    assert rss_items[0].tags == ["rss", "tech"]

    # Verify checkpoint was updated
    feeds = await mock_db.get_rss_feeds()
    assert feeds[0]["last_entry_id"] == "entry-new"
