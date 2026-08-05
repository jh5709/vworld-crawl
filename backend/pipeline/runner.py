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
from db import ducklake_db, metadata_path as _default_metadata_path
from schema_detector import (
    _get_conn,
    _is_parquet,
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
    drop_cols = [c["original"] for c in column_mapping if c.get("drop")]
    keep_cols = [c["original"] for c in column_mapping if not c.get("drop")]
    # Always include geom (needed for WKB, bbox, and validation)
    if "geom" not in keep_cols:
        keep_cols.append("geom")
    rename_map = {
        c["original"]: c["renamed"]
        for c in column_mapping
        if c.get("renamed") and not c.get("drop")
    }

    mode = "valid" if keep_valid else "invalid"
    table_name = dataset_name if keep_valid else f"{dataset_name}_rejects"

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

        # 2. Drop columns: OGC_FID + user-dropped
        if drop_cols:
            p.transform("xf.dropcol", columns=drop_cols)

        # 3. Project: keep only non-dropped columns (+ geom)
        p.transform("xf.project", columns=keep_cols)

        # 4. Rename: apply user renames (use Pipeline.rename, not xf.rename)
        if rename_map:
            p.rename(**rename_map)

        # 5. SQL: add WKB + bounding box columns
        p.transform(
            "code.sql",
            sql="""
            SELECT *,
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

        # 7. Sink: write to DuckLake
        p.sink(
            "snk.ducklake",
            path=metadata_path,
            dataPath=data_path,
            tableName=table_name,
            mode="append",
        )

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

            # Pass 1: valid rows → main table
            _run_single_file(
                path,
                dataset_name,
                column_mapping,
                data_path,
                metadata_path,
                keep_valid=True,
                progress_callback=progress_callback,
            )

            # Send initial pending state for invalid pass
            if progress_callback:
                progress_callback(create_initial_progress(
                    idx, len(shapefile_paths), file_name, "invalid"
                ))

            # Pass 2: invalid rows → rejects table
            _run_single_file(
                path,
                dataset_name,
                column_mapping,
                data_path,
                metadata_path,
                keep_valid=False,
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
