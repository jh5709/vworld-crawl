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
A province file containing only changed rows (inserts, updates, deletes) since a prior publication date. Identified by date metadata in the filename (e.g., `roads_20250801.zip`) or companion metadata. Merged via Duckle upsert with a `data_date` column and an upsert key column. The Schema Editor's "Advanced — delta load" section exposes the toggle, date, and key-column selector.
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

**Crawl session**:
An HTTP session to a geospatial data portal. Two modes: **login** (authenticated — stores encrypted credentials in memory, re-auths on expiry) and **public** (no-auth — connects to public data catalogues like geojson.xyz or data.nextgis.com). Managed by `CrawlSession` in `backend/crawler/session.py`.
_Avoid_: Connection, client

**Public mode**:
No-authentication discovery for public data catalogues. The user enters a URL (no credentials) and clicks Connect. The crawler uses a plain httpx client with no login step. Introduced in #39 to enable testing and non-VWorld sources.
_Avoid_: Anonymous, guest

**Connected**:
Generic session state — either authenticated (login mode) or ready (public mode). The GUI shows a green shield with "Connected" or "Authenticated". Session status is polled every 30 seconds.
_Avoid_: Logged in, active

**Respectful crawling**:
Policies to minimize server impact: browser-like User-Agent (Chrome/Linux), randomized 1–3s delays between page requests, and robots.txt honored (disallowed URLs are refused, not just warned). Crawl-Delay from robots.txt is applied when specified. Managed by `backend/crawler/respect.py`.
_Avoid_: Rate limiting, throttling

**Batch size**:
Maximum number of concurrent file downloads. User-configurable in the GUI (default: 5 for portals, 3 for testing). Respects VWorld server resources and bounds memory usage.
_Avoid_: Concurrency limit, pool size

**Queue**:
The ordered list of selected files awaiting download. Files beyond the batch size wait in queue; completed downloads release a slot for the next queued file.
_Avoid_: Backlog, pending

**Auto-pagination**:
Discovery mode that walks all pages automatically until exhausted, streaming progress to the GUI ("Page 3 of 17"). Discovery runs in a background thread (ThreadPoolExecutor) so the event loop stays responsive and Stop works. Limited to 100 pages max with visited-URL tracking to prevent infinite loops.
_Avoid_: Full scan, auto-walk

**Manual pagination**:
Discovery mode where the user clicks "Next Page" to advance. Each click fetches one page; accumulated files persist across pages. Uses a stored DiscoveryState for page tracking.
_Avoid_: Step mode, page-by-page

**Crawl state**:
Persistent record of last-known file URLs, ETags, and Last-Modified timestamps per downloaded file. Stored in the plain DuckDB catalog (`catalog/vworld_catalog.db`) — NOT in DuckLake — since this is app metadata, not lakehouse data. Uses DELETE + INSERT (DuckLake does not support PRIMARY KEY or INSERT OR REPLACE). Checked on re-crawl to detect changed files. Managed by `backend/crawler/state.py`.
_Avoid_: Catalog, registry

**Sanitized filename**:
Remote file names are sanitized before writing to disk to prevent path traversal attacks (e.g., `../../etc/passwd`). Directory components and `..` are stripped; only the basename is kept. Applied automatically by `_sanitize_filename()` in `backend/crawler/download.py`.

**Download progress**:
Per-file progress streamed via WebSocket during batch downloads. Each file reports: status (queued/downloading/done/failed/stopped), progress (0.0–1.0, or null for indeterminate unknown-size files), downloaded bytes, local path, ETag, and Last-Modified. The GUI renders progress bars with percentage labels; indeterminate files show an animated pulse bar with "...".

**Crawler stop**:
Both discovery and download support cancellation. Discovery stop sets a flag checked between pages (with future cancellation for thread-based runs). Download stop sets a flag checked per chunk (64KB). Stopped operations preserve accumulated files and completed downloads.

**Re-crawl**:
An incremental crawl that re-discovers all files but downloads only those that changed. Compares each URL against crawl_state by fetching a real file ETag/Last-Modified via HEAD requests (capped at 50, 0.2s pacing). Returns three buckets: new, updated, unchanged. The GUI shows a change summary panel and pre-selects new+updated files. Delta file dates are auto-detected from filenames (YYYYMMDD or YYYY-MM-DD patterns). Endpoint: `POST /api/crawler/recrawl`.
_Avoid_: Refresh, re-discover

**HEAD check**:
A lightweight HTTP HEAD request to an individual file URL to retrieve its current ETag and Last-Modified, used during re-crawl to compare against stored crawl_state values. The listing page's own ETag is NOT the file's ETag, so per-file checks are required for accurate change detection. Limited to 50 requests at 0.2s intervals.
_Avoid_: Probe, etag fetch

**Upsert merge**:
A pipeline write mode (`write_mode="upsert"`) that replaces rows matching a conflict (upsert key) column and inserts new rows. Delta files use this so updated features replace prior versions instead of duplicating. Reject tables always append regardless of mode.
_Avoid_: Merge-insert, overwrite

**Reprojection**:
Automatic CRS transformation at pipeline step 1.5 (after source, before transforms). Source CRS detected via `ST_CRS(geom)` for GDAL formats or GeoParquet `geo` metadata. If source CRS differs from EPSG:4326, a `code.sql` step uses `ST_Transform(geom, src, 'EPSG:4326', always_xy:=true)` — the `always_xy` flag prevents PROJ's authority-compliant lat/lon axis swap. Unknown CRS logs a warning and passes through. All DuckLake tables store WGS84 lon/lat.
_Avoid_: CRS convert, transform

**Map preview**:
A deck.gl map overlay opened from the DuckLake console (green map-icon per table). Shows the full-table bounding box (dashed yellow). User toggles draw mode for rectangle selection; row limits adapt to geometry type (points 10k, lines 2k, polygons 500). Bright layers (cyan/fuchsia/lime) render on CARTO dark basemap tiles. Side panel: Statistics tab (column stats with expandable histograms) and Attributes tab (paginated rows with spatial filter). Endpoints: `/api/tables/{name}/bounds`, `/stats`, `/features`, `/attributes`, `/histogram`.
_Avoid_: GIS viewer, mini-map

**DuckLake storage**:
The lakehouse proper — DuckLake tables, their Parquet data files, and snapshots. Permanent and queryable. Distinguished from download staging in the console's storage summary.
_Avoid_: Database, lake

**Download staging**:
The crawler's download directory — a temporary holding area for original files before and after they are loaded into DuckLake storage. The DuckLake console shows a staging summary (file count, total size) and lists staged files not yet loaded into any table.
_Avoid_: Inbox, temp dir

**Staged**:
A downloaded file in staging that no pipeline has consumed yet (crawl_state `dataset_name` is empty). Shown under "Not yet loaded" in the console with Load-into-table and Delete actions.
_Avoid_: Unlinked, orphaned

**Loaded**:
A staged file that a pipeline has consumed into a DuckLake table. crawl_state attributes each file to the most recent table that consumed it (one file = one table). The original stays in staging until cleared.
_Avoid_: Linked

**Cleared**:
A staged original deleted from disk (via Clear staging on a table, or Delete in the staging section). The crawl_state row is kept with status `cleaned` so the URL, ETag, and Last-Modified survive for re-download and re-crawl change detection.
_Avoid_: Purged, cleaned up

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
