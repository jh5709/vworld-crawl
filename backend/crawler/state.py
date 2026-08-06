"""
Crawl state persistence — tracks URLs, ETags, and timestamps for incremental
re-crawls (ticket #40). Uses the plain DuckDB catalog (NOT DuckLake) since
this is app metadata, not lakehouse data.

Creates a `crawl_state` table in the DuckDB catalog file.
"""

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import duckdb

logger = logging.getLogger(__name__)

CRAWL_STATE_TABLE = "crawl_state"

# Path to the plain DuckDB catalog (same file catalog.py uses)
CATALOG_PATH = os.getenv(
    "VWORLD_CATALOG_PATH",
    os.path.join(os.path.dirname(__file__), "..", "catalog", "vworld_catalog.db"),
)


@dataclass
class CrawlEntry:
    """One row in the crawl state table."""
    url: str
    file_name: str
    etag: str = ""
    last_modified: str = ""
    file_size: int = 0
    downloaded_at: str = ""
    status: str = ""
    local_path: str = ""
    dataset_name: str = ""


def _catalog_connect() -> duckdb.DuckDBPyConnection:
    """Open the plain DuckDB catalog (not DuckLake)."""
    path = os.path.abspath(CATALOG_PATH)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return duckdb.connect(path)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_crawl_state():
    """Create the crawl_state table in the DuckDB catalog if it doesn't exist."""
    with _catalog_connect() as db:
        db.execute(f"""
            CREATE TABLE IF NOT EXISTS {CRAWL_STATE_TABLE} (
                url VARCHAR,
                file_name VARCHAR NOT NULL,
                etag VARCHAR DEFAULT '',
                last_modified VARCHAR DEFAULT '',
                file_size BIGINT DEFAULT 0,
                downloaded_at VARCHAR DEFAULT '',
                status VARCHAR DEFAULT '',
                local_path VARCHAR DEFAULT '',
                dataset_name VARCHAR DEFAULT ''
            )
        """)
        # Add dataset_name column if upgrading from older schema
        cols = [r[0] for r in db.execute(f"DESCRIBE {CRAWL_STATE_TABLE}").fetchall()]
        if "dataset_name" not in cols:
            db.execute(f"ALTER TABLE {CRAWL_STATE_TABLE} ADD COLUMN dataset_name VARCHAR DEFAULT ''")
        # Unique index for upsert semantics (DuckDB supports UNIQUE on non-DuckLake)
        try:
            db.execute(f"""
                CREATE UNIQUE INDEX IF NOT EXISTS ix_crawl_url
                ON {CRAWL_STATE_TABLE} (url)
            """)
        except Exception:
            pass
        logger.debug("crawl_state table ready")


def upsert_entry(entry: CrawlEntry) -> None:
    """Insert or update a crawl state entry (DELETE old + INSERT new).

    Preserves existing dataset_name if the new entry has an empty one.
    This prevents re-download/status updates from unlinking source files
    from their DuckLake tables.
    """
    init_crawl_state()
    with _catalog_connect() as db:
        # Preserve dataset_name from existing entry if new one is empty
        if not entry.dataset_name:
            old = get_entry(entry.url)
            if old and old.dataset_name:
                entry.dataset_name = old.dataset_name

        db.execute(
            f"DELETE FROM {CRAWL_STATE_TABLE} WHERE url = ?",
            [entry.url],
        )
        db.execute(
            f"INSERT INTO {CRAWL_STATE_TABLE} "
            f"(url, file_name, etag, last_modified, file_size, downloaded_at, status, local_path, dataset_name) "
            f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                entry.url,
                entry.file_name,
                entry.etag,
                entry.last_modified,
                entry.file_size,
                entry.downloaded_at or _now_iso(),
                entry.status,
                entry.local_path,
                entry.dataset_name,
            ],
        )


def get_entry(url: str) -> Optional[CrawlEntry]:
    """Retrieve a crawl state entry by URL."""
    init_crawl_state()
    try:
        with _catalog_connect() as db:
            row = db.execute(
                f"SELECT url, file_name, etag, last_modified, file_size, "
                f"downloaded_at, status, local_path, dataset_name "
                f"FROM {CRAWL_STATE_TABLE} WHERE url = ?",
                [url],
            ).fetchone()
            if row:
                return CrawlEntry(
                    url=row[0], file_name=row[1],
                    etag=row[2] or "", last_modified=row[3] or "",
                    file_size=row[4] or 0, downloaded_at=row[5] or "",
                    status=row[6] or "", local_path=row[7] or "",
                    dataset_name=row[8] or "",
                )
    except Exception as e:
        logger.warning("get_entry failed: %s", e)
    return None


def list_entries() -> list[CrawlEntry]:
    """List all crawl state entries, most recent first."""
    init_crawl_state()
    try:
        with _catalog_connect() as db:
            rows = db.execute(
                f"SELECT url, file_name, etag, last_modified, file_size, "
                f"downloaded_at, status, local_path, dataset_name "
                f"FROM {CRAWL_STATE_TABLE} ORDER BY downloaded_at DESC"
            ).fetchall()
            return [
                CrawlEntry(
                    url=r[0], file_name=r[1], etag=r[2] or "",
                    last_modified=r[3] or "", file_size=r[4] or 0,
                    downloaded_at=r[5] or "", status=r[6] or "",
                    local_path=r[7] or "", dataset_name=r[8] or "",
                )
                for r in rows
            ]
    except Exception:
        return []


