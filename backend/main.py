"""
VWorld Crawl — FastAPI Backend

Entry point for the web console. Serves the React frontend in production
and exposes REST + WebSocket endpoints for pipeline operations.
"""

import logging
import os
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import duckdb
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse

from catalog import health_check as catalog_health
from ducklake_ops import ducklake_health
from schema_detector import scan_directory, detect_schema, preview_rows

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# --- Paths ---
BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIST = BASE_DIR / "frontend" / "dist"
FRONTEND_INDEX = FRONTEND_DIST / "index.html"

# --- App Lifespan ---
_duckdb_conn: duckdb.DuckDBPyConnection | None = None


def get_db() -> duckdb.DuckDBPyConnection:
    """Get the shared DuckDB connection for the app."""
    assert _duckdb_conn is not None, "DuckDB not initialized"
    return _duckdb_conn


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: init DuckDB + DuckLake. Shutdown: close connection."""
    global _duckdb_conn

    # Initialize DuckDB in-memory connection
    _duckdb_conn = duckdb.connect(":memory:")
    logger.info("DuckDB in-memory connection established.")

    # Setup DuckLake (extension only, no ATTACH — pipeline runner owns the catalog)
    try:
        _duckdb_conn.execute("INSTALL ducklake; LOAD ducklake;")
        logger.info("DuckLake extension loaded.")
    except Exception as e:
        logger.error("DuckLake extension failed to load: %s", e)

    yield

    # Shutdown
    if _duckdb_conn:
        _duckdb_conn.close()
        logger.info("DuckDB connection closed.")


# --- FastAPI App ---
app = FastAPI(
    title="VWorld Crawl",
    description="Geospatial data pipeline console for Korean VWorld shapefiles",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS (allow frontend dev server on :5173)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Health Check ---
@app.get("/api/health")
async def api_health():
    """Health check: verifies DuckDB + DuckLake + catalog connectivity."""
    db = get_db()
    return {
        "app": "vworld-crawl",
        "version": "0.1.0",
        "ducklake": ducklake_health(db),
        "catalog": catalog_health(),
    }


# --- Hello World (smoke test) ---
@app.get("/api")
async def api_root():
    return {"message": "VWorld Crawl API", "docs": "/docs"}


# --- Directory Scanner ---
from pydantic import BaseModel

class ScanRequest(BaseModel):
    path: str

class DetectRequest(BaseModel):
    path: str

class PreviewRequest(BaseModel):
    path: str
    columns: list[dict]  # [{original, renamed, drop}]
    limit: int = 10


@app.post("/api/scan-directory")
async def api_scan_directory(req: ScanRequest):
    """Scan a directory for .zip and .shp files."""
    try:
        files = scan_directory(req.path)
        return {
            "path": req.path,
            "files": [{"name": f.name, "size": f.size, "date": f.date, "path": f.path} for f in files],
        }
    except (FileNotFoundError, NotADirectoryError) as e:
        return {"path": req.path, "files": [], "error": str(e)}
    except Exception as e:
        logger.exception("scan-directory failed")
        return {"path": req.path, "files": [], "error": str(e)}


@app.post("/api/detect-schema")
async def api_detect_schema(req: DetectRequest):
    """Detect schema (CRS, columns, geometry type) from a shapefile or zip."""
    result = detect_schema(req.path)
    return {
        "path": result.path,
        "crs": result.crs,
        "crs_description": result.crs_description,
        "geometry_type": result.geometry_type,
        "row_count": result.row_count,
        "columns": [{"name": c.name, "type": c.type, "width": c.width} for c in result.columns],
        "valid_count": result.valid_count,
        "invalid_count": result.invalid_count,
        "invalid_sample": result.invalid_sample or None,
        "error": result.error or None,
    }


@app.post("/api/preview")
async def api_preview(req: PreviewRequest):
    """Preview first N rows with column mapping applied."""
    result = preview_rows(req.path, req.columns, req.limit)
    if "error" in result:
        return {"columns": [], "rows": [], "error": result["error"]}
    return result


from pipeline import run_pipeline, post_crawl_compact
from db import data_path, metadata_path
from ducklake_console import (
    table_list,
    table_preview,
    table_snapshots,
    compact_table,
    reindex_table,
    expire_snapshots,
)

# --- Crawler imports ---
from crawler.session import CrawlSession
from crawler.discover import (
    DiscoveredFile,
    DiscoveryResult,
    DiscoveryState,
    discover_files,
    discover_all_pages,
    DEFAULT_SELECTORS,
)
from crawler.download import (
    DownloadFile,
    DownloadProgress,
    DownloadState,
    run_download_queue,
    DEFAULT_BATCH_SIZE,
    DEFAULT_DOWNLOAD_DIR,
)
from crawler.state import CrawlEntry, upsert_entry, link_to_dataset, get_entries_by_dataset, cleanup_source_files, get_staged_entries, delete_staged_files, check_changes
from map_api import get_bounds, get_stats, get_features, get_attributes

# Module-level crawler state (single-user app)
import concurrent.futures
_crawler_session: CrawlSession | None = None
_discovery_state: DiscoveryState | None = None
_discovery_thread_future: "concurrent.futures.Future | None" = None
_download_state: DownloadState | None = None
_download_dir: str = os.path.join(os.path.dirname(__file__), DEFAULT_DOWNLOAD_DIR)


class PipelineRequest(BaseModel):
    paths: list[str]
    dataset_name: str
    column_mapping: list[dict]
    data_date: str = ""                 # date string for delta files (YYYY-MM-DD or empty)
    write_mode: str = "append"          # "append" | "upsert"
    conflict_columns: list[str] = []     # upsert key columns when write_mode="upsert"


@app.post("/api/run-pipeline")
async def api_run_pipeline(req: PipelineRequest):
    """Run the Duckle pipeline on selected shapefiles."""
    try:
        result = run_pipeline(
            shapefile_paths=req.paths,
            dataset_name=req.dataset_name,
            column_mapping=req.column_mapping,
            data_path=str(data_path()),
            metadata_path=str(metadata_path()),
            data_date=req.data_date or None,
            write_mode=req.write_mode,
            conflict_columns=req.conflict_columns,
        )

        if result.error:
            return {"success": False, "error": result.error}

        # Link downloaded source files to this dataset (for cleanup/re-download)
        for path in req.paths:
            try:
                link_to_dataset(path, req.dataset_name)
            except Exception:
                pass

        # Run post-crawl operations if rows were loaded (non-fatal)
        if result.rows_loaded > 0:
            try:
                post_crawl_compact(result.dataset)
            except Exception as e:
                logger.warning("Post-crawl error (non-fatal): %s", e)

        return {
            "success": True,
            "dataset": result.dataset,
            "table_name": result.table_name,
            "rows_loaded": result.rows_loaded,
            "rows_rejected": result.rows_rejected,
            "files_processed": result.files_processed,
        }
    except Exception as e:
        logger.exception("Pipeline failed")
        return {"success": False, "error": str(e)}


@app.websocket("/ws/pipeline")
async def ws_pipeline(ws: WebSocket):
    """WebSocket endpoint for real-time pipeline progress."""
    import json as _json
    
    await ws.accept()
    
    try:
        # Receive pipeline parameters
        raw = await ws.receive_text()
        params = _json.loads(raw)
        
        paths = params.get("paths", [])
        dataset_name = params.get("dataset_name", "")
        column_mapping = params.get("column_mapping", [])
        data_date = params.get("data_date", "")
        write_mode = params.get("write_mode", "append")
        conflict_columns = params.get("conflict_columns", [])
        
        if not paths or not dataset_name:
            await ws.send_text(_json.dumps({
                "type": "error",
                "error": "paths and dataset_name are required"
            }))
            await ws.close()
            return
        
        # Progress callback: send events over WebSocket
        async def send_progress(progress):
            try:
                await ws.send_text(_json.dumps({
                    "type": "progress",
                    "phase": progress.phase,
                    "file_index": progress.file_index,
                    "total_files": progress.total_files,
                    "file_name": progress.file_name,
                    "nodes": [
                        {
                            "name": n.name,
                            "status": n.status,
                            "rows": n.rows,
                            "error": n.error or None,
                        }
                        for n in progress.nodes
                    ],
                }, default=str))
            except Exception:
                pass
        
        # Run pipeline in a thread so the event loop stays responsive
        import concurrent.futures
        
        def run_in_thread():
            return run_pipeline(
                shapefile_paths=paths,
                dataset_name=dataset_name,
                column_mapping=column_mapping,
                data_path=str(data_path()),
                metadata_path=str(metadata_path()),
                data_date=data_date or None,
                write_mode=write_mode,
                conflict_columns=conflict_columns,
                progress_callback=lambda p: asyncio.run_coroutine_threadsafe(
                    send_progress(p), loop
                ),
            )
        
        loop = asyncio.get_running_loop()
        
        # Send start event
        await ws.send_text(_json.dumps({
            "type": "start",
            "dataset": dataset_name,
            "total_files": len(paths),
        }))
        
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(run_in_thread)
            result = await asyncio.wrap_future(future)
        
        # Post-crawl (non-fatal)
        if result.rows_loaded > 0:
            try:
                post_crawl_compact(result.dataset)
            except Exception as e:
                logger.warning("Post-crawl error (non-fatal): %s", e)

        # Link downloaded source files to dataset
        for path in paths:
            try:
                link_to_dataset(path, dataset_name)
            except Exception:
                pass
        
        # Send completion
        await ws.send_text(_json.dumps({
            "type": "complete",
            "success": not result.error,
            "dataset": result.dataset,
            "table_name": result.table_name,
            "rows_loaded": result.rows_loaded,
            "rows_rejected": result.rows_rejected,
            "files_processed": result.files_processed,
            "error": result.error or None,
        }))
        
    except WebSocketDisconnect:
        logger.info("WebSocket disconnected")
    except Exception as e:
        logger.exception("WebSocket pipeline error")
        try:
            await ws.send_text(_json.dumps({
                "type": "error",
                "error": str(e)
            }))
        except Exception:
            pass
    finally:
        try:
            await ws.close()
        except Exception:
            pass


# --- DuckLake Data Viewer (quick access before console is built) ---


@app.get("/api/pick-directory")
async def api_pick_directory():
    """Open a native folder picker dialog and return the selected path."""
    import subprocess
    
    # Try common Linux file pickers, fall back gracefully
    for cmd in [
        ["zenity", "--file-selection", "--directory", "--title=Select Shapefile Directory"],
        ["kdialog", "--getexistingdirectory"],
        ["zenity", "--file-selection", "--title=Select Shapefile Directory"],
    ]:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode == 0 and result.stdout.strip():
                return {"path": result.stdout.strip()}
        except Exception:
            continue
    
    return {"path": "", "error": "No folder picker available. Type the path manually."}

@app.get("/api/tables")
async def api_list_tables():
    """List all tables in the DuckLake catalog with stats."""
    if not metadata_path().exists():
        return {"tables": [], "note": "No catalog found. Run a pipeline first."}
    return {"tables": table_list()}


@app.get("/api/tables/{table_name}")
async def api_table_data(table_name: str, limit: int = 20, offset: int = 0):
    """Get rows from a DuckLake table (geometry/blob columns hidden)."""
    try:
        return table_preview(table_name, limit, offset)
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/tables/{table_name}/snapshots")
async def api_table_snapshots(table_name: str):
    """Get snapshot timeline for a table."""
    return {"snapshots": table_snapshots(table_name)}


@app.get("/api/tables/{table_name}/sources")
async def api_table_sources(table_name: str):
    """Get source files linked to a DuckLake table (from crawl_state)."""
    entries = get_entries_by_dataset(table_name)
    return {
        "table": table_name,
        "sources": [
            {
                "url": e.url,
                "file_name": e.file_name,
                "etag": e.etag,
                "last_modified": e.last_modified,
                "file_size": e.file_size,
                "downloaded_at": e.downloaded_at,
                "status": e.status,
                "local_path": e.local_path,
            }
            for e in entries
        ],
    }


@app.post("/api/tables/{table_name}/compact")
async def api_compact_table(table_name: str):
    """Compact a table by merging adjacent files."""
    return compact_table(table_name)


@app.post("/api/tables/{table_name}/reindex")
async def api_reindex_table(table_name: str):
    """Reindex a table by rewriting data files."""
    return reindex_table(table_name)


# ---------------------------------------------------------------------------
# Map preview endpoints
# ---------------------------------------------------------------------------

@app.get("/api/tables/{table_name}/bounds")
async def api_table_bounds(table_name: str):
    """Full-table bounding box for map preview."""
    bounds = get_bounds(table_name)
    if bounds is None:
        return {"error": "No spatial data found"}
    return {
        "table": table_name,
        "xmin": bounds.xmin, "ymin": bounds.ymin,
        "xmax": bounds.xmax, "ymax": bounds.ymax,
    }


@app.get("/api/tables/{table_name}/stats")
async def api_table_stats(table_name: str):
    """Per-column statistics for the whole table."""
    return get_stats(table_name)


class FeaturesRequest(BaseModel):
    xmin: float | None = None
    ymin: float | None = None
    xmax: float | None = None
    ymax: float | None = None
    limit: int | None = None


@app.post("/api/tables/{table_name}/features")
async def api_table_features(table_name: str, req: FeaturesRequest):
    """GeoJSON features from a table, optionally filtered by bounding box."""
    try:
        return get_features(
            table_name,
            xmin=req.xmin, ymin=req.ymin,
            xmax=req.xmax, ymax=req.ymax,
            limit=req.limit,
        )
    except Exception as e:
        logger.exception("Features query failed for %s", table_name)
        return {"error": str(e)}


class AttributesRequest(BaseModel):
    xmin: float | None = None
    ymin: float | None = None
    xmax: float | None = None
    ymax: float | None = None
    offset: int = 0
    limit: int = 100


@app.post("/api/tables/{table_name}/attributes")
async def api_table_attributes(table_name: str, req: AttributesRequest):
    """Paginated attribute rows for the attribute table view."""
    try:
        return get_attributes(
            table_name,
            xmin=req.xmin, ymin=req.ymin,
            xmax=req.xmax, ymax=req.ymax,
            offset=req.offset, limit=req.limit,
        )
    except Exception as e:
        logger.exception("Attributes query failed for %s", table_name)
        return {"error": str(e)}


@app.post("/api/ducklake/expire-snapshots")
async def api_expire_snapshots(days: int = 30, dry_run: bool = False):
    """Expire catalog snapshots older than N days (catalog-wide).

    dry_run=true previews what would be expired without changing anything.
    """
    return expire_snapshots(days, dry_run)


# ---------------------------------------------------------------------------
# Crawler Endpoints
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    host: str = ""
    username: str = ""
    password: str = ""
    login_url: str = ""
    target_url: str = ""
    auth_required: bool = True  # False = public/no-auth mode


class DiscoverRequest(BaseModel):
    auto: bool = True
    page_url: str = ""


class DownloadRequest(BaseModel):
    files: list[dict]
    batch_size: int = DEFAULT_BATCH_SIZE
    download_dir: str = ""


@app.post("/api/crawler/login")
async def api_crawler_login(req: LoginRequest):
    """Authenticate to VWorld (or create a public session) and store it."""
    global _crawler_session, _discovery_state, _download_state

    try:
        password = req.password or os.getenv("VWORLD_PASSWORD", "")
        host = req.host or os.getenv("VWORLD_URL", "")
        username = req.username or os.getenv("VWORLD_USERNAME", "")

        if req.auth_required and (not host or not username or not password):
            return {"success": False, "error": "Host, username, and password are required."}

        session = CrawlSession(
            host=host,
            username=username,
            password=password,
            auth_required=req.auth_required,
        )

        if req.auth_required:
            login_url = req.login_url or None
            target_url = req.target_url or None
            success = session.login(login_url=login_url, target_url=target_url)
        else:
            session._connect()
            success = True

        if success:
            _crawler_session = session
            _discovery_state = None
            _download_state = None
            return {"success": True, "message": "Session ready", "authenticated": session.authenticated}
        else:
            session.close()
            return {"success": False, "error": "Login failed — check credentials and URL"}
    except Exception as e:
        logger.exception("Crawler login error")
        return {"success": False, "error": str(e)}


@app.get("/api/crawler/session-status")
async def api_crawler_session_status():
    """Check if the crawler session is active."""
    global _crawler_session

    if _crawler_session is None:
        return {"authenticated": False, "host": "", "session_active": False}

    try:
        alive = _crawler_session.check_session()
        return {
            "authenticated": alive,
            "host": _crawler_session.host if alive else "",
            "session_active": alive,
        }
    except Exception:
        return {"authenticated": False, "host": "", "session_active": False}


@app.post("/api/crawler/logout")
async def api_crawler_logout():
    """Clear the crawler session."""
    global _crawler_session, _discovery_state, _download_state

    if _crawler_session:
        _crawler_session.close()
        _crawler_session = None
    _discovery_state = None
    _download_state = None
    return {"success": True}


@app.post("/api/crawler/discover")
async def api_crawler_discover(req: DiscoverRequest):
    """Discover downloadable files. Runs in a background thread so the
    event loop stays responsive and Stop can work."""
    global _crawler_session, _discovery_state, _discovery_thread_future

    if _crawler_session is None:
        return {"success": False, "error": "Not connected. Please create a session first."}

    if not _crawler_session.ensure_session():
        return {"success": False, "error": "Session expired and re-auth failed."}

    import concurrent.futures

    page_url = req.page_url or f"{_crawler_session.host}/data/download" if _crawler_session.host else req.page_url

    if req.auto:
        _discovery_state = DiscoveryState(page_url=page_url)

        def run_auto():
            return discover_all_pages(_crawler_session, page_url, state=_discovery_state)

        loop = asyncio.get_running_loop()
        with concurrent.futures.ThreadPoolExecutor() as pool:
            _discovery_thread_future = pool.submit(run_auto)
            result = await asyncio.wrap_future(_discovery_thread_future)
            _discovery_thread_future = None
    else:
        # Manual: fetch one page, advancing the stored state
        if _discovery_state is None:
            _discovery_state = DiscoveryState(page_url=page_url)
            fetch_url = page_url
        else:
            fetch_url = _discovery_state.next_page_url or page_url

        loop = asyncio.get_running_loop()
        with concurrent.futures.ThreadPoolExecutor() as pool:
            _discovery_thread_future = pool.submit(
                discover_files, _crawler_session, fetch_url, DEFAULT_SELECTORS, _discovery_state
            )
            result = await asyncio.wrap_future(_discovery_thread_future)
            _discovery_thread_future = None

        if not result.error and result.next_page_url:
            _discovery_state.next_page_url = result.next_page_url
            _discovery_state.current_page += 1

    return {
        "success": result.error != "stopped",
        "stopped": result.error == "stopped",
        "files": [
            {
                "name": f.name, "url": f.url, "size": f.size,
                "size_str": f.size_str, "date": f.date,
                "description": f.description, "etag": f.etag,
                "last_modified": f.last_modified,
            }
            for f in result.files
        ],
        "current_page": result.current_page,
        "total_pages": result.total_pages,
        "has_next": result.has_next,
        "error": result.error if result.error and result.error != "stopped" else None,
    }


@app.post("/api/crawler/discover/stop")
async def api_crawler_discover_stop():
    """Stop an ongoing auto-discovery."""
    global _discovery_state, _discovery_thread_future
    if _discovery_state:
        _discovery_state.stopped = True
    if _discovery_thread_future:
        _discovery_thread_future.cancel()
    return {"success": True}


@app.post("/api/crawler/download")
async def api_crawler_download(req: DownloadRequest):
    """Start a download queue. For real-time progress, use the WebSocket endpoint.
    This endpoint waits for completion and returns final results."""
    global _crawler_session, _download_state, _download_dir

    if _crawler_session is None:
        return {"success": False, "error": "Not connected."}
    if not req.files:
        return {"success": False, "error": "No files selected."}

    dir_path = req.download_dir or _download_dir
    os.makedirs(dir_path, exist_ok=True)

    if not _crawler_session.ensure_session():
        return {"success": False, "error": "Session expired."}

    import concurrent.futures

    _download_state = DownloadState(
        files=[DownloadFile(name=f["name"], url=f["url"]) for f in req.files],
        batch_size=req.batch_size,
        download_dir=dir_path,
    )

    loop = asyncio.get_running_loop()
    with concurrent.futures.ThreadPoolExecutor() as pool:
        future = pool.submit(
            run_download_queue,
            _crawler_session, req.files,
            download_dir=dir_path,
            batch_size=req.batch_size,
            state=_download_state,
        )
        state = await asyncio.wrap_future(future)

    _persist_download_state(state)

    completed = sum(1 for f in state.files if f.status == "done")
    failed = sum(1 for f in state.files if f.status == "failed")
    stopped = sum(1 for f in state.files if f.status == "stopped")

    return {
        "success": True,
        "completed": completed, "failed": failed, "stopped": stopped,
        "total": len(state.files), "download_dir": dir_path,
        "files": [
            {"name": f.name, "status": f.status,
             "local_path": f.local_path, "size": f.size, "error": f.error}
            for f in state.files
        ],
    }


def _persist_download_state(state: DownloadState):
    """Persist completed downloads to crawl_state."""
    for f in state.files:
        if f.status == "done":
            try:
                upsert_entry(CrawlEntry(
                    url=f.url, file_name=f.name,
                    etag=f.etag, last_modified=f.last_modified,
                    file_size=f.size, status=f.status, local_path=f.local_path,
                ))
            except Exception as e:
                logger.warning("Failed to persist crawl state for %s: %s", f.name, e)


@app.post("/api/crawler/download/stop")
async def api_crawler_download_stop():
    """Stop an ongoing download queue."""
    global _download_state
    if _download_state:
        _download_state.stopped = True
    return {"success": True}


@app.websocket("/ws/crawler/download")
async def ws_crawler_download(ws: WebSocket):
    """WebSocket endpoint for real-time download progress."""
    import json as _json

    await ws.accept()

    global _crawler_session, _download_state, _download_dir

    try:
        raw = await ws.receive_text()
        params = _json.loads(raw)

        files = params.get("files", [])
        batch_size = params.get("batch_size", DEFAULT_BATCH_SIZE)
        dir_path = params.get("download_dir", _download_dir)

        if not files:
            await ws.send_text(_json.dumps({"type": "error", "error": "No files selected."}))
            await ws.close()
            return

        if _crawler_session is None:
            await ws.send_text(_json.dumps({"type": "error", "error": "Not connected."}))
            await ws.close()
            return

        os.makedirs(dir_path, exist_ok=True)

        # Create a fresh download state so Stop works
        _download_state = DownloadState(
            files=[DownloadFile(name=f["name"], url=f["url"]) for f in files],
            batch_size=batch_size,
            download_dir=dir_path,
        )

        loop = asyncio.get_running_loop()

        def send_progress(progress: DownloadProgress):
            try:
                asyncio.run_coroutine_threadsafe(
                    ws.send_text(_json.dumps({
                        "type": "progress",
                        "phase": progress.phase,
                        "active_count": progress.active_count,
                        "completed_count": progress.completed_count,
                        "failed_count": progress.failed_count,
                        "total_count": progress.total_count,
                        "files": progress.files,
                    }, default=str)),
                    loop,
                )
            except Exception:
                pass

        def run_in_thread():
            return run_download_queue(
                _crawler_session, files,
                download_dir=dir_path,
                batch_size=batch_size,
                progress_callback=send_progress,
                state=_download_state,
            )

        await ws.send_text(_json.dumps({
            "type": "start", "total_files": len(files), "batch_size": batch_size,
        }))

        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(run_in_thread)
            state = await asyncio.wrap_future(future)

        _persist_download_state(state)

        completed = sum(1 for f in state.files if f.status == "done")
        failed = sum(1 for f in state.files if f.status == "failed")
        stopped = sum(1 for f in state.files if f.status == "stopped")

        await ws.send_text(_json.dumps({
            "type": "complete",
            "completed": completed, "failed": failed, "stopped": stopped,
            "total": len(state.files), "download_dir": dir_path,
            "files": [
                {"name": f.name, "status": f.status,
                 "local_path": f.local_path, "size": f.size, "error": f.error}
                for f in state.files
            ],
        }))

    except WebSocketDisconnect:
        logger.info("Crawler WebSocket disconnected")
    except Exception as e:
        logger.exception("Crawler WebSocket error")
        try:
            await ws.send_text(_json.dumps({"type": "error", "error": str(e)}))
        except Exception:
            pass
    finally:
        try:
            await ws.close()
        except Exception:
            pass


@app.get("/api/crawler/download-dir")
async def api_pick_download_dir():
    """Open a native folder picker to choose the download directory."""
    global _download_dir
    import subprocess

    for cmd in [
        ["zenity", "--file-selection", "--directory", "--title=Select Download Directory"],
        ["kdialog", "--getexistingdirectory"],
    ]:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode == 0 and result.stdout.strip():
                path = result.stdout.strip()
                _download_dir = path
                return {"path": path}
        except Exception:
            continue

    return {"path": _download_dir, "error": "No folder picker available. Type the path manually."}


@app.get("/api/crawler/staged")
async def api_crawler_staged():
    """Download staging summary: originals on disk + those not yet loaded into a table."""
    def size_of(e: CrawlEntry) -> int:
        try:
            return os.path.getsize(e.local_path)
        except OSError:
            return e.file_size

    entries = get_staged_entries()
    not_loaded = [e for e in entries if not e.dataset_name]
    return {
        "total_files": len(entries),
        "total_size": sum(size_of(e) for e in entries),
        "not_loaded": [
            {
                "url": e.url,
                "file_name": e.file_name,
                "file_size": size_of(e),
                "local_path": e.local_path,
                "downloaded_at": e.downloaded_at,
            }
            for e in not_loaded
        ],
    }


class StagedDeleteRequest(BaseModel):
    urls: list[str]


@app.post("/api/crawler/staged/delete")
async def api_crawler_staged_delete(req: StagedDeleteRequest):
    """Delete staged originals. crawl_state rows are kept with status 'cleaned'
    so URLs/ETags survive for re-download and re-crawl change detection."""
    try:
        result = delete_staged_files(req.urls)
        return {
            "success": True,
            "deleted": result["deleted"],
            "failed": result["failed"],
            "freed_bytes": result["freed_bytes"],
            "message": f"Cleared {len(result['deleted'])} staged file(s), "
                       f"freed {result['freed_bytes']:,} bytes.",
        }
    except Exception as e:
        logger.exception("Staged delete failed")
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Re-crawl (incremental)
# ---------------------------------------------------------------------------

class RecrawlRequest(BaseModel):
    page_url: str
    auto: bool = True           # auto-paginate all pages


def _detect_delta_date(file_name: str) -> str | None:
    """Detect a date embedded in a filename for delta-file identification.

    Common VWorld / geospatial portal patterns:
      20250801_{dataset}.zip     roads_2025-08-01.zip
      {dataset}_delta_20250801   delta_202508_roads.shp

    Returns ISO date string (YYYY-MM-DD) if detected, None otherwise.
    """
    import re
    patterns = [
        # 20250801 or 2025-08-01
        (r'(\d{4})(\d{2})(\d{2})', lambda m: f"{m.group(1)}-{m.group(2)}-{m.group(3)}"),
        (r'(\d{4})-(\d{2})-(\d{2})', lambda m: m.group(0)),
    ]
    for pat, fmt in patterns:
        match = re.search(pat, file_name)
        if match:
            return fmt(match)
    return None


@app.post("/api/crawler/recrawl")
async def api_crawler_recrawl(req: RecrawlRequest):
    """Re-discover files and compare against crawl_state for incremental crawl.

    Returns file lists partitioned into new/unchanged/updated based on
    ETag and Last-Modified comparison.
    """
    global _discovery_state
    session = _crawler_session
    if session is None:
        return {"success": False, "error": "No active session. Connect first."}

    _discovery_state = DiscoveryState()

    loop = asyncio.get_running_loop()
    with concurrent.futures.ThreadPoolExecutor() as pool:
        files_raw = await loop.run_in_executor(
            pool,
            lambda: (
                discover_all_pages(session, req.page_url, _discovery_state)
                if req.auto
                else discover_page(session, req.page_url, _discovery_state, page_num=1)
            ),
        )

    # Build list for check_changes (dicts with url, etag, last_modified)
    discovered = [
        {
            "url": f.get("url", ""),
            "name": f.get("name", ""),
            "size": f.get("size", 0),
            "size_str": f.get("size_str", ""),
            "date": f.get("date", ""),
            "description": f.get("description", ""),
            "etag": f.get("etag", ""),
            "last_modified": f.get("last_modified", ""),
        }
        for f in files_raw
    ]

    # Split into new/unchanged/updated
    changes = check_changes(discovered)

    # Detect potential delta files
    for f in changes.get("new", []) + changes.get("updated", []):
        date = _detect_delta_date(f.get("name", ""))
        f["delta_date"] = date

    return {
        "success": True,
        "discovered_total": len(discovered),
        "new_count": len(changes.get("new", [])),
        "unchanged_count": len(changes.get("unchanged", [])),
        "updated_count": len(changes.get("updated", [])),
        "new": changes.get("new", []),
        "unchanged": changes.get("unchanged", []),
        "updated": changes.get("updated", []),
    }


@app.post("/api/crawler/cleanup/{dataset_name}")
async def api_crawler_cleanup(dataset_name: str):
    """Delete source files linked to a DuckLake table after successful pipeline.

    The DuckLake Parquet files remain untouched — only the downloaded
    .geojson/.shp/.zip intermediate files are removed.
    """
    try:
        result = cleanup_source_files(dataset_name)
        return {
            "success": True,
            "dataset": dataset_name,
            "deleted": result["deleted"],
            "failed": result["failed"],
            "freed_bytes": result["freed_bytes"],
            "message": f"Cleared {len(result['deleted'])} staged original(s), "
                       f"freed {result['freed_bytes']:,} bytes.",
        }
    except Exception as e:
        logger.exception("Cleanup failed for %s", dataset_name)
        return {"success": False, "error": str(e)}


@app.post("/api/crawler/redownload/{dataset_name}")
async def api_crawler_redownload(dataset_name: str):
    """Re-download source files for a dataset whose local copies were cleaned up.

    Uses the URLs stored in crawl_state. Files go to the default download dir.
    """
    global _crawler_session, _download_dir

    entries = get_entries_by_dataset(dataset_name)
    if not entries:
        return {"success": False, "error": f"No source files tracked for dataset '{dataset_name}'."}

    # Filter to entries that still have a URL (not fully purged)
    redownloadable = [e for e in entries if e.url and e.status == "cleaned"]
    if not redownloadable:
        return {"success": False, "error": f"No cleaned files to re-download for '{dataset_name}'. Files are still on disk or status is '{entries[0].status}'."}

    import concurrent.futures

    files = [{"name": e.file_name, "url": e.url} for e in redownloadable]

    _download_state = DownloadState(
        files=[DownloadFile(name=f["name"], url=f["url"]) for f in files],
        batch_size=min(len(files), 5),
        download_dir=str(_download_dir),
    )

    session = _crawler_session
    if session is None:
        # Create a public session for re-download
        from crawler.session import CrawlSession
        session = CrawlSession(auth_required=False)
        session._connect()

    loop = asyncio.get_running_loop()
    with concurrent.futures.ThreadPoolExecutor() as pool:
        future = pool.submit(
            run_download_queue,
            session, files,
            download_dir=str(_download_dir),
            batch_size=_download_state.batch_size,
            state=_download_state,
        )
        state = await asyncio.wrap_future(future)

    _persist_download_state(state)

    completed = sum(1 for f in state.files if f.status == "done")
    failed = sum(1 for f in state.files if f.status == "failed")

    return {
        "success": True,
        "dataset": dataset_name,
        "completed": completed,
        "failed": failed,
        "message": f"Re-downloaded {completed}/{len(files)} file(s) to {_download_dir}",
    }


@app.get("/api/crawler/env-credentials")
async def api_crawler_env_credentials():
    """Return any credentials set via environment variables for auto-fill."""
    return {
        "url": os.getenv("VWORLD_URL", ""),
        "username": os.getenv("VWORLD_USERNAME", ""),
        "password": "" if not os.getenv("VWORLD_PASSWORD") else "***",
        "has_password": bool(os.getenv("VWORLD_PASSWORD")),
    }


# --- Static Files (React frontend) ---
if FRONTEND_DIST.exists() and FRONTEND_DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}", response_class=HTMLResponse)
    async def serve_react(full_path: str = ""):
        """Serve React SPA for all non-API routes."""
        target = FRONTEND_DIST / full_path
        if target.exists() and target.is_file():
            return FileResponse(target)
        return FileResponse(FRONTEND_INDEX)

    logger.info("Frontend static files mounted from %s", FRONTEND_DIST)
else:
    logger.info("Frontend dist not found at %s — API-only mode", FRONTEND_DIST)


# --- CLI Entry Point ---
if __name__ == "__main__":
    import uvicorn

    host = os.getenv("VWORLD_HOST", "127.0.0.1")
    port = int(os.getenv("VWORLD_PORT", "8000"))
    reload = os.getenv("VWORLD_RELOAD", "false").lower() == "true"

    logger.info("Starting VWorld Crawl on %s:%d (reload=%s)", host, port, reload)
    uvicorn.run("main:app", host=host, port=port, reload=reload)
