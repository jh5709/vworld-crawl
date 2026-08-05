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


class PipelineRequest(BaseModel):
    paths: list[str]
    dataset_name: str
    column_mapping: list[dict]


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
        )

        if result.error:
            return {"success": False, "error": result.error}

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


@app.post("/api/tables/{table_name}/compact")
async def api_compact_table(table_name: str):
    """Compact a table by merging adjacent files."""
    return compact_table(table_name)


@app.post("/api/tables/{table_name}/reindex")
async def api_reindex_table(table_name: str):
    """Reindex a table by rewriting data files."""
    return reindex_table(table_name)


@app.post("/api/ducklake/expire-snapshots")
async def api_expire_snapshots(days: int = 30, dry_run: bool = False):
    """Expire catalog snapshots older than N days (catalog-wide).

    dry_run=true previews what would be expired without changing anything.
    """
    return expire_snapshots(days, dry_run)


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
