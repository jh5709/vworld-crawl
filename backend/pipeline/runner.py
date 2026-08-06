"""
Pipeline runner — Duckle-based ETL for VWorld shapefiles.

Thin abstraction over duckle's Python API as required by ADR #1.
Wraps the full pipeline flow: spatial → project → rename → sql → validate → sink.

Usage:
    from pipeline.runner import run_pipeline

    result = run_pipeline(
        shapefile_paths=["/data/seoul_roads.zip", "/data/busan_roads.zip"],
        dataset_name="roads",
        column_mapping=[
            {"original": "road_id", "renamed": "", "drop": False},
            {"original": "name", "renamed": "road_name", "drop": False},
            {"original": "length_m", "renamed": "", "drop": True},
        ],
        data_path="vworld_data/",
        metadata_path="catalog/ducklake_metadata.ducklake",
    )
"""

import logging
import os
import shutil
import io
from dataclasses import dataclass, field
from typing import Callable, Optional

import duckle

from db import data_path as _default_data_path
from db import ducklake_db, metadata_path as _default_metadata_path, quote_ident
from schema_detector import (
    _get_conn,
    _is_parquet,
    _parquet_crs,
    _parquet_geo_metadata,
    _unzip_spatial,
    parquet_geometry_info,
)
from pipeline.progress import (
    NodeProgress,
    PipelineProgress,
    parse_duckle_output,
    create_initial_progress,
    VALID_NODES,
)

logger = logging.getLogger(__name__)

DUCKLE_VERSION = duckle.__version__


@dataclass
class PipelineResult:
    """Result from a pipeline run for one dataset."""

    dataset: str
    rows_loaded: int = 0
    rows_rejected: int = 0
    table_name: str = ""
    snapshot_version: str = ""
    files_processed: int = 0
    error: str = ""


# ---------------------------------------------------------------------------
# CRS detection + reprojection
# ---------------------------------------------------------------------------

# CRS strings that need no reprojection (already WGS84 lon/lat)
_CRS84_EQUIVALENTS = {"EPSG:4326", "OGC:CRS84", "CRS84", "WGS84", "WGS 84"}


def detect_source_crs(read_path: str) -> str | None:
    """Detect the CRS of a spatial file.

    GeoParquet: from 'geo' key-value metadata (default OGC:CRS84 per spec).
    GDAL formats (.shp/.geojson/.gpkg): ST_CRS on the first geometry.

    Returns a CRS string ST_Transform understands ('EPSG:5179', WKT, PROJJSON),
    or None if unknown.
    """
    conn = _get_conn()
    try:
        if _is_parquet(read_path):
            crs = _parquet_crs(_parquet_geo_metadata(conn, read_path))
            return crs or None
        row = conn.execute(
            "SELECT ST_CRS(geom) FROM ST_Read(?) LIMIT 1", [read_path]
        ).fetchone()
        if row and row[0]:
            return str(row[0])
    except Exception as e:
        logger.warning("CRS detection failed for %s: %s", read_path, e)
    return None


def _normalize_crs(crs: str) -> str:
    """Extract 'EPSG:nnnn' from PROJJSON/WKT when possible, else return raw."""
    import json as _json
    if crs.startswith("{"):
        try:
            data = _json.loads(crs)
            if "id" in data and "code" in data["id"]:
                return f"EPSG:{data['id']['code']}"
            return data.get("name", crs)
        except (ValueError, TypeError):
            return crs
    return crs


def _crs_needs_reproject(crs: str | None) -> bool:
    if not crs:
        return False
    norm = _normalize_crs(crs)
    return norm.upper().replace(" ", "") not in {
        c.replace(" ", "") for c in _CRS84_EQUIVALENTS
    }


# ---------------------------------------------------------------------------
# Per-file pipeline (one province shapefile)
# ---------------------------------------------------------------------------

