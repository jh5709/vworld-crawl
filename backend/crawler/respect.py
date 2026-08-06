"""
Respectful crawling utilities.

- Browser-like User-Agent string
- Randomized request delays (1–3s)
- robots.txt parsing and Crawl-Delay honoring (disallowed URLs are NOT fetched)
"""

import logging
import random
import time
from typing import Optional
from urllib.robotparser import RobotFileParser
from urllib.parse import urlparse
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

# Browser-like User-Agent (Chrome on Linux, stable version)
BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
)

# Use this UA for robots.txt too (not Python-urllib)
ROBOTS_UA = BROWSER_UA

DEFAULT_MIN_DELAY = 1.0
DEFAULT_MAX_DELAY = 3.0


def random_delay(min_s: float = DEFAULT_MIN_DELAY, max_s: float = DEFAULT_MAX_DELAY) -> float:
    """Sleep for a randomized interval, returning the actual delay used."""
    delay = random.uniform(min_s, max_s)
    logger.debug("Rate-limit delay: %.1fs", delay)
    time.sleep(delay)
    return delay


class RobotsCache:
    """Cache robots.txt rules per (scheme, host)."""

    def __init__(self):
        self._parsers: dict[str, RobotFileParser | None] = {}

    def _robots_url(self, url: str) -> str:
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}/robots.txt"

    def _ensure(self, url: str) -> RobotFileParser | None:
        robots_url = self._robots_url(url)
        if robots_url in self._parsers:
            return self._parsers[robots_url]

        rp = RobotFileParser()
        rp.set_url(robots_url)
        try:
            # Fetch robots.txt with the same browser UA
            req = Request(robots_url, headers={"User-Agent": ROBOTS_UA})
            rp.parse(urlopen(req, timeout=10).read().decode("utf-8"))
            logger.info("robots.txt fetched from %s with UA %s", robots_url, ROBOTS_UA)
            delay = rp.crawl_delay(ROBOTS_UA)
            if delay:
                logger.info("robots.txt Crawl-Delay: %d seconds", delay)
        except Exception:
            logger.debug("robots.txt unavailable at %s, assuming allowed", robots_url)
            rp = None
        self._parsers[robots_url] = rp
        return rp

    def is_allowed(self, url: str) -> bool:
        """Check if a URL is allowed by robots.txt. Returns True if allowed."""
        rp = self._ensure(url)
        if rp is None:
            return True
        try:
            return rp.can_fetch(ROBOTS_UA, url)
        except Exception:
            return True

    def crawl_delay(self, url: str) -> Optional[float]:
        """Return the Crawl-Delay from robots.txt, if specified."""
        rp = self._ensure(url)
        if rp is None:
            return None
        try:
            delay = rp.crawl_delay(ROBOTS_UA)
            return float(delay) if delay else None
        except Exception:
            return None


_robots_cache = RobotsCache()


def check_robots(url: str) -> Optional[float]:
    """Check robots.txt for the given URL.

    Returns Crawl-Delay in seconds, or None.
    Raises RobotsDisallowed if the URL is explicitly disallowed.
    """
    if not _robots_cache.is_allowed(url):
        raise RobotsDisallowed(f"robots.txt disallows: {url}")
    return _robots_cache.crawl_delay(url)


class RobotsDisallowed(Exception):
    """Raised when robots.txt disallows a URL."""
    pass
