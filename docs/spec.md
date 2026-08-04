# Spec: VWorld Crawl Console

## Problem Statement

A geospatial analyst needs to periodically download and process shapefile data from the Korean VWorld portal. The portal requires login, hosts data across multiple paginated pages, and publishes updates as both full replacement files and incremental delta files. Currently, the analyst manually downloads `.zip` files, unzips them, edits column names, runs validation, and loads them into a local database — a repetitive, error-prone process. They need a pipeline console that automates discovery, download, transform, and load, with interactive control over schema mapping and visibility into geometry quality issues.

A companion Wails desktop application already exists for full GIS visualization — the console focuses on the data pipeline, not the map.

## Solution

A web-based pipeline operations console (React + FastAPI) that:

1. Authenticates to VWorld using user-provided credentials (or environment variables)
2. Walks paginated VWorld pages to discover downloadable shapefiles
3. Queues and downloads selected files with configurable concurrency
4. Presents an interactive schema editor for column rename/drop before load
5. Runs Duckle-compiled pipelines: `src.spatial → transform → validate → snk.ducklake`
6. Streams real-time progress, including row counts and geometry rejection events
7. Loads results into DuckLake (DuckDB catalog) with WKB encoding and bounding-box columns for downstream consumption
8. Provides post-crawl operations: compact, reindex, spatial index, snapshot expiry

A minimal map preview confirms pipeline output; the existing Wails desktop handles full GIS visualization.

## User Stories

1. As a geospatial analyst, I want to log in with my VWorld credentials in a browser, so that I can access protected shapefile data without managing environment variables manually.
2. As a geospatial analyst, I want my credentials auto-filled from environment variables when available, so that unattended server deployments work without manual login.
3. As a geospatial analyst, I want to enter the VWorld portal URL and see a paginated list of downloadable files, so that I know what data is available before downloading.
4. As a geospatial analyst, I want to toggle between auto-pagination (walk all pages automatically) and manual pagination (click "Next Page" myself), so that I can control the pace of discovery.
5. As a geospatial analyst, I want to skip the crawler entirely and select a local directory containing pre-downloaded `.zip` files, so that I can still use the pipeline when VWorld crawling is blocked or unavailable.
6. As a geospatial analyst, I want to see a progress indicator during auto-pagination (e.g., "Page 3 of 17"), so that I know how much discovery is remaining.
7. As a geospatial analyst, I want to stop discovery mid-walk, so that I can inspect files found so far without waiting for all pages.
8. As a geospatial analyst, I want to see discovered files (or local directory files) in a grid with name, size, last-modified date, and status, so that I can decide which files to process.
9. As a geospatial analyst, I want to select files via checkboxes with a "Select All" action and a text filter, so that I can efficiently choose files from a large list.
10. As a geospatial analyst, I want to set a batch size for concurrent downloads (when crawling), so that I can control the load on the VWorld server and my local machine. When loading from a local directory, files are processed sequentially at full speed with no batch limit needed.
11. As a geospatial analyst, I want to see per-file progress in a queue (download progress when crawling, extraction progress when loading locally), so that I know which files are downloading and which are waiting.
12. As a geospatial analyst, I want to stop the queue mid-progress, so that I can cancel an operation without leaving partial state.
13. As a geospatial analyst, I want the crawler to respect VWorld's server by using a real browser User-Agent, randomized request delays, and a limited batch size, so that the crawl is indistinguishable from normal browsing behavior.
14. As a geospatial analyst, I want the crawler to persist its state (URLs, ETags, timestamps) after each crawl, so that re-crawling only downloads changed files.
15. As a geospatial analyst, I want the selected files automatically unzipped and their schemas detected (columns, types, CRS), so that I can review the data structure before loading, regardless of whether files came from the crawler or a local directory. (columns, types, CRS), so that I can review the data structure before loading.
16. As a geospatial analyst, I want to see the raw schema from the shapefile and interactively rename columns (click column → type new name) and drop columns (toggle off), so that I control the output schema.
17. As a geospatial analyst, I want to preview the first rows with my schema mapping applied, so that I can verify my column choices before running the pipeline.
18. As a geospatial analyst, I want to run the pipeline and see real-time progress per node (source reading, renaming, validation, sink writing), so that I know what the pipeline is doing.
19. As a geospatial analyst, I want to see a live reject counter during geometry validation (e.g., "47 rows rejected"), so that I'm aware of data quality issues as they happen.
20. As a geospatial analyst, I want rejects saved to a `{dataset}_rejects` table with error reasons, so that no data is silently lost and I can investigate failures.
21. As a geospatial analyst, I want to see a summary after the pipeline completes (rows loaded, rows rejected, tables updated), so that I have a clear outcome.
22. As a geospatial analyst, I want each dataset's province files appended into a single DuckLake table, so that I get one country-wide table per dataset without manual merging.
23. As a geospatial analyst, I want every row encoded with WKB geometry and bounding-box columns, so that my Wails desktop app can consume the data efficiently.
24. As a geospatial analyst, I want the pipeline to add a `data_date` column populated from delta file metadata, so that I can run time-series queries.
25. As a geospatial analyst, I want to browse all DuckLake tables with their latest snapshot version, file count, and total size, so that I can monitor my data lakehouse.
26. As a geospatial analyst, I want to view the snapshot timeline for a table (all versions with timestamps), so that I can understand the history of schema and data changes.
27. As a geospatial analyst, I want to trigger compact and reindex operations on a table from the GUI, so that I can optimize query performance after a large append.
28. As a geospatial analyst, I want to trigger a spatial index (RTREE) on a table from the GUI, so that bounding-box queries in the Wails desktop are fast.
29. As a geospatial analyst, I want to expire old snapshots and reclaim disk space, so that storage doesn't grow unbounded.
30. As a geospatial analyst, I want to browse a reject table and inspect individual failed rows with their error reasons, so that I can decide whether to fix or ignore them.
31. As a geospatial analyst, I want a minimal map preview showing feature count and bounding box for a selected table, so that I can quickly confirm the pipeline output without opening the Wails desktop.
32. As a geospatial analyst, I want the GUI to show my login session status (authenticated / expired), so that I know if I need to re-authenticate before crawling.
33. As a geospatial analyst, I want the console to work as a single-user application with a DuckDB catalog (no PostgreSQL dependency), so that I have zero operational overhead to get started.

