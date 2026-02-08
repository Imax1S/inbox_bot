"""Fetch and extract article content from URLs."""

import logging

import httpx

logger = logging.getLogger(__name__)

MAX_CHARS = 20_000  # ~5000 tokens
FETCH_TIMEOUT = 15.0
USER_AGENT = (
    "Mozilla/5.0 (compatible; DigestBot/1.0; +https://github.com/inbox-agent-bot)"
)


async def fetch_and_extract(url: str) -> tuple[str | None, str | None]:
    """Fetch a URL and extract article text.

    Returns (extracted_text, error_message).
    If extraction succeeds, error_message is None.
    If it fails, extracted_text may still contain partial content.
    """
    try:
        html = await _fetch_html(url)
    except Exception as e:
        logger.warning("Failed to fetch %s: %s", url, e)
        return None, f"Fetch failed: {e}"

    # Try readability-lxml first
    text = _extract_with_readability(html)

    # Fallback to BeautifulSoup heuristic
    if not text or len(text.strip()) < 100:
        text = _extract_with_bs4(html)

    if not text or len(text.strip()) < 50:
        return None, "Could not extract article text"

    # Truncate to token budget
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + "\n\n[...truncated]"

    return text, None


async def _fetch_html(url: str) -> str:
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=FETCH_TIMEOUT,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.text


def _extract_with_readability(html: str) -> str | None:
    try:
        from readability import Document

        doc = Document(html)
        summary_html = doc.summary()

        from bs4 import BeautifulSoup

        soup = BeautifulSoup(summary_html, "html.parser")
        return soup.get_text(separator="\n", strip=True)
    except Exception as e:
        logger.debug("readability extraction failed: %s", e)
        return None


def _extract_with_bs4(html: str) -> str | None:
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")

        # Remove script, style, nav, footer elements
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()

        # Get text from the largest content block
        paragraphs = soup.find_all("p")
        if paragraphs:
            text = "\n\n".join(p.get_text(strip=True) for p in paragraphs if p.get_text(strip=True))
            if len(text) > 100:
                return text

        # Last resort: full body text
        body = soup.find("body")
        if body:
            return body.get_text(separator="\n", strip=True)

        return soup.get_text(separator="\n", strip=True)
    except Exception as e:
        logger.debug("bs4 extraction failed: %s", e)
        return None


def extract_page_title(html: str) -> str | None:
    """Extract the page title from HTML."""
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        title_tag = soup.find("title")
        return title_tag.get_text(strip=True) if title_tag else None
    except Exception:
        return None
