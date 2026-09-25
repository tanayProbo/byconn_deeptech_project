"""Link discovery for the crawl frontier.

Shared by the CLI (:mod:`byconn.main`) and the API (:mod:`server`) so both
apply identical frontier rules: same-host only, navigation schemes dropped,
fragments stripped, and ``robots.txt`` respected.
"""

import asyncio
import logging
from typing import List, Optional, Set
from urllib.parse import urldefrag, urljoin, urlparse
from urllib.robotparser import RobotFileParser

logger = logging.getLogger("byconnx.core.link_discovery")

USER_AGENT = "ByconnXBot"

# Schemes that never represent a fetchable page.
SKIPPED_SCHEMES = ("#", "javascript:", "mailto:", "tel:", "data:", "about:")


def extract_links(html: str, base_url: str, host: Optional[str] = None) -> List[str]:
    """Returns unique same-host absolute links found in a page.

    Args:
        html: Raw page HTML.
        base_url: Absolute URL the page was served from, used to resolve
            relative hrefs.
        host: Host to stay on. Defaults to the host of ``base_url``. A crawl
            must not wander to third-party domains.

    Returns:
        Absolute URLs with fragments stripped, in document order and deduped.
    """
    from bs4 import BeautifulSoup  # deferred: keeps import cost off startup

    links: List[str] = []
    seen: Set[str] = set()
    if not html:
        return links

    allowed_host = host or urlparse(base_url).netloc

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception as exc:
        logger.warning("Link extraction failed for %s: %s", base_url, exc)
        return links

    for anchor in soup.find_all("a", href=True):
        href = (anchor["href"] or "").strip()
        if not href or href.lower().startswith(SKIPPED_SCHEMES):
            continue
        absolute, _fragment = urldefrag(urljoin(base_url, href))
        if not absolute or absolute in seen:
            continue
        if allowed_host and urlparse(absolute).netloc != allowed_host:
            continue
        seen.add(absolute)
        links.append(absolute)
    return links


async def load_robots(base_url: str, timeout: float = 10.0) -> Optional[RobotFileParser]:
    """Fetches and parses the target's ``robots.txt``.

    Returns ``None`` when robots.txt cannot be retrieved, which callers treat
    as "no additional restrictions beyond the frontier rules".
    """
    parsed = urlparse(base_url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    parser = RobotFileParser()
    parser.set_url(robots_url)
    try:
        await asyncio.wait_for(parser.read(), timeout=timeout)
        logger.info("Loaded robots.txt for %s", parsed.netloc)
    except Exception as exc:
        logger.warning("Could not load robots.txt (%s); allowing crawl of seed only", exc)
        return None
    return parser


def is_allowed(robots: Optional[RobotFileParser], url: str) -> bool:
    """Checks a URL against robots.txt, defaulting to allowed when unknown."""
    if robots is None:
        return True
    try:
        return robots.can_fetch(USER_AGENT, url)
    except Exception as exc:  # malformed robots.txt must not break a crawl
        logger.debug("robots.txt check failed for %s: %s", url, exc)
        return True


__all__ = ["extract_links", "load_robots", "is_allowed", "USER_AGENT"]
