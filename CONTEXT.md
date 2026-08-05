# VWorld Crawl

A geospatial data pipeline console that crawls VWorld web pages to discover, download, and process geospatial files (shapefiles, GeoJSON, GeoPackage, GeoParquet), loading them into DuckLake for querying and time-series analysis. A companion Wails desktop app provides full GIS visualization.

## Language

**Crawl**:
A session that discovers downloadable geospatial file links from one or more paginated VWorld web pages.
_Avoid_: Scrape, spider

**Discovery**:
The first phase of a crawl — walks paginated pages, accumulates a file list. No files are downloaded yet.
_Avoid_: Scan, enumerate

**Dataset**:
A logical grouping of files that share the same schema (e.g., roads, buildings, parcels). One dataset = one DuckLake table. A dataset may span multiple provinces, each delivered as a separate file (`.zip`, `.shp`, `.geojson`, `.gpkg`, `.parquet`).
_Avoid_: Layer, theme, category

**Province file**:
A single file containing the geospatial data for one province within a dataset. Appended into the dataset's DuckLake table. Supported formats: `.shp`, `.geojson`, `.gpkg` (read via GDAL/ST_Read), `.parquet`/`.geoparquet` (read via DuckDB's `read_parquet` with WKB geometry decoding), and `.zip` (unzipped to the first supported spatial file inside).
_Avoid_: Tile, partition, chunk

**Source file**:
A supported geospatial file on disk, auto-detected by extension. When a `.zip` archive contains multiple files, the first spatial file (priority: `.shp` → `.gpkg` → `.geojson` → `.parquet`) is used.
_Avoid_: Input file, raw file

**Table**:
A DuckLake table representing one dataset. Schema evolves across DuckLake snapshots (v1 = raw VWorld schema, v2 = cleaned schema with renamed columns, etc.).
_Avoid_: Collection, layer

**Snapshot**:
A DuckLake point-in-time version of a table. Created automatically on every pipeline run. Supports time travel queries (`AT SNAPSHOT 'v17'`).
_Avoid_: Version tag, revision

**Raw schema**:
The column names and types exactly as VWorld publishes them (e.g., `RN`, `ROAD_NAME`, `LENG`). Stored in the table's earliest snapshots.
_Avoid_: Source schema, original

**Clean schema**:
The column names and types after user-applied transforms (rename, drop). Column mapping is configured interactively in the GUI and applied via Duckle's `xf.rename` and `xf.project`.
_Avoid_: Target schema, transformed, curated

**Transform config**:
The user's column mapping choices — which columns to rename (old → new) and which to drop. Applied during the pipeline; the raw columns remain accessible via time travel to earlier snapshots.
_Avoid_: Mapping file, schema config

**Rejects**:
Rows whose geometry fails validation (`qa.geomvalidate`). Saved to a `{dataset}_rejects` table with error reasons. The user sees reject counts in real time during pipeline execution and in the final summary.
_Avoid_: Errors, dead-letter, quarantine

**Full file**:
A province file containing the complete dataset for that province. Replaces or appends all rows.
_Avoid_: Complete, snapshot file

**Delta file**:
A province file containing only changed rows (inserts, updates, deletes) since a prior publication date. Identified by date metadata in the filename or companion metadata. Merged via Duckle upsert.
_Avoid_: Incremental, diff, patch

**Data date**:
The publication date of a delta file, stored as an explicit `data_date` column in the table. Enables time-series queries (`WHERE data_date BETWEEN '2025-01' AND '2025-03'`).
_Avoid_: Version date, effective date, as-of date

**Pipeline**:
A Duckle-compiled execution graph. For GDAL formats (`.shp`, `.geojson`, `.gpkg`): `src.spatial → … → snk.ducklake`. For GeoParquet (`.parquet`, `.geoparquet`): `src.parquet → WKB decode → … → snk.ducklake`. Defined in Python via the Duckle API, triggered from the web GUI.
_Avoid_: Job, workflow, DAG

**Post-crawl**:
Operations that run once after all province files are appended: compact (merge adjacent Parquet files), rewrite data files (rebuild metadata). Spatial indexes are not available on DuckLake tables — the per-row `bbox_*` columns and Parquet zone maps provide bounding-box filtering instead. RTREE indexes are created at read time by the Wails desktop when it materializes a table locally.
_Avoid_: Finalize, optimize, cleanup

**Wails desktop**:
A separate Go/Wails desktop application that reads DuckLake tables directly for full GIS visualization. The web app provides only minimal map preview for pipeline confirmation.
_Avoid_: GIS viewer, map client

## Crawler Concepts

**Batch size**:
Maximum number of concurrent file downloads. User-configurable in the GUI (default: 5). Respects VWorld server resources and bounds memory usage.
_Avoid_: Concurrency limit, pool size

**Queue**:
The ordered list of selected files awaiting download. Files beyond the batch size wait in queue; completed downloads release a slot for the next queued file.
_Avoid_: Backlog, pending

**Auto-pagination**:
Discovery mode that walks all pages automatically until exhausted, streaming progress to the GUI ("Page 3 of 17").
_Avoid_: Full scan, auto-walk

**Manual pagination**:
Discovery mode where the user clicks "Next Page" to control pace. Useful for inspecting page contents before proceeding.
_Avoid_: Step mode, page-by-page

**Crawl state**:
Persistent record of last-known file URLs, ETags, and Last-Modified timestamps. Used to detect changed files on re-crawl. Stored in a `crawl_state` DuckDB table.
_Avoid_: Catalog, registry

## Encoding

**WKB**:
Well-Known Binary geometry encoding. Every row in the DuckLake table includes a `geom_wkb` column for direct consumption by the Go/Wails desktop app (no GeoJSON parsing overhead).
_Avoid_: Binary geometry, hex geometry

**Bounding box columns**:
Per-row `bbox_xmin`, `bbox_ymin`, `bbox_xmax`, `bbox_ymax` columns for fast spatial filtering without deserializing the full geometry. These columns are the primary spatial access path: DuckLake does not support native indexes (RTREE, ART), so Parquet zone maps (min/max statistics) on the bbox columns serve as the lakehouse equivalent of a spatial index, pruning row groups during filtered scans.
_Avoid_: Envelope, extent

**GeoParquet**:
An OGC standard for encoding geospatial vector data in Apache Parquet files. Geometry is stored as a WKB blob column with `geo` key-value metadata specifying the primary geometry column and CRS. DuckDB-written native GEOMETRY columns are also supported. Read via DuckDB's `read_parquet` + `ST_GeomFromWKB` when the column is a WKB BLOB, or used directly when it is already a GEOMETRY type.
_Avoid_: Spatial Parquet, geospatial parquet

**Catalog**:
The DuckLake metadata file (`ducklake_metadata.ducklake`) plus its data directory (`.ducklake.files/`). All console operations (table list, snapshots, compact, expire) target this catalog. Paths are resolved relative to the backend directory, not the current working directory.
_Avoid_: Database, lake
