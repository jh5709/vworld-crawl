"""
VWorld download queue — batched downloads with per-file progress and cancellation.

Downloads selected files to a configurable directory. Progress is reported
via callback (works with WebSocket streaming). The downloader runs in a
background thread so the event loop stays responsive.
"""

import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .respect import random_delay

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 5
DEFAULT_DOWNLOAD_DIR = "downloads"


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class DownloadFile:
    """A file in the download queue."""
    name: str
    url: str
    size: int = 0
    status: str = "queued"      # queued | downloading | done | failed | stopped
    progress: float | None = None  # 0.0–1.0; None = indeterminate
    downloaded_bytes: int = 0
    local_path: str = ""
    error: str = ""
    etag: str = ""
    last_modified: str = ""


@dataclass
class DownloadProgress:
    """Snapshot of the download queue state for progress reporting."""
    phase: str = ""
    files: list[dict] = field(default_factory=list)
    active_count: int = 0
    completed_count: int = 0
    failed_count: int = 0
    total_count: int = 0


@dataclass
class DownloadState:
    """Mutable state for cancellation and tracking."""
    files: list[DownloadFile] = field(default_factory=list)
    batch_size: int = DEFAULT_BATCH_SIZE
    stopped: bool = False
    download_dir: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sanitize_filename(name: str) -> str:
    """Sanitize a filename from a remote source — prevent path traversal."""
    # Strip directory components, nulls, and leading dots/dashes
    safe = os.path.basename(name).lstrip(".")
    if not safe:
        safe = "downloaded_file"
    # Replace any remaining path separators
    safe = safe.replace("/", "_").replace("\\", "_")
    return safe


# ---------------------------------------------------------------------------
# Download logic
# ---------------------------------------------------------------------------

def _download_single(
    session,  # CrawlSession
    file: DownloadFile,
    download_dir: str,
    state: DownloadState,
    progress_callback: Optional[Callable[[DownloadProgress], None]] = None,
) -> DownloadFile:
    """Download a single file with progress tracking."""
    safe_name = _sanitize_filename(file.name)
    local_path = Path(download_dir) / safe_name
    file.local_path = str(local_path)
    file.status = "downloading"
    file.progress = 0.0

    if progress_callback:
        _emit_progress(state, progress_callback)

    try:
        with session.client.stream("GET", file.url, follow_redirects=True) as resp:
            resp.raise_for_status()

            # Capture response metadata
            content_length = resp.headers.get("content-length")
            if content_length:
                file.size = int(content_length)
            file.etag = resp.headers.get("etag", "")
            file.last_modified = resp.headers.get("last-modified", "")

            os.makedirs(download_dir, exist_ok=True)

            downloaded = 0
            with open(local_path, "wb") as f:
                for chunk in resp.iter_bytes(chunk_size=65536):
                    if state.stopped:
                        file.status = "stopped"
                        file.progress = downloaded / max(file.size, 1) if file.size else None
                        file.downloaded_bytes = downloaded
                        try:
                            f.close()
                            os.remove(local_path)
                        except Exception:
                            pass
                        return file

                    f.write(chunk)
                    downloaded += len(chunk)
                    file.downloaded_bytes = downloaded
                    if file.size:
                        file.progress = min(downloaded / file.size, 1.0)
                    else:
                        file.progress = None  # indeterminate

                    # Emit progress roughly every MB
                    if progress_callback and downloaded % (1 << 20) < 65536:
                        _emit_progress(state, progress_callback)

        file.status = "done"
        file.progress = 1.0
        file.downloaded_bytes = downloaded

        logger.info("Downloaded %s (%d bytes) to %s", file.name, downloaded, local_path)

    except Exception as e:
        logger.error("Download failed for %s: %s", file.name, e)
        file.status = "failed"
        file.error = str(e)
        try:
            if os.path.exists(local_path):
                os.remove(local_path)
        except Exception:
            pass

    return file


def _emit_progress(state: DownloadState, callback: Callable):
    """Build and send a progress snapshot."""
    active = sum(1 for f in state.files if f.status == "downloading")
    completed = sum(1 for f in state.files if f.status == "done")
    failed = sum(1 for f in state.files if f.status == "failed")

    if state.stopped:
        phase = "stopped"
    elif completed + failed >= len(state.files):
        phase = "complete"
    else:
        phase = "downloading"

    progress = DownloadProgress(
        phase=phase,
        files=[
            {
                "name": f.name,
                "url": f.url,
                "size": f.size,
                "status": f.status,
                "progress": f.progress,
                "downloaded_bytes": f.downloaded_bytes,
                "local_path": f.local_path,
                "error": f.error,
                "etag": f.etag,
                "last_modified": f.last_modified,
            }
            for f in state.files
        ],
        active_count=active,
        completed_count=completed,
        failed_count=failed,
        total_count=len(state.files),
    )
    callback(progress)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_download_queue(
    session,  # CrawlSession
    files: list[dict],  # [{name, url}]
    download_dir: str = DEFAULT_DOWNLOAD_DIR,
    batch_size: int = DEFAULT_BATCH_SIZE,
    progress_callback: Optional[Callable[[DownloadProgress], None]] = None,
    state: Optional[DownloadState] = None,
) -> DownloadState:
    """Download files with configurable batch size and progress tracking."""
    if state is None:
        state = DownloadState(
            files=[DownloadFile(name=f["name"], url=f["url"]) for f in files],
            batch_size=batch_size,
            download_dir=download_dir,
        )

    os.makedirs(download_dir, exist_ok=True)
    logger.info("Download queue: %d files, batch %d, dir: %s",
                 len(state.files), batch_size, download_dir)

    for i in range(0, len(state.files), batch_size):
        if state.stopped:
            break

        batch = state.files[i:i + batch_size]
        logger.info("Batch %d: %d files", i // batch_size + 1, len(batch))

        threads = []
        for file in batch:
            if file.status in ("done", "failed"):
                continue
            if state.stopped:
                break
            random_delay(0.1, 0.5)
            t = threading.Thread(
                target=_download_single,
                args=(session, file, download_dir, state, progress_callback),
                daemon=True,
            )
            t.start()
            threads.append(t)

        for t in threads:
            t.join()

        if progress_callback:
            _emit_progress(state, progress_callback)

    completed = sum(1 for f in state.files if f.status == "done")
    failed = sum(1 for f in state.files if f.status == "failed")
    logger.info("Download done: %d done, %d failed", completed, failed)

    return state
