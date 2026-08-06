# Crawler Architecture: Files-on-Disk + Threaded Discovery + Public Mode

The crawler downloads geospatial files to disk as intermediate storage (not DuckDB tables), runs discovery in background threads (not blocking the event loop), and supports both authenticated (VWorld login) and public (no-auth) modes.

**Status:** accepted

## Considered Options

### Intermediate storage: DuckDB tables vs. files on disk

- **DuckDB tables (rejected):** Download directly into a single `downloads.db` DuckDB file with one table per dataset. This would eliminate raw files but DuckDB is single-writer — parallel batch downloads (5 threads) would contend on the same file, causing write conflicts. Also loses raw format fidelity (original `.geojson` text is gone, replaced by DuckDB's typed representation), and removes the ability to inspect/debug the source file independently.

- **Files on disk (accepted):** Each downloaded file is saved to `downloads/` as its original format (`.geojson`, `.shp`, `.zip`, etc.). The file is the clean interface boundary between crawler and pipeline. Parallel downloads write to independent files (no contention). The raw file on disk is the source of truth — the pipeline can be re-run from it, and it can be opened in any GIS tool for inspection.

Rejected alternative: duckle `src.duckdb` + `snk.duckdb` pipeline chain. Would require two duckle runs per file (download-to-db then db-to-DuckLake), doubles overhead, and still has a DuckDB file on disk (just a different format).

### Discovery threading: blocking sync vs. background thread

- **Blocking sync in async endpoints (rejected — original implementation):** `discover_all_pages()` called synchronously inside `async def api_crawler_discover()`. The event loop was blocked for the entire discovery duration (minutes across 17 pages). The `/api/crawler/discover/stop` endpoint could never be processed because the loop was frozen. All other API routes were unresponsive.

- **ThreadPoolExecutor with asyncio.wrap_future (accepted):** Discovery runs in a `ThreadPoolExecutor` thread. `asyncio.wrap_future(future)` awaits the result without blocking the event loop. The Stop endpoint sets a `stopped` flag on the shared `DiscoveryState` object, checked between pages. `future.cancel()` is also called as a fallback. Pattern matches the existing pipeline WebSocket handler.

### Session management: single VWorldSession vs. CrawlSession with modes

- **VWorldSession (replaced):** Always required login. Login grabbed the first `<form>` on the page (often a search form, not the login form). Re-login leaked old httpx clients. No public mode.

- **CrawlSession (accepted):** Single class with `auth_required: bool` parameter. When `False`, creates a plain httpx client with no login step — enables testing against public catalogues (geojson.xyz, data.nextgis.com) and non-VWorld sources. When `True`, finds the login form by locating `input[type=password]` then walking up to the parent `<form>`, closes old clients before re-auth, and verifies auth by checking for redirect-to-login on a target page.

### Crawl state storage: DuckLake vs. plain DuckDB

- **DuckLake (rejected):** The `crawl_state` table was initially created in the DuckLake catalog with a `PRIMARY KEY` constraint. DuckLake does not support PRIMARY KEY (`NotImplementedException`), and without it `INSERT OR REPLACE` requires a UNIQUE constraint which DuckLake also doesn't support (`BinderException`). Additionally, the table appeared as a user table in the DuckLake Console (alongside roads, buildings) with compact/reindex buttons that made no sense for app metadata.

- **Plain DuckDB catalog (accepted):** `crawl_state` is now stored in `catalog/vworld_catalog.db` (the same DuckDB file used by `catalog.py` for the catalog abstraction). This is the principled choice: DuckLake is for geospatial data tables; the DuckDB catalog is for app-internal metadata. Uses `DELETE` + `INSERT` for upsert semantics with a `UNIQUE INDEX` on the URL column.

### Filename handling: raw vs. sanitized

- **Raw remote names (rejected — security risk):** `Path(download_dir) / file.name` with no sanitization. A malicious server could return `../../.bashrc` as a filename, writing outside the download directory.

- **Sanitized (accepted):** `_sanitize_filename()` strips all directory components and leading dots. The basename alone is used. This is a defense-in-depth measure alongside the single-user localhost binding.

### Respectful crawling: advisory vs. enforced

- **Advisory only (rejected — original implementation):** `check_robots()` logged a warning on disallow but the caller fetched the URL anyway. The robots.txt UA was `Python-urllib` while requests used Chrome's UA — mismatched identities.

- **Enforced (accepted):** `check_robots()` raises `RobotsDisallowed` on explicit disallow. The same Chrome UA is used for both robots.txt fetches and page requests (consistent identity). A `MAX_PAGES=100` cap and visited-URL set prevent infinite loops. Randomized 1–3s delays apply between pages (in the page-walk loop, not re-applied per-page by the inner fetch function).

## Consequences

- Discovery is responsive: Stop works, other API routes work. Tested against geojson.xyz (133 files, ~3 seconds).
- The crawler works against any HTTP-served spatial file listing, not just VWorld. Public mode enables community data sources.
- Download parallelism is preserved (batch of 5 writes to 5 separate files, no lock contention).
- Filename sanitization is a safety net; the app binds to `127.0.0.1` by default (additional protection).
- The dual-catalog approach (DuckDB for metadata, DuckLake for data) sets a pattern for future app-level tables.
- **Terminology: DuckLake storage vs. Download staging.** The downloads folder is "Download staging" — a temporary holding area. Files flow: Staged (not yet loaded) → Loaded into a table (still in staging) → Cleared (deleted from staging; row kept for re-crawl). Per-table, originals are shown under "Originals in staging." One downloaded file is attributed to one dataset (the most recent pipeline that consumed it); running a new pipeline on the same file moves the attribution.
- duckle's `src.duckdb` + `snk.duckdb` are available for future optimization if single-writer contention is addressed, but are not needed for the current file-based flow.
- **CRS reprojection (step 1.5):** Source CRS is auto-detected via `ST_CRS(geom)` (GDAL) or GeoParquet `geo` metadata. When the source CRS differs from EPSG:4326, a `code.sql` transform uses `ST_Transform(geom, src, 'EPSG:4326', always_xy:=true)` — `always_xy` is critical to prevent PROJ's authority-compliant lat/lon axis swap. Unknown CRS (no `.prj`) logs a warning and passes through unchanged. All DuckLake tables therefore store WGS84 lon/lat — map preview and Wails desktop consume them with no per-table CRS handling.
