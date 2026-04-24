"""Telegraph (telegra.ph) publisher for the final digest.

Usage:
    writer = TelegraphWriter()
    await writer.ensure_account(db)
    url = await writer.publish(title, markdown)

One-time: createAccount is called lazily and the access_token is cached in the
`settings` table. Subsequent publishes reuse it.

The markdown → Node conversion is intentionally minimal — headings, paragraphs,
lists, blockquotes, code, inline bold/italic/code/links. No images (we don't
produce them in the pipeline). If the content exceeds Telegraph's ~64KB limit
the tail is trimmed and a hint is appended pointing at the attached Markdown.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from .db.database import Database

logger = logging.getLogger(__name__)

TELEGRAPH_API = "https://api.telegra.ph"
TELEGRAPH_ACCESS_TOKEN_KEY = "telegraph_access_token"
TELEGRAPH_AUTHOR = "Inbox Agent"
# Telegraph hard-caps page content around 64KB of JSON; leave some headroom.
MAX_CONTENT_CHARS = 55_000


class TelegraphWriter:
    """Thin async wrapper around the Telegraph HTTP API."""

    def __init__(self, client: httpx.AsyncClient | None = None):
        self._client = client
        self._access_token: str | None = None

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=20.0)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def ensure_account(self, db: Database) -> str:
        """Return a valid access_token, creating an account on first run."""
        if self._access_token:
            return self._access_token
        token = await db.get_setting(TELEGRAPH_ACCESS_TOKEN_KEY)
        if token:
            self._access_token = token
            return token
        client = await self._http()
        resp = await client.post(
            f"{TELEGRAPH_API}/createAccount",
            data={
                "short_name": TELEGRAPH_AUTHOR,
                "author_name": TELEGRAPH_AUTHOR,
            },
        )
        resp.raise_for_status()
        payload = resp.json()
        if not payload.get("ok"):
            raise RuntimeError(
                f"Telegraph createAccount failed: {payload.get('error')}"
            )
        token = payload["result"]["access_token"]
        await db.set_setting(TELEGRAPH_ACCESS_TOKEN_KEY, token)
        self._access_token = token
        logger.info("Telegraph account created; token cached in settings")
        return token

    async def publish(self, db: Database, title: str, markdown: str) -> str:
        """Create a Telegraph page and return its URL.

        Does NOT swallow errors — callers decide whether to degrade gracefully.
        """
        access_token = await self.ensure_account(db)
        safe_title = (title or "Digest").strip()[:200] or "Digest"
        trimmed = _truncate_markdown(markdown, MAX_CONTENT_CHARS)
        nodes = markdown_to_nodes(trimmed)

        client = await self._http()
        import json as _json

        resp = await client.post(
            f"{TELEGRAPH_API}/createPage",
            data={
                "access_token": access_token,
                "title": safe_title,
                "author_name": TELEGRAPH_AUTHOR,
                "content": _json.dumps(nodes, ensure_ascii=False),
                "return_content": "false",
            },
        )
        resp.raise_for_status()
        payload = resp.json()
        if not payload.get("ok"):
            raise RuntimeError(
                f"Telegraph createPage failed: {payload.get('error')}"
            )
        url = payload["result"]["url"]
        logger.info("Published Telegraph page: %s", url)
        return url


def _truncate_markdown(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    last_para = cut.rfind("\n\n")
    if last_para > max_chars * 0.5:
        cut = cut[:last_para]
    return cut.rstrip() + "\n\n_…continued in the attached Markdown file._"


# ────────── Markdown → Telegraph Node[] ──────────

_INLINE_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC_STAR = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
_ITALIC_UNDERSCORE = re.compile(r"(?<!_)_([^_\n]+)_(?!_)")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def markdown_to_nodes(text: str) -> list[Any]:
    """Convert a Markdown string into a Telegraph Node array.

    Supported block constructs: ATX headings (h3/h4), paragraphs, blockquotes,
    unordered lists (`-`, `*`, `+`), ordered lists, fenced code blocks (``` ```),
    horizontal rules (`---`). Supported inline: **bold**, *italic*, _italic_,
    `code`, [text](url).
    """
    nodes: list[Any] = []
    lines = text.splitlines()
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            i += 1
            continue

        # Fenced code block
        if stripped.startswith("```"):
            code_lines: list[str] = []
            i += 1
            while i < n and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            if i < n:
                i += 1  # skip closing fence
            nodes.append({
                "tag": "pre",
                "children": ["\n".join(code_lines)],
            })
            continue

        # Horizontal rule
        if re.fullmatch(r"[-*_]{3,}", stripped):
            nodes.append({"tag": "hr"})
            i += 1
            continue

        # ATX heading
        heading_match = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading_match:
            level = len(heading_match.group(1))
            content = heading_match.group(2).strip()
            # Telegraph only supports h3 and h4 in body content
            tag = "h3" if level <= 3 else "h4"
            nodes.append({"tag": tag, "children": _inline_nodes(content)})
            i += 1
            continue

        # Blockquote
        if stripped.startswith(">"):
            quote_lines: list[str] = []
            while i < n and lines[i].strip().startswith(">"):
                quote_lines.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            nodes.append({
                "tag": "blockquote",
                "children": _inline_nodes(" ".join(quote_lines).strip()),
            })
            continue

        # Unordered list
        if re.match(r"^[-*+]\s+", stripped):
            items: list[Any] = []
            while i < n and re.match(r"^[-*+]\s+", lines[i].strip()):
                item_text = re.sub(r"^[-*+]\s+", "", lines[i].strip())
                items.append({"tag": "li", "children": _inline_nodes(item_text)})
                i += 1
            nodes.append({"tag": "ul", "children": items})
            continue

        # Ordered list
        if re.match(r"^\d+[.)]\s+", stripped):
            items = []
            while i < n and re.match(r"^\d+[.)]\s+", lines[i].strip()):
                item_text = re.sub(r"^\d+[.)]\s+", "", lines[i].strip())
                items.append({"tag": "li", "children": _inline_nodes(item_text)})
                i += 1
            nodes.append({"tag": "ol", "children": items})
            continue

        # Paragraph: accumulate consecutive non-empty lines not starting a block
        para_lines: list[str] = [line]
        i += 1
        while i < n:
            next_stripped = lines[i].strip()
            if not next_stripped:
                break
            if (
                next_stripped.startswith("#")
                or next_stripped.startswith(">")
                or next_stripped.startswith("```")
                or re.match(r"^[-*+]\s+", next_stripped)
                or re.match(r"^\d+[.)]\s+", next_stripped)
                or re.fullmatch(r"[-*_]{3,}", next_stripped)
            ):
                break
            para_lines.append(lines[i])
            i += 1
        nodes.append({
            "tag": "p",
            "children": _inline_nodes(" ".join(p.strip() for p in para_lines)),
        })

    return nodes


def _inline_nodes(text: str) -> list[Any]:
    """Convert inline Markdown to a Telegraph children array."""
    if not text:
        return []

    # Stepwise tokenisation: we find the next markup occurrence, emit preceding
    # plain text, then the styled node, and continue. This keeps things simple
    # while covering the set of inline constructs we actually emit.
    out: list[Any] = []
    cursor = 0
    token_res = [
        (_INLINE_CODE, lambda m: {"tag": "code", "children": [m.group(1)]}),
        (_BOLD, lambda m: {"tag": "strong", "children": _inline_nodes(m.group(1))}),
        (_LINK, lambda m: {
            "tag": "a",
            "attrs": {"href": m.group(2)},
            "children": _inline_nodes(m.group(1)),
        }),
        (_ITALIC_STAR, lambda m: {"tag": "em", "children": _inline_nodes(m.group(1))}),
        (_ITALIC_UNDERSCORE, lambda m: {"tag": "em", "children": _inline_nodes(m.group(1))}),
    ]

    while cursor < len(text):
        best_pos = -1
        best_match = None
        best_factory = None
        for pattern, factory in token_res:
            m = pattern.search(text, cursor)
            if m and (best_pos == -1 or m.start() < best_pos):
                best_pos = m.start()
                best_match = m
                best_factory = factory
        if best_match is None:
            out.append(text[cursor:])
            break
        if best_pos > cursor:
            out.append(text[cursor:best_pos])
        out.append(best_factory(best_match))
        cursor = best_match.end()

    return out
