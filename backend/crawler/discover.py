"""
VWorld paginated discovery — walks listing pages to find downloadable spatial files.

Supports auto mode (walk all pages) and manual mode (fetch one page at a time).
Uses BeautifulSoup to parse HTML and extract file metadata.

Spatial files are identified in two ways:
  1. Link text ends with a known spatial extension (.zip, .shp, .geojson, .gpkg, .parquet)
  2. Link URL ends with a known spatial extension (catches generic link text like "download")
"""

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .respect import check_robots, random_delay

logger = logging.getLogger(__name__)

MAX_PAGES = 100  # safety cap for auto-walk

# File extensions recognized as spatial data
SPATIAL_EXTS = (".zip", ".shp", ".geojson", ".gpkg", ".parquet", ".geoparquet")


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class DiscoveredFile:
    """A discovered downloadable file."""
    name: str
    url: str
    size: int = 0
    size_str: str = ""
    date: str = ""
    description: str = ""
    etag: str = ""
    last_modified: str = ""


@dataclass
class DiscoveryResult:
    """Result from a discovery request."""
    files: list[DiscoveredFile] = field(default_factory=list)
    current_page: int = 1
    total_pages: int = 1
    has_next: bool = False
    next_page_url: str = ""
    error: str = ""


@dataclass
class DiscoveryState:
    """Mutable discovery state for cancellation and manual pagination."""
    page_url: str
    current_page: int = 1
    total_pages: int = 1
    next_page_url: str = ""
    accumulated_files: list[DiscoveredFile] = field(default_factory=list)
    stopped: bool = False
    visited_urls: set = field(default_factory=set)


# ---------------------------------------------------------------------------
# Selector configuration
# ---------------------------------------------------------------------------

@dataclass
class ListingSelectors:
    """CSS selectors for parsing the file listing page."""
    row: str = (
        "table.download-list tbody tr, .file-list .file-item, "
        ".data-table tbody tr, tr.file-row"
    )
    name: str = "td:nth-child(2) a, .file-name a, a.file-link, td a"
    size: str = "td:nth-child(3), .file-size"
    date: str = "td:nth-child(4), .file-date"
    description: str = "td:nth-child(1), .file-desc"
    next_page: str = (
        ".pagination .next a, .paging .next a, "
        "a.next_page, a[rel='next'], "
        ".page-nav a:contains('다음'), .page-nav a:contains('Next')"
    )
    total_pages: str = (
        ".pagination .total, .paging .total, .page-info, "
        ".pagination strong:last-of-type"
    )
    page_info: str = ".page-nav, .pagination, .paging"


DEFAULT_SELECTORS = ListingSelectors()


# ---------------------------------------------------------------------------
# File extraction
# ---------------------------------------------------------------------------

def _is_spatial(name: str, url: str) -> bool:
    """Check if a link targets a spatial file."""
    name_lower = name.lower()
    for ext in SPATIAL_EXTS:
        if name_lower.endswith(ext):
            return True
    # Also check URL path ends with a spatial extension (catches "download" links)
    from urllib.parse import urlparse as _up
    url_path = _up(url).path.lower()
    if url_path:
        for ext in SPATIAL_EXTS:
            if url_path.endswith(ext):
                return True
    return False


def _extract_files(soup: BeautifulSoup, selectors: ListingSelectors) -> list[DiscoveredFile]:
    """Parse a listing page and extract downloadable file metadata."""
    files: list[DiscoveredFile] = []

    # Try structured extraction first
    rows = soup.select(selectors.row)
    if rows:
        for row in rows:
            name_el = None
            for name_sel in selectors.name.split(", "):
                name_el = row.select_one(name_sel)
                if name_el:
                    break
            if not name_el:
                link = row.find("a")
                if link and link.get("href"):
                    name_el = link
                else:
                    continue

            file_name = name_el.get_text(strip=True)
            file_url = name_el.get("href", "")
            if not file_url or not _is_spatial(file_name, file_url):
                continue

            # Resolve protocol-relative URLs
            if file_url.startswith("//"):
                file_url = "https:" + file_url

            size_str, size_bytes = "", 0
            for size_sel in selectors.size.split(", "):
                size_el = row.select_one(size_sel)
                if size_el:
                    size_str = size_el.get_text(strip=True)
                    size_bytes = _parse_size(size_str)
                    break

            date_str = ""
            for date_sel in selectors.date.split(", "):
                date_el = row.select_one(date_sel)
                if date_el:
                    date_str = date_el.get_text(strip=True)
                    break

            desc = ""
            for desc_sel in selectors.description.split(", "):
                desc_el = row.select_one(desc_sel)
                if desc_el:
                    desc = desc_el.get_text(strip=True)
                    break

            files.append(DiscoveredFile(
                name=file_name, url=file_url,
                size=size_bytes, size_str=size_str,
                date=date_str, description=desc,
            ))

    # Fallback: find all links on the page pointing to spatial files
    if not files:
        for link in soup.find_all("a", href=True):
            href = link.get("href", "")
            text = link.get_text(strip=True)
            if not _is_spatial(text, href):
                continue
            url = href
            if url.startswith("//"):
                url = "https:" + url
            # Use filename from URL if link text is generic
            name = text
            generic_names = {"download", "link", "file", "here", "click here", ""}
            if name.lower() in generic_names:
                from urllib.parse import urlparse as _up2
                path = _up2(url).path
                if path:
                    name = path.rsplit("/", 1)[-1] or name
            files.append(DiscoveredFile(name=name, url=url))

    return files


