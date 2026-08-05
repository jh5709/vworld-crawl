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
from dataclasses import dataclass, field

import duckle

from schema_detector import _unzip_shapefile

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
) -> int:
    """
    Run the pipeline for a single province file.

    Args:
        shapefile_path: Path to .shp or .zip.
        dataset_name: DuckLake table name.
        column_mapping: [{original, renamed, drop}] from schema editor.
        data_path: DuckLake DATA_PATH directory.
        metadata_path: DuckLake metadata file path.
        keep_valid: True → valid rows go to {dataset}; False → invalid rows go to {dataset}_rejects.

    Returns:
        Number of rows written.
    """
    # Build drop, keep, rename maps
    drop_cols = ["OGC_FID"] + [c["original"] for c in column_mapping if c.get("drop")]
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

    # Handle .zip files: unzip to temp dir, pass .shp to duckle
    read_path = shapefile_path
    tmpdir = None
    if shapefile_path.lower().endswith(".zip"):
        read_path = _unzip_shapefile(shapefile_path)
        tmpdir = os.path.dirname(read_path)

    p = duckle.Pipeline()

    try:
        # 1. Source: read shapefile
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

        result = p.run()
        logger.info(
            "Pipeline %s → %s: %s", os.path.basename(shapefile_path), table_name, result
        )
        return 0
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
    data_path: str = "vworld_data/",
    metadata_path: str = "catalog/ducklake_metadata.ducklake",
) -> PipelineResult:
    """
    Run the full pipeline for one dataset across all province files.

    For each province file:
      1. src.spatial → read shapefile
      2. xf.dropcol → remove OGC_FID + user-dropped columns
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

    # Resolve absolute paths
    data_path = os.path.abspath(data_path)
    metadata_path = os.path.abspath(metadata_path)

    # Ensure directories exist
    os.makedirs(data_path, exist_ok=True)
    os.makedirs(os.path.dirname(metadata_path), exist_ok=True)

    total_loaded = 0
    total_rejected = 0

    for path in shapefile_paths:
        if not os.path.exists(path):
            logger.warning("Skipping missing file: %s", path)
            continue

        try:
            # Pass 1: valid rows → main table
            _run_single_file(
                path,
                dataset_name,
                column_mapping,
                data_path,
                metadata_path,
                keep_valid=True,
            )

            # Pass 2: invalid rows → rejects table
            _run_single_file(
                path,
                dataset_name,
                column_mapping,
                data_path,
                metadata_path,
                keep_valid=False,
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
        import duckdb

        db = duckdb.connect(":memory:")
        db.execute("INSTALL ducklake; LOAD ducklake;")
        db.execute(
            f"ATTACH 'ducklake:{metadata_path}' AS vworld"
        )

        # Count valid rows
        valid = db.execute(
            f"SELECT count(*) FROM vworld.{dataset_name}"
        ).fetchone()
        if valid:
            total_loaded = valid[0]

        # Count rejected rows
        try:
            invalid = db.execute(
                f"SELECT count(*) FROM vworld.{dataset_name}_rejects"
            ).fetchone()
            if invalid:
                total_rejected = invalid[0]
        except Exception:
            pass  # rejects table may not exist if no invalid geometries

        db.close()
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
    metadata_path: str = "catalog/ducklake_metadata.ducklake",
    data_path: str = "vworld_data/",
) -> None:
    """
    Run post-crawl maintenance on a dataset table.

    Operations:
      1. Merge adjacent Parquet files (compaction)
      2. RTREE spatial index for fast bounding-box queries

    Should be called after all province files for a dataset have been loaded.
    """
    import duckdb

    db = duckdb.connect(":memory:")
    try:
        db.execute("INSTALL spatial; LOAD spatial;")
        db.execute("INSTALL ducklake; LOAD ducklake;")
        db.execute(
            f"ATTACH 'ducklake:{metadata_path}' AS vworld"
        )

        logger.info("Post-crawl: merging files for %s...", dataset_name)
        try:
            db.execute(f"CALL ducklake_merge_adjacent_files('{dataset_name}', 'vworld')")
        except Exception as e:
            logger.warning("Merge skipped: %s", e)

        logger.info("Post-crawl: creating spatial index on %s...", dataset_name)
        try:
            db.execute(
                f"CREATE INDEX IF NOT EXISTS {dataset_name}_rtree "
                f"ON vworld.{dataset_name} USING RTREE(geom)"
            )
        except Exception as e:
            logger.warning("Spatial index skipped (DuckLake may not support indexes): %s", e)

        logger.info("Post-crawl complete for %s", dataset_name)
    finally:
        db.close()