## Implementation Decisions

### Architecture

- **Web app, not desktop.** React + shadcn/ui frontend, Python FastAPI backend. A separate Go/Wails desktop handles full GIS visualization. The web console focuses on pipeline operations.
- **Duckle as library, not fork.** Duckle (`pip install duckle>=0.5.9,<0.6`) provides all ETL components. A thin `pipeline/runner.py` module wraps all Duckle calls so API changes are localized.
- **DuckDB as DuckLake catalog.** `ATTACH 'ducklake:vworld_metadata.ducklake'` — zero external dependencies. A catalog abstraction (`VWORLD_CATALOG_TYPE` env var) allows PostgreSQL swap later.

### Authentication

- **Dual-entry login screen:** the first screen offers two choices — "Connect to VWorld" (URL + username + password fields) or "Load from local directory" (directory picker). No credentials needed for local mode.
- When "Connect to VWorld" is selected, GUI login screen with URL, username, password fields. Credentials held in memory only.
- **Environment variable fallback:** `VWORLD_URL`, `VWORLD_USERNAME`, `VWORLD_PASSWORD` auto-populate the login fields. Enables unattended/CI deployment.
- **Session manager:** httpx client with cookie persistence. Detects session expiry and re-authenticates. Single client reused across all requests.

### Crawler

- **Dual source:** the user chooses between "Crawl from URL" (VWorld login + paginated discovery) and "Load from directory" (local filesystem path with `.zip` files). Both modes feed the same downstream pipeline.
- **Local directory mode:** user selects a local directory path. The app scans for `.zip` files, displays them in the same file grid used for crawl discovery, and proceeds directly to unzip + schema detection. No download queue — files are processed sequentially at full speed.
- **Discovery:** paginated walk with configurable auto/manual mode. Link selectors and file patterns configurable per crawl.
- **Download queue:** user-configurable batch size (default 5). Files beyond batch wait in queue; completed downloads release a slot. Not applicable in local directory mode.
- **Respectful crawling:** browser-like User-Agent, randomized 1–3s delays between page requests, `robots.txt` honored with `Crawl-Delay` support.
- **State persistence:** `crawl_state` table tracks URL, ETag, Last-Modified per file. Re-crawl compares against state and downloads only changed files. Not applicable in local directory mode.
- **Delta detection:** delta files identified by date metadata in filename. Applied as upsert merge with `data_date` column populated. In local directory mode, `data_date` is inferred from file modification time if no metadata is present.