def _parse_size(s: str) -> int:
    """Parse human-readable file size string to bytes."""
    s = s.strip().upper().replace(",", "").replace(" ", "")
    if not s:
        return 0
    match = re.match(r"([\d.]+)\s*(B|KB|MB|GB|TB)?", s)
    if not match:
        try:
            return int(float(s))
        except ValueError:
            return 0
    num = float(match.group(1))
    unit = match.group(2) or "B"
    multipliers = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
    return int(num * multipliers.get(unit, 1))


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

def _find_next_page(soup: BeautifulSoup, selectors: ListingSelectors) -> str:
    """Find the next page URL from pagination controls."""
    for next_sel in selectors.next_page.split(", "):
        if ":contains(" in next_sel:
            base_sel = next_sel.split(":contains(")[0].strip()
            text_filter = next_sel.split(":contains('")[1].rstrip("')")
            for link in soup.select(base_sel):
                if text_filter in link.get_text():
                    href = link.get("href", "")
                    if href and href != "#":
                        return href
        else:
            link = soup.select_one(next_sel)
            if link:
                href = link.get("href", "")
                if href and href != "#":
                    return href
    return ""


def _detect_total_pages(soup: BeautifulSoup, selectors: ListingSelectors) -> int:
    """Try to detect total pages from the page info element."""
    for sel in selectors.total_pages.split(", "):
        el = soup.select_one(sel)
        if el:
            text = el.get_text(strip=True)
            match = re.search(r"(?:of|/)\s*(\d+)", text)
            if match:
                return int(match.group(1))
            try:
                return int(text)
            except ValueError:
                continue

    page_nav = soup.select_one(selectors.page_info)
    if page_nav:
        numbers = []
        for link in page_nav.find_all("a"):
            try:
                numbers.append(int(link.get_text(strip=True)))
            except ValueError:
                pass
        if numbers:
            return max(numbers)
    return 1


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def discover_files(
    session,  # CrawlSession
    page_url: str,
    selectors: ListingSelectors | None = None,
    state: DiscoveryState | None = None,
) -> DiscoveryResult:
    """Fetch a single listing page and extract file metadata.

    Delay/robots checking is done by the caller (discover_all_pages) so
    this function runs at full speed when called standalone.
    """
    sel = selectors or DEFAULT_SELECTORS
    result = DiscoveryResult()

    try:
        resp = session.client.get(page_url)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")
        result.files = _extract_files(soup, sel)

        # Capture ETag/Last-Modified from response (apply to all files on page)
        etag = resp.headers.get("etag", "")
        last_mod = resp.headers.get("last-modified", "")
        for f in result.files:
            if etag:
                f.etag = etag
            if last_mod:
                f.last_modified = last_mod

        result.total_pages = _detect_total_pages(soup, sel)
        result.current_page = state.current_page if state else 1

        next_raw = _find_next_page(soup, sel)
        if next_raw and not next_raw.startswith("http"):
            next_raw = urljoin(page_url, next_raw)
        result.next_page_url = next_raw
        result.has_next = bool(result.next_page_url)

        # Resolve relative file URLs
        for f in result.files:
            if not f.url.startswith("http"):
                f.url = urljoin(page_url, f.url)

        logger.info(
            "Page %d: %d files, has next: %s",
            result.current_page, len(result.files), result.has_next,
        )

    except Exception as e:
        logger.exception("Discovery failed for %s", page_url)
        result.error = str(e)

    return result


def discover_all_pages(
    session,  # CrawlSession
    start_url: str,
    selectors: ListingSelectors | None = None,
    state: DiscoveryState | None = None,
) -> DiscoveryResult:
    """Walk all listing pages (auto mode), accumulating files.

    Rate-limit delay is applied between pages (not within discover_files).
    """
    if state is None:
        state = DiscoveryState(page_url=start_url)

    sel = selectors or DEFAULT_SELECTORS
    result = DiscoveryResult()
    current_url = start_url

    # Check robots.txt once for the domain
    crawl_delay = check_robots(start_url)

    page_count = 0
    while current_url and not state.stopped and page_count < MAX_PAGES:
        # Prevent infinite loops
        if current_url in state.visited_urls:
            logger.warning("Already visited %s — stopping to prevent loop", current_url)
            break
        state.visited_urls.add(current_url)

        # Rate-limit
        if crawl_delay and page_count > 0:
            time.sleep(crawl_delay)
        elif page_count > 0:
            random_delay()

        page_result = discover_files(session, current_url, sel, state)
        page_count += 1

        if page_result.error:
            result.error = page_result.error
            break

        result.files.extend(page_result.files)
        state.accumulated_files = result.files
        state.current_page = page_count + 1

        if not page_result.has_next:
            result.has_next = False
            break

        next_url = page_result.next_page_url
        if next_url.startswith("/"):
            next_url = urljoin(current_url, next_url)
        current_url = next_url
        state.next_page_url = current_url

    # Deduplicate
    seen_urls: set[str] = set()
    deduped: list[DiscoveredFile] = []
    for f in result.files:
        if f.url not in seen_urls:
            seen_urls.add(f.url)
            deduped.append(f)
    result.files = deduped

    if state.stopped:
        logger.info("Discovery stopped after %d files", len(result.files))
        result.error = "stopped"
    else:
        result.total_pages = page_count
        result.current_page = page_count
        result.has_next = not state.stopped and page_count < MAX_PAGES and page_result.has_next
        logger.info("Discovery complete: %d files across %d pages",
                     len(result.files), page_count)

    return result
