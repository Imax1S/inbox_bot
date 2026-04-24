"""Read-only Telegram channel ingestion via Telethon.

Security model:
- Uses the user's personal Telegram session (created via
  `python -m src.scripts.telethon_login`).
- **Only polls channels explicitly added to the `telegram_channels` table.**
  The Telethon client is never asked to read anything outside the allowlist:
  there is no call to `iter_dialogs`, no broad `get_entity` on arbitrary input.
  Any channel not in the DB allowlist is invisible to this module.
- The session file should sit in `data/` and be treated as a secret.

Posts from allowlisted channels are fed into the existing `CollectorAgent`
using the same path as DM and RSS inputs; items are saved to the `items`
table with `source_url = https://t.me/<name_or_id>/<msg_id>` for dedup.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from ..agents.collector import CollectorAgent
from ..content.url_parser import fetch_and_extract
from ..db.database import Database
from ..db.models import Item, ItemStatus, ItemType

logger = logging.getLogger(__name__)

# How often to poll channels (seconds)
CHANNEL_POLL_INTERVAL_SECONDS = 30 * 60
# How many posts to ingest per channel per poll — guard against a backlog flood.
MAX_POSTS_PER_POLL = 20


@dataclass
class ChannelPollResult:
    total_added: int = 0
    items: list[dict] = field(default_factory=list)  # {channel, title, link}


class ChannelReader:
    """Polls allowlisted Telegram channels with Telethon."""

    def __init__(self, api_id: int, api_hash: str, session_path: Path):
        if api_id <= 0 or not api_hash:
            raise ValueError("Telethon api_id/api_hash are required")
        self.api_id = api_id
        self.api_hash = api_hash
        self.session_path = Path(session_path)
        self._lock = asyncio.Lock()

    def is_configured(self) -> bool:
        return (
            self.api_id > 0
            and bool(self.api_hash)
            and self.session_path.exists()
        )

    async def poll_channels(
        self,
        db: Database,
        collector_agent: CollectorAgent,
    ) -> ChannelPollResult:
        """Poll all allowlisted channels and save new posts as items.

        Uses a single Telethon session and a DB-driven allowlist. Any channel
        not returned by `db.list_telegram_channels()` is simply not touched.
        """
        if not self.is_configured():
            logger.debug("ChannelReader not configured — skipping poll")
            return ChannelPollResult()

        channels = await db.list_telegram_channels()
        if not channels:
            logger.debug("No channels in allowlist — skipping poll")
            return ChannelPollResult()

        # Serialize — Telethon sessions don't like concurrent use.
        async with self._lock:
            return await self._poll_with_client(db, collector_agent, channels)

    async def _poll_with_client(
        self,
        db: Database,
        collector_agent: CollectorAgent,
        channels: list[dict],
    ) -> ChannelPollResult:
        # Import locally so the rest of the app works without Telethon installed.
        from telethon import TelegramClient  # type: ignore[import-not-found]

        result = ChannelPollResult()
        client = TelegramClient(
            str(self.session_path),
            self.api_id,
            self.api_hash,
        )
        try:
            await client.connect()
            if not await client.is_user_authorized():
                logger.error(
                    "Telethon session not authorized at %s — "
                    "run `python -m src.scripts.telethon_login` first",
                    self.session_path,
                )
                return result

            for record in channels:
                identifier = record["identifier"]
                try:
                    count, posted = await self._poll_single_channel(
                        client=client,
                        db=db,
                        collector_agent=collector_agent,
                        record=record,
                    )
                except Exception as exc:
                    logger.exception(
                        "Channel %s failed: %s — skipping",
                        identifier,
                        exc,
                    )
                    continue
                result.total_added += count
                result.items.extend(posted)

        finally:
            await client.disconnect()

        return result

    async def _poll_single_channel(
        self,
        client,
        db: Database,
        collector_agent: CollectorAgent,
        record: dict,
    ) -> tuple[int, list[dict]]:
        identifier: str = record["identifier"]
        last_seen: int = record["last_seen_msg_id"] or 0

        entity = await client.get_entity(identifier)
        channel_title = getattr(entity, "title", None) or identifier
        url_slug = _url_slug(identifier, entity)

        added = 0
        posted: list[dict] = []
        newest_seen = last_seen

        # iter_messages with min_id=last_seen and reverse=True yields in chronological
        # order (oldest first), which is what we want for the collector step.
        async for msg in client.iter_messages(
            entity,
            min_id=last_seen,
            reverse=True,
            limit=MAX_POSTS_PER_POLL,
        ):
            if msg.id <= last_seen:
                continue
            if not (msg.text or ""):
                newest_seen = max(newest_seen, msg.id)
                continue

            source_url = f"https://t.me/{url_slug}/{msg.id}"
            if await db.item_exists_by_source_url(source_url):
                newest_seen = max(newest_seen, msg.id)
                continue

            raw_content = msg.text
            extracted_text: str | None = None

            first_url = _first_url(raw_content)
            if first_url:
                try:
                    extracted_text, fetch_err = await fetch_and_extract(first_url)
                    if fetch_err:
                        logger.warning(
                            "Channel %s: URL fetch for %s returned warning: %s",
                            identifier,
                            first_url,
                            fetch_err,
                        )
                except Exception as fetch_exc:
                    logger.warning(
                        "Channel %s: URL fetch crashed for %s: %s",
                        identifier,
                        first_url,
                        fetch_exc,
                    )

            try:
                collector_result = await collector_agent.process(
                    raw_content=raw_content,
                    extracted_text=extracted_text,
                    item_type=ItemType.ARTICLE,
                )
            except Exception as collector_exc:
                logger.exception(
                    "Channel %s: collector failed for msg %s: %s",
                    identifier,
                    msg.id,
                    collector_exc,
                )
                collector_result = None

            item = Item(
                id=str(uuid4()),
                created_at=msg.date or datetime.now(),
                type=ItemType.ARTICLE,
                raw_content=raw_content,
                source_url=source_url,
                extracted_text=extracted_text,
                summary=(
                    collector_result.summary
                    if collector_result
                    else raw_content[:200]
                ),
                tags=collector_result.tags if collector_result else [],
                language=collector_result.language if collector_result else "en",
                week_id=Database.current_period_id(),
                status=ItemStatus.COLLECTED,
            )
            await db.save_item(item)
            added += 1
            newest_seen = max(newest_seen, msg.id)
            posted.append({
                "channel": channel_title,
                "title": (raw_content.split("\n", 1)[0] or "(no title)")[:80],
                "link": source_url,
            })

            logger.info(
                "Channel %s: ingested msg %d (%s)",
                identifier,
                msg.id,
                item.summary[:60],
            )

        if newest_seen > last_seen:
            await db.update_telegram_channel_checkpoint(
                record["id"],
                last_seen_msg_id=newest_seen,
                title=channel_title,
            )

        return added, posted


def _url_slug(identifier: str, entity) -> str:
    """Build the t.me URL fragment for a channel.

    Prefers the public username; falls back to the `c/<internal_id>` form used
    by private/numeric channels.
    """
    username = getattr(entity, "username", None)
    if username:
        return username
    ident = identifier.lstrip("@")
    if ident.startswith("-100"):
        return f"c/{ident[4:]}"
    return ident


_URL_RE = None


def _first_url(text: str) -> str | None:
    """Return the first http(s) URL found in the text, if any."""
    global _URL_RE
    if _URL_RE is None:
        import re
        _URL_RE = re.compile(r"https?://\S+")
    m = _URL_RE.search(text)
    if not m:
        return None
    return m.group(0).rstrip(").,;:!?\"'")
