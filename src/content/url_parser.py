"""Fetch and extract article content from URLs."""

import logging
import re
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

MAX_CHARS = 20_000  # ~5000 tokens
FETCH_TIMEOUT = 15.0
USER_AGENT = (
    "Mozilla/5.0 (compatible; DigestBot/1.0; +https://github.com/inbox-agent-bot)"
)

_TWITTER_DOMAINS = {"twitter.com", "x.com", "mobile.twitter.com", "mobile.x.com"}
_TWITTER_OEMBED_URL = "https://publish.twitter.com/oembed"
_FXTWITTER_API_URL = "https://api.fxtwitter.com"


def _is_twitter_url(url: str) -> bool:
    """Return True if the URL points to twitter.com or x.com."""
    try:
        host = urlparse(url).netloc.lower().lstrip("www.")
        return host in _TWITTER_DOMAINS
    except Exception:
        return False


def _parse_twitter_url(url: str) -> tuple[str | None, str | None]:
    """Extract (username, tweet_id) from a Twitter/X URL.

    Returns (None, None) if the URL doesn't match the expected format.
    """
    try:
        path_parts = urlparse(url).path.strip("/").split("/")
        # Expected: /{username}/status/{tweet_id}[/...]
        if len(path_parts) >= 3 and path_parts[1] == "status":
            return path_parts[0], path_parts[2]
    except Exception:
        pass
    return None, None


async def _fetch_twitter_oembed(url: str) -> tuple[str | None, str | None]:
    """Extract tweet text via Twitter's free oEmbed API (no auth required).

    Returns (extracted_text, error_message).
    """
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=FETCH_TIMEOUT) as client:
            resp = await client.get(
                _TWITTER_OEMBED_URL,
                params={"url": url, "omit_script": "true"},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.warning("Twitter oEmbed request failed for %s: %s", url, e)
        return None, f"Could not fetch tweet: {e}"

    html = data.get("html", "")
    author_name = data.get("author_name", "unknown")
    author_url = data.get("author_url", "")

    # Extract handle from author URL (e.g. https://twitter.com/elonmusk → @elonmusk)
    handle = ""
    if author_url:
        path = urlparse(author_url).path.strip("/")
        if path:
            handle = f"@{path}"

    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        blockquote = soup.find("blockquote")
        if blockquote:
            p = blockquote.find("p")
            if p:
                tweet_text = p.get_text(separator=" ", strip=True)
                # If the tweet text is only a URL, this is likely a Twitter Article —
                # return None so the caller falls back to FxTwitter which has the full content.
                meaningful_text = re.sub(r"https?://\S+", "", tweet_text).strip()
                if not meaningful_text:
                    return None, "Tweet contains only a link (likely a Twitter Article)"
                author_line = f"{author_name} ({handle})" if handle else author_name
                return f"Tweet by {author_line}:\n\n{tweet_text}", None
    except Exception as e:
        logger.debug("Failed to parse oEmbed HTML: %s", e)

    return None, "Could not parse tweet content"


async def _fetch_fxtwitter(url: str) -> tuple[str | None, str | None]:
    """Extract tweet text via FxTwitter API (no auth required, more reliable).

    Returns (extracted_text, error_message).
    """
    username, tweet_id = _parse_twitter_url(url)
    if not username or not tweet_id:
        return None, "Could not parse Twitter URL for FxTwitter lookup"

    api_url = f"{_FXTWITTER_API_URL}/{username}/status/{tweet_id}"
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=FETCH_TIMEOUT) as client:
            resp = await client.get(api_url, headers={"User-Agent": USER_AGENT})
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.warning("FxTwitter API request failed for %s: %s", url, e)
        return None, f"Could not fetch tweet via FxTwitter: {e}"

    tweet = data.get("tweet") or {}
    tweet_text = tweet.get("text", "").strip()

    author = tweet.get("author") or {}
    author_name = author.get("name", "unknown")
    author_handle = author.get("screen_name", "")
    author_line = f"{author_name} (@{author_handle})" if author_handle else author_name

    # Handle Twitter Articles (long-form posts with empty text field)
    article = tweet.get("article")
    if not tweet_text and article:
        article_title = article.get("title", "").strip()
        article_preview = article.get("preview_text", "").strip()
        if article_title or article_preview:
            parts = []
            if article_title:
                parts.append(f"**{article_title}**")
            if article_preview:
                parts.append(article_preview)
            tweet_text = "\n\n".join(parts)
        else:
            return None, "FxTwitter returned empty tweet text"
    elif not tweet_text:
        return None, "FxTwitter returned empty tweet text"

    # Append quoted tweet text if present
    quote = tweet.get("quote")
    if quote:
        q_text = quote.get("text", "").strip()
        q_author = quote.get("author") or {}
        q_name = q_author.get("name", "")
        q_handle = q_author.get("screen_name", "")
        if q_text:
            q_author_line = f"{q_name} (@{q_handle})" if q_handle else q_name
            tweet_text += f"\n\n[Quoting {q_author_line}]: {q_text}"

    return f"Tweet by {author_line}:\n\n{tweet_text}", None


async def fetch_and_extract(url: str) -> tuple[str | None, str | None]:
    """Fetch a URL and extract article text.

    Returns (extracted_text, error_message).
    If extraction succeeds, error_message is None.
    If it fails, extracted_text may still contain partial content.
    """
    # Twitter/X.com requires special handling — regular HTTP gives empty JS shell
    if _is_twitter_url(url):
        logger.debug("Detected Twitter/X URL, using oEmbed: %s", url)
        text, err = await _fetch_twitter_oembed(url)
        if text:
            return text, None
        # oEmbed failed — try FxTwitter as fallback
        logger.debug("oEmbed failed (%s), falling back to FxTwitter: %s", err, url)
        return await _fetch_fxtwitter(url)

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