### Schema Editor

- Raw schema auto-detected from shapefile (columns, types, CRS).
- Columns displayed with editable name field and toggle to drop.
- Preview shows N rows with current mapping applied.
- Mapping choices drive Duckle's `xf.rename` and `xf.project` calls.

### Pipeline

- One dataset = one DuckLake table. Province files appended via `write_mode="append"`.
- Schema evolves across DuckLake snapshots: v1 = raw VWorld schema, v2 = cleaned schema, etc.
- Per-row enrichment: `ST_AsWKB(geom)` for WKB encoding, `ST_XMin/YMin/XMax/YMax` for bounding-box columns, `data_date` from delta metadata.
- Geometry validation split: valid rows → main table, invalid rows → `{dataset}_rejects` table.
- Post-crawl: compact (merge Parquet files), reindex (rebuild metadata), spatial index (RTREE on geometry).

### Real-time Progress

- WebSocket endpoint streams per-node status, row counts, and reject events during pipeline execution.
- Frontend displays: node status (pending/running/done), rows processed, reject count.
- Final summary after pipeline completes.

### DuckLake Console

- Table list: name, latest snapshot version, file count, total size.
- Snapshot timeline per table with timestamps.
- Operations: Compact, Reindex, Spatial Index, Expire Snapshots — each with confirmation and result feedback.
- Reject table browser: preview failed rows with error reasons.

### Map Preview

- Minimal: feature count + bounding box on a lightweight map.
- Purpose: confirmation, not analysis. Wails desktop handles full GIS.

## Testing Decisions

### Seam Strategy

Tests are written at the highest possible seam — the FastAPI route — exercising all layers:

| Seam | What's tested | Tool |
|------|--------------|------|
| FastAPI routes | Full HTTP request → response cycle | `httpx.AsyncClient` against test app |
| WebSocket | Real-time progress events received by client | Test WebSocket client, assert event stream |
| Pipeline runner | Duckle graph built with correct components and properties | Mock Duckle API, assert `pipeline.to_dict()` |
| DuckLake ops | Generated SQL for compact/reindex/spatial index | In-memory DuckDB + DuckLake extension |
| Crawler | Link discovery and file download logic | HTML fixtures for VWorld pages, mocked HTTP transport |

### Test Principles

- Test external behavior, not implementation details. If the pipeline delivers correct rows to DuckLake, the test passes regardless of how the Duckle graph is built.
- One integration test at the FastAPI route level covers crawler → pipeline → DuckLake end-to-end using a small test shapefile.
- Crawler tests use static HTML fixtures that match VWorld page structure — changes to the VWorld HTML break the tests and signal that the crawler selectors need updating.
- WebSocket tests verify event ordering and content, not socket-level implementation.

## Out of Scope

- **Full GIS visualization** — handled by the existing Wails desktop app.
- **Scheduled/automatic crawls** — manual trigger only for MVP. Duckle's `ctl.schedule` can be added later.
- **Multi-user support** — single-user console with DuckDB catalog. PostgreSQL migration path designed but not implemented.
- **Forking or modifying Duckle source** — all components consumed via the Python API.
- **Schema inference for non-shapefile formats** — only shapefiles in MVP. Duckle supports GeoJSON/GeoPackage/KML natively if needed later.
- **Automatic schema mapping** — column rename/drop is interactive. No ML-based column name inference.
- **Batch editing of schema across multiple datasets** — one dataset's schema at a time.
- **Download resume for interrupted files** — downloads restart from beginning. Full-file resume support deferred.

## Further Notes

- **Open questions:** concrete VWorld URLs, exact auth mechanism (form login vs API key), delta vs full file detection method — these will be resolved during the first crawl against the real VWorld portal and may require spec amendments.
- **Duckle's beta status:** Duckle v0.5.x is beta. The version pin (`>=0.5.9,<0.6`) and `pipeline/runner.py` abstraction layer contain this risk.
- **DuckLake v1.0:** production-ready with backward compatibility guarantees. Parquet data files are portable to Iceberg/Delta if DuckLake is ever swapped.
- **Credentials:** never persisted to disk. Held in memory for the session duration. Environment variables are the only persistent credential mechanism.
