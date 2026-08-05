"""
VWorld Crawl — FastAPI Backend

Entry point for the web console. Serves the React frontend in production
and exposes REST + WebSocket endpoints for pipeline operations.
"""

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import duckdb
from fastapi import FastAPI
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
            data_path=os.getenv("VWORLD_DUCKLAKE_DATA_PATH", "vworld_data/"),
            metadata_path=os.getenv(
                "VWORLD_DUCKLAKE_METADATA_PATH", "catalog/ducklake_metadata.ducklake"
            ),
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


# --- DuckLake Data Viewer (quick access before console is built) ---

@app.get("/api/tables")
async def api_list_tables():
    """List all tables in the DuckLake catalog."""
    import duckdb

    metadata_path = os.getenv(
        "VWORLD_DUCKLAKE_METADATA_PATH", "catalog/ducklake_metadata.ducklake"
    )
    if not os.path.exists(metadata_path):
        return {"tables": [], "note": "No catalog found. Run a pipeline first."}

    db = duckdb.connect(":memory:")
    try:
        db.execute("INSTALL ducklake; LOAD ducklake;")
        db.execute(f"ATTACH 'ducklake:{metadata_path}' AS vworld")

        tables = db.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='vworld'"
        ).fetchall()

        result = []
        for (name,) in tables:
            count = db.execute(f"SELECT count(*) FROM vworld.{name}").fetchone()[0]
            cols = db.execute(f"DESCRIBE vworld.{name}").fetchall()
            result.append({
                "name": name,
                "rows": count,
                "columns": [{"name": c[0], "type": c[1]} for c in cols],
            })
        return {"tables": result}
    finally:
        db.close()


@app.get("/api/tables/{table_name}")
async def api_table_data(table_name: str, limit: int = 20, offset: int = 0):
    """Get rows from a DuckLake table."""
    import duckdb

    metadata_path = os.getenv(
        "VWORLD_DUCKLAKE_METADATA_PATH", "catalog/ducklake_metadata.ducklake"
    )
    db = duckdb.connect(":memory:")
    try:
        db.execute("INSTALL ducklake; LOAD ducklake;")
        db.execute(f"ATTACH 'ducklake:{metadata_path}' AS vworld")

        count = db.execute(
            f"SELECT count(*) FROM vworld.{table_name}"
        ).fetchone()[0]

        cols = db.execute(f"DESCRIBE vworld.{table_name}").fetchall()

        # Skip geometry and blob columns for display
        display_cols = [
            c[0] for c in cols
            if c[1] not in ("GEOMETRY", "BLOB", "BIGINT") or c[0] == "id"
        ]

        rows = db.execute(
            f"SELECT {', '.join(display_cols)} FROM vworld.{table_name} "
            f"LIMIT {limit} OFFSET {offset}"
        ).fetchall()

        return {
            "table": table_name,
            "total_rows": count,
            "columns": display_cols,
            "rows": [dict(zip(display_cols, r)) for r in rows],
        }
    except Exception as e:
        return {"error": str(e)}
    finally:
        db.close()


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