def check_changes(
    entries: list[dict],
) -> dict[str, list[dict]]:
    """Compare discovered files against crawl state using ETag/Last-Modified.

    Returns: {"new": [...], "unchanged": [...], "updated": [...]}
    """
    result: dict[str, list[dict]] = {"new": [], "unchanged": [], "updated": []}

    for entry in entries:
        url = entry.get("url", "")
        existing = get_entry(url)
        if existing is None:
            result["new"].append(entry)
        else:
            # Compare ETag and Last-Modified to detect changes
            new_etag = entry.get("etag", "")
            new_lm = entry.get("last_modified", "")
            changed = False
            if new_etag and existing.etag and new_etag != existing.etag:
                changed = True
            elif new_lm and existing.last_modified and new_lm != existing.last_modified:
                changed = True
            elif not new_etag and not new_lm:
                # No metadata to compare — treat as unchanged
                pass
            if changed:
                result["updated"].append(entry)
            else:
                result["unchanged"].append(entry)

    return result


# ---------------------------------------------------------------------------
# Dataset linkage
# ---------------------------------------------------------------------------

def link_to_dataset(local_path: str, dataset_name: str) -> None:
    """Link a downloaded file (by its local path) to a DuckLake table.

    Called after a successful pipeline run so the console can show
    which source files back which DuckLake tables.
    """
    init_crawl_state()
    with _catalog_connect() as db:
        db.execute(
            f"UPDATE {CRAWL_STATE_TABLE} SET dataset_name = ? WHERE local_path = ?",
            [dataset_name, local_path],
        )
        logger.debug("Linked %s → dataset %s", local_path, dataset_name)


def get_entries_by_dataset(dataset_name: str) -> list[CrawlEntry]:
    """Get all source files linked to a DuckLake table."""
    init_crawl_state()
    try:
        with _catalog_connect() as db:
            rows = db.execute(
                f"SELECT url, file_name, etag, last_modified, file_size, "
                f"downloaded_at, status, local_path, dataset_name "
                f"FROM {CRAWL_STATE_TABLE} WHERE dataset_name = ? "
                f"ORDER BY downloaded_at DESC",
                [dataset_name],
            ).fetchall()
            return [
                CrawlEntry(
                    url=r[0], file_name=r[1], etag=r[2] or "",
                    last_modified=r[3] or "", file_size=r[4] or 0,
                    downloaded_at=r[5] or "", status=r[6] or "",
                    local_path=r[7] or "", dataset_name=r[8] or "",
                )
                for r in rows
            ]
    except Exception:
        return []


def get_staged_entries() -> list[CrawlEntry]:
    """Entries whose originals are still on disk in the download staging area."""
    return [
        e for e in list_entries()
        if e.status == "done" and e.local_path and os.path.exists(e.local_path)
    ]


def delete_staged_files(urls: list[str]) -> dict:
    """Delete staged originals by crawl_state URL.

    The crawl_state row is kept with status 'cleaned' so the URL, ETag,
    and Last-Modified survive for re-download and re-crawl change detection.

    Returns {"deleted": [filenames], "failed": [filenames], "freed_bytes": N}
    """
    deleted = []
    failed = []
    freed = 0

    for url in urls:
        entry = get_entry(url)
        if entry is None:
            failed.append(url)
            continue
        if entry.local_path and os.path.exists(entry.local_path):
            try:
                freed += os.path.getsize(entry.local_path)
                os.remove(entry.local_path)
            except OSError as e:
                failed.append(entry.file_name)
                logger.warning("Failed to delete %s: %s", entry.local_path, e)
                continue
        if entry.status != "cleaned":
            entry.status = "cleaned"
            upsert_entry(entry)
        deleted.append(entry.file_name)

    return {"deleted": deleted, "failed": failed, "freed_bytes": freed}


def cleanup_source_files(dataset_name: str) -> dict:
    """Delete source files linked to a dataset.

    Returns {"deleted": [filenames], "failed": [filenames], "freed_bytes": N}
    """
    entries = get_entries_by_dataset(dataset_name)
    deleted = []
    failed = []
    freed = 0

    for entry in entries:
        if entry.local_path and os.path.exists(entry.local_path):
            try:
                freed += os.path.getsize(entry.local_path)
                os.remove(entry.local_path)
                deleted.append(entry.file_name)
                # Update status
                entry.status = "cleaned"
                upsert_entry(entry)
            except OSError as e:
                failed.append(entry.file_name)
                logger.warning("Failed to delete %s: %s", entry.local_path, e)
        elif entry.local_path:
            # File already gone — mark as cleaned
            if entry.status != "cleaned":
                entry.status = "cleaned"
                upsert_entry(entry)

    return {"deleted": deleted, "failed": failed, "freed_bytes": freed}
