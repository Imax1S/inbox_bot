"""Telegraph (telegra.ph) publisher for Telegram Instant View long-read format.

When a telegra.ph URL is sent in Telegram, an "Instant View" button appears
that opens the content in a native reading mode inside the app.
"""

import json
import logging
import re
from typing import Any

import httpx

logger = logging.getLogger(__name__)

TELEGRAPH_API = "https://api.telegra.ph"
TELEGRAPH_MAX_BYTES = 50_000  # Conservative limit; API accepts up to ~64KB

# Inline markdown pattern: splits text into formatted/plain segments
_INLINE_SPLIT = re.compile(
    r"(\*\*[^*\n]+?\*\*"       # **bold**
    r"|\*[^*\n]+?\*"            # *italic*
    r"|`[^`\n]+?`"              # `code`
    r"|\[[^\]\n]+\]\([^)\n]+\))"  # [text](url)
)


class TelegraphPublisher:
    """Publishes markdown digest content to Telegraph for Telegram Instant View."""

    def __init__(self, access_token: str | None = None):
        self.access_token = access_token

    async def ensure_account(self, db) -> None:
        """Load existing or create a new Telegraph account; persists token in DB."""
        token = await db.get_setting("telegraph_token")
        if token:
            self.access_token = token
            return
        self.access_token = await self._create_account()
        await db.set_setting("telegraph_token", self.access_token)

    async def _create_account(self) -> str:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{TELEGRAPH_API}/createAccount",
                json={
                    "short_name": "WeeklyDigest",
                    "author_name": "Weekly Digest Bot",
                },
            )
            data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegraph createAccount failed: {data}")
        return data["result"]["access_token"]

    async def publish(self, content_md: str) -> str:
        """Convert markdown to Telegraph format and create a page.

        Returns the public URL of the created page.
        """
        if not self.access_token:
            raise RuntimeError("No Telegraph access token — call ensure_account() first.")

        title, nodes = _md_to_telegraph(content_md)

        # Truncate if content exceeds Telegraph's size limit
        serialized = json.dumps(nodes, ensure_ascii=False).encode()
        if len(serialized) > TELEGRAPH_MAX_BYTES:
            logger.warning(
                "Telegraph content is %d bytes, truncating to %d",
                len(serialized),
                TELEGRAPH_MAX_BYTES,
            )
            nodes = _truncate_nodes(nodes)

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{TELEGRAPH_API}/createPage",
                json={
                    "access_token": self.access_token,
                    "title": title[:256],
                    "content": nodes,
                    "return_content": False,
                },
            )
            data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegraph createPage failed: {data}")
        return data["result"]["url"]


# ── Markdown → Telegraph nodes ────────────────────────────────────────────────