def _run_single_file(
    shapefile_path: str,
    dataset_name: str,
    column_mapping: list[dict],
    data_path: str,
    metadata_path: str,
    *,
    keep_valid: bool,
    data_date: str | None = None,
    write_mode: str = "append",
    conflict_columns: list[str] | None = None,
    progress_callback: Optional[Callable[[PipelineProgress], None]] = None,
) -> list[NodeProgress]:
    """
    Run the pipeline for a single province file.

    Args:
        shapefile_path: Path to .shp or .zip.
        dataset_name: DuckLake table name.
        column_mapping: [{original, renamed, drop}] from schema editor.
        data_path: DuckLake DATA_PATH directory.
        metadata_path: DuckLake metadata file path.
        keep_valid: True → valid rows go to {dataset}; False → invalid rows go to {dataset}_rejects.
        progress_callback: Called with PipelineProgress updates (for WebSocket streaming).

    Returns:
        List of NodeProgress from duckle's output.
    """
    # Build drop, keep, rename maps.
    # Note: OGC_FID is not force-dropped here — xf.project already restricts
    # output to keep_cols, and OGC_FID doesn't exist in GPKG/GeoParquet.
    #
    # When column_mapping is empty (user provided no schema), pass all
    # columns through — do not reduce to only ["geom"]. The empty-list
    # case means "keep everything", not "keep nothing".
    has_mapping = bool(column_mapping)
    drop_cols = [c["original"] for c in column_mapping if c.get("drop")]
    keep_cols = [c["original"] for c in column_mapping if not c.get("drop")]
    # Always include geom (needed for WKB, bbox, and validation)
    if has_mapping:
        if not keep_cols:
            keep_cols = ["geom"]
        elif "geom" not in keep_cols:
            keep_cols.append("geom")
    rename_map = {
        c["original"]: c["renamed"]
        for c in column_mapping
        if c.get("renamed") and not c.get("drop")
    }

    mode = "valid" if keep_valid else "invalid"
    table_name = dataset_name if keep_valid else f"{dataset_name}_rejects"

    # Pre-check: when upserting a delta file with data_date into an existing
    # table that was created without it, add the column first. DuckLake's
    # MERGE INTO requires matching column counts between source and target.
    # (DuckLake schema evolution handles reads across snapshots for the new
    # column — old data fills NULL — but the write path needs ALTER TABLE.)
    if data_date and write_mode == "upsert" and keep_valid:
        try:
            from db import ducklake_db
            with ducklake_db(catalog=metadata_path, require_catalog=False) as db:
                cols = [r[0] for r in db.execute(
                    f"DESCRIBE vworld.{quote_ident(table_name)}"
                ).fetchall()]
                if "data_date" not in cols:
                    db.execute(
                        f"ALTER TABLE vworld.{quote_ident(table_name)} ADD COLUMN data_date DATE"
                    )
                    logger.info("Added data_date column to %s for delta upsert", table_name)
        except Exception:
            # Table doesn't exist yet — first pipeline run creates it with all columns
            pass

    # Handle .zip files: unzip to temp dir, pass the spatial file to duckle
    read_path = shapefile_path
    tmpdir = None
    if shapefile_path.lower().endswith(".zip"):
        read_path, tmpdir = _unzip_spatial(shapefile_path)

    p = duckle.Pipeline()

    try:
        # 1. Source: read the file. GeoParquet goes through src.parquet +
        #    WKB decode (ST_Read/GDAL can't read Parquet); everything else
        #    (.shp, .geojson, .gpkg) via src.spatial (ST_Read).
        if _is_parquet(read_path):
            info = parquet_geometry_info(_get_conn(), read_path)
            if not info:
                raise ValueError(
                    f"No geometry column found in {os.path.basename(read_path)} "
                    f"(expected WKB BLOB or GEOMETRY column — is this a GeoParquet file?)"
                )
            geom_col, needs_decode = info
            geom_expr = (f'ST_GeomFromWKB("{geom_col}")' if needs_decode
                         else f'"{geom_col}"')
            p.source("src.parquet", path=read_path)
            p.transform(
                "code.sql",
                sql=f'SELECT * EXCLUDE ("{geom_col}"), {geom_expr} AS geom FROM input',
            )
        else:
            p.source("src.spatial", path=read_path)

        # 1.5 Reproject to EPSG:4326 when the source CRS differs.
        # All DuckLake tables are stored in WGS84 lon/lat so the map preview
        # and Wails desktop can consume them without per-table CRS handling.
        src_crs = detect_source_crs(read_path)
        if _crs_needs_reproject(src_crs):
            logger.info(
                "Reprojecting %s: %s → EPSG:4326",
                os.path.basename(read_path), src_crs,
            )
            crs_lit = src_crs.replace("'", "''")  # escape for SQL literal
            p.transform(
                "code.sql",
                sql=(
                    "SELECT * EXCLUDE (geom), "
                    f"ST_Transform(geom, '{crs_lit}', 'EPSG:4326', true) AS geom "
                    "FROM input"
                ),
            )
        elif src_crs is None:
            logger.warning(
                "CRS unknown for %s — assuming EPSG:4326",
                os.path.basename(read_path),
            )

        # 2. Drop columns: user-dropped (only when mapping is provided)
        if has_mapping and drop_cols:
            p.transform("xf.dropcol", columns=drop_cols)

        # 3. Project: only when mapping is provided; otherwise pass all columns
        if has_mapping and keep_cols:
            p.transform("xf.project", columns=keep_cols)

        # 4. Rename: apply user renames (use Pipeline.rename, not xf.rename)
        if rename_map:
            p.rename(**rename_map)

        # 5. SQL: add WKB + bounding box + optional data_date column
        date_cols = ""
        if data_date:
            import re as _re
            if not _re.fullmatch(r"\d{4}-\d{2}-\d{2}", data_date):
                raise ValueError(
                    f"data_date must be YYYY-MM-DD, got {data_date!r}"
                )
            date_cols = f", '{data_date}'::DATE AS data_date"
        p.transform(
            "code.sql",
            sql=f"""
            SELECT *{date_cols},
                   ST_AsWKB(geom) AS geom_wkb,
                   ST_XMin(geom) AS bbox_xmin,
                   ST_YMin(geom) AS bbox_ymin,
                   ST_XMax(geom) AS bbox_xmax,
                   ST_YMax(geom) AS bbox_ymax
            FROM input
            """,
        )

        # 6. Validate: split valid/invalid geometries
        p.transform("qa.geomvalidate", geometryColumn="geom", mode=mode)

        # 7. Sink: write to DuckLake.
        # Valid rows honor write_mode (append or upsert for delta files);
        # rejects always append.
        sink_props: dict = {
            "path": metadata_path,
            "dataPath": data_path,
            "tableName": table_name,
            "mode": write_mode if keep_valid else "append",
        }
        if keep_valid and write_mode == "upsert" and conflict_columns:
            sink_props["conflictColumns"] = conflict_columns
        p.sink("snk.ducklake", **sink_props)

        # Capture duckle's stdout to parse progress
        buf = io.StringIO()
        import sys as _sys
        _stdout = _sys.stdout
        _sys.stdout = buf
        try:
            p.run()
        finally:
            _sys.stdout = _stdout
        
        duckle_output = buf.getvalue()
        logger.info(
            "Pipeline %s → %s completed", os.path.basename(shapefile_path), table_name
        )
        
        # Parse node progress
        nodes = parse_duckle_output(duckle_output)
        
        # Notify callback
        if progress_callback:
            progress = PipelineProgress(
                phase="valid" if keep_valid else "invalid",
                file_index=0,
                total_files=1,
                file_name=os.path.basename(shapefile_path),
                nodes=nodes,
            )
            progress_callback(progress)
        
        return nodes
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_pipeline(
    shapefile_paths: list[str],
    dataset_name: str,
    column_mapping: list[dict],
    data_path: str | None = None,
    metadata_path: str | None = None,
    data_date: str | None = None,
    data_dates: list[str] | None = None,
    write_mode: str = "append",
    conflict_columns: list[str] | None = None,
    progress_callback: Optional[Callable[[PipelineProgress], None]] = None,
) -> PipelineResult:
    """
    Run the full pipeline for one dataset across all province files.

    For each province file:
      1. src.spatial → read shapefile (.zip unzipped first)
         (src.parquet + WKB decode for .parquet/.geoparquet)
      2. xf.dropcol → remove user-dropped columns
      3. xf.project → keep non-dropped columns
      4. xf.rename → apply user renames
      5. code.sql → add geom_wkb, bbox_xmin/ymin/xmax/ymax
      6. qa.geomvalidate → keep_valid
      7. snk.ducklake → append to {dataset} table

    Invalid geometries are captured in a second pass:
      qa.geomvalidate → keep_invalid → snk.ducklake → {dataset}_rejects

    Args:
        shapefile_paths: List of .shp or .zip paths to process.
        dataset_name: Name of the DuckLake table (e.g. "roads").
        column_mapping: [{original, renamed, drop}] from schema editor.
        data_path: DuckLake DATA_PATH directory.
        metadata_path: DuckLake metadata file path.

    Returns:
        PipelineResult with row counts, table name, and snapshot version.
    """
    result = PipelineResult(dataset=dataset_name, table_name=dataset_name)

    if not shapefile_paths:
        result.error = "No shapefiles provided"
        return result

    # Resolve absolute paths (default: anchored at the backend directory)
    data_path = os.path.abspath(data_path or str(_default_data_path()))
    metadata_path = os.path.abspath(metadata_path or str(_default_metadata_path()))

    # Ensure directories exist
    os.makedirs(data_path, exist_ok=True)
    os.makedirs(os.path.dirname(metadata_path), exist_ok=True)

    total_loaded = 0
    total_rejected = 0

    for idx, path in enumerate(shapefile_paths):
        if not os.path.exists(path):
            logger.warning("Skipping missing file: %s", path)
            continue

        file_name = os.path.basename(path)

        try:
            # Send initial pending state for valid pass
            if progress_callback:
                progress_callback(create_initial_progress(
                    idx, len(shapefile_paths), file_name, "valid"
                ))

            # Per-file data_date: vector overrides the single fallback
            file_data_date = (
                data_dates[idx] if data_dates and idx < len(data_dates) and data_dates[idx]
                else data_date
            )

            # Pass 1: valid rows → main table
            _run_single_file(
                path,
                dataset_name,
                column_mapping,
                data_path,
                metadata_path,
                keep_valid=True,
                data_date=file_data_date,
                write_mode=write_mode,
                conflict_columns=conflict_columns,
                progress_callback=progress_callback,
            )

            # Send initial pending state for invalid passes
            if progress_callback:
                progress_callback(create_initial_progress(
                    idx, len(shapefile_paths), name, "invalid"
                ))

            # Pass 2: invalid rows → rejects table
            _run_single_file(
                path,
                dataset_name,
                column_mapping,
                data_path,
                metadata_path,
                keep_valid=False,
                data_date=file_data_date,
                write_mode=write_mode,
                conflict_columns=conflict_columns,
                progress_callback=progress_callback,
            )

            result.files_processed += 1

        except duckle.DuckleError as e:
            msg = str(e)
            logger.error("Pipeline failed for %s: %s", path, msg)
            result.error = msg
            return result
        except Exception as e:
            logger.exception("Unexpected error processing %s", path)
            result.error = str(e)
            return result

    # Estimate row counts by querying DuckLake
    try:
        with ducklake_db(catalog=metadata_path) as db:
            total_loaded = db.execute(
                f"SELECT count(*) FROM vworld.{dataset_name}"
            ).fetchone()[0]

            try:
                total_rejected = db.execute(
                    f"SELECT count(*) FROM vworld.{dataset_name}_rejects"
                ).fetchone()[0]
            except Exception:
                pass  # rejects table may not exist if no invalid geometries
    except Exception as e:
        logger.warning("Could not query row counts: %s", e)

    result.rows_loaded = total_loaded
    result.rows_rejected = total_rejected

    logger.info(
        "Pipeline complete: %s → %d loaded, %d rejected (%d files)",
        dataset_name,
        total_loaded,
        total_rejected,
        result.files_processed,
    )

    return result


# ---------------------------------------------------------------------------
# Post-crawl operations
# ---------------------------------------------------------------------------

def post_crawl_compact(
    dataset_name: str,
    metadata_path: str | None = None,
    data_path: str | None = None,
) -> None:
    """
    Run post-crawl maintenance on a dataset table.

    Operations: merge adjacent Parquet files (compaction).
    Spatial indexing is not attempted — DuckLake does not support indexes;
    the bbox_* columns provide fast bounding-box filtering.

    Should be called after all province files for a dataset have been loaded.
    """
    with ducklake_db(catalog=metadata_path) as db:
        logger.info("Post-crawl: merging files for %s...", dataset_name)
        try:
            db.execute(
                "CALL ducklake_merge_adjacent_files('vworld', ?)", [dataset_name]
            )
        except Exception as e:
            logger.warning("Merge skipped: %s", e)

        logger.info("Post-crawl complete for %s", dataset_name)