def _md_to_telegraph(md: str) -> tuple[str, list]:
    """Convert a markdown digest to a (title, nodes) pair for the Telegraph API.

    Telegraph nodes are dicts like ``{"tag": "p", "children": [...]}`` or plain
    strings for text content.  See https://telegra.ph/api#NodeElement
    """
    lines = md.splitlines()
    title = "Weekly Digest"
    nodes: list[Any] = []
    i = 0
    pending_list: list[Any] = []  # buffered <li> nodes waiting to form a <ul>

    # Skip optional YAML frontmatter (--- ... ---)
    if lines and lines[0].strip() == "---":
        i = 1
        while i < len(lines) and lines[i].strip() != "---":
            i += 1
        i += 1  # step past closing ---

    def flush_list() -> None:
        if pending_list:
            nodes.append({"tag": "ul", "children": list(pending_list)})
            pending_list.clear()

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        i += 1

        # ── Determine whether this line is a list item ──────────────────────
        is_list_item = bool(re.match(r"^[-*+] |^\d+\. ", line))
        if not is_list_item:
            flush_list()

        # ── Headings ─────────────────────────────────────────────────────────
        if re.match(r"^# (?!#)", line):
            text = line[2:].strip()
            # Use the first H1 as the page title (plain text, no markdown)
            title = re.sub(r"\*{1,2}([^*]+)\*{1,2}", r"\1", text)
            title = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", title).strip()
            nodes.append({"tag": "h3", "children": _parse_inline(text)})

        elif re.match(r"^## (?!#)", line):
            nodes.append({"tag": "h3", "children": _parse_inline(line[3:].strip())})

        elif re.match(r"^### (?!#)", line):
            nodes.append({"tag": "h4", "children": _parse_inline(line[4:].strip())})

        elif re.match(r"^#{4,} ", line):
            text = re.sub(r"^#+\s+", "", line).strip()
            nodes.append({"tag": "h4", "children": _parse_inline(text)})

        # ── Horizontal rule ───────────────────────────────────────────────────
        elif re.fullmatch(r"[-*_]{3,}", stripped):
            nodes.append({"tag": "hr"})

        # ── Blockquote ────────────────────────────────────────────────────────
        elif line.startswith("> "):
            nodes.append({"tag": "blockquote", "children": _parse_inline(line[2:].strip())})

        # ── Unordered / ordered list items ───────────────────────────────────
        elif re.match(r"^[-*+] ", line):
            pending_list.append({"tag": "li", "children": _parse_inline(line[2:].strip())})

        elif re.match(r"^\d+\. ", line):
            text = re.sub(r"^\d+\.\s+", "", line).strip()
            pending_list.append({"tag": "li", "children": _parse_inline(text)})

        # ── Empty line ────────────────────────────────────────────────────────
        elif not stripped:
            pass  # list already flushed above; blank lines separate paragraphs

        # ── Regular paragraph ─────────────────────────────────────────────────
        else:
            inline = _parse_inline(stripped)
            if inline:
                nodes.append({"tag": "p", "children": inline})

    flush_list()
    return title, nodes


def _parse_inline(text: str) -> list[Any]:
    """Parse inline markdown formatting into Telegraph node children.

    Handles: **bold**, *italic*, `code`, [text](url), and Obsidian [[#links]].
    Returns a list of strings and/or node dicts.
    """
    # Strip Obsidian internal wiki links: [[#Heading Text]] → Heading Text
    text = re.sub(r"\[\[#([^\]]+)\]\]", r"\1", text)

    segments = _INLINE_SPLIT.split(text)
    result: list[Any] = []

    for seg in segments:
        if not seg:
            continue

        bold = re.fullmatch(r"\*\*([^*]+)\*\*", seg)
        italic = re.fullmatch(r"\*([^*]+)\*", seg)
        code = re.fullmatch(r"`([^`]+)`", seg)
        link = re.fullmatch(r"\[([^\]]+)\]\(([^)]+)\)", seg)

        if bold:
            result.append({"tag": "strong", "children": [bold.group(1)]})
        elif italic:
            result.append({"tag": "em", "children": [italic.group(1)]})
        elif code:
            result.append({"tag": "code", "children": [code.group(1)]})
        elif link:
            result.append(
                {
                    "tag": "a",
                    "attrs": {"href": link.group(2)},
                    "children": [link.group(1)],
                }
            )
        else:
            result.append(seg)

    return result


def _truncate_nodes(nodes: list, max_bytes: int = TELEGRAPH_MAX_BYTES) -> list:
    """Return a truncated copy of the nodes list that fits within *max_bytes*."""
    result: list[Any] = []
    current = 2  # for the surrounding []

    for node in nodes:
        chunk = json.dumps(node, ensure_ascii=False).encode()
        # +2 for the JSON comma + space separator
        if current + len(chunk) + 2 > max_bytes:
            result.append(
                {
                    "tag": "p",
                    "children": [
                        {"tag": "em", "children": ["[Content truncated — see the attached .md file for the full digest.]"]}
                    ],
                }
            )
            break
        result.append(node)
        current += len(chunk) + 2

    return result
