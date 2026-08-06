# VWorld Crawl — Architecture (Post-Grilling)

> Sharpened through `/grill-with-docs` session. Read alongside [CONTEXT.md](./CONTEXT.md) and [docs/adr/](./docs/adr/).

## 1. System Overview

```
┌──────────────────────────────────────────────────────────────────┐
│               Web Console (React + FastAPI)                      │
│                                                                   │
│  ┌─────────────┐  ┌───────────────┐  ┌────────────────────────┐  │
│  │ Crawler     │  │ Schema Editor │  │ DuckLake Console       │  │
│  │ Login/Public│  │ (rename/drop  │  │ Tables, snapshots,     │  │
│  │ Discovery   │  │  columns)     │  │ compact, reindex,      │  │
│  │ + Download  │  │               │  │ expire                  │  │
│  │ + Queue     │  │               │  │                        │  │
│  └─────────────┘  └───────────────┘  └────────────────────────┘  │
│       │                  │                      │                 │
│       └──────────────────┼──────────────────────┘                 │
│                          │ WebSocket (real-time progress)         │
└──────────────────────────┼───────────────────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────────────────┐
│                    FastAPI Backend                                 │
│                                                                   │
│  ┌──────────────┐   ┌─────────────────────────────────────────┐  │
│  │ Crawler      │   │  Duckle Pipeline (duckle Python API)     │  │
│  │              │   │                                         │  │
│  │ CrawlSession │   │  src.spatial → xf.rename → xf.project   │  │
│  │  - login     │   │    → qa.geomvalidate                    │  │
│  │  - public    │   │      → valid → snk.ducklake (append)   │  │
│  │  - re-auth   │   │      → reject → snk.ducklake (rejects) │  │
│  │              │   │                                         │  │
│  │ Discover     │   │  + code.sql (WKB + bbox columns)        │  │
│  │  - auto mode │   │                                         │  │
│  │  - manual    │   │                                         │  │
│  │  - stoppable │   │                                         │  │
│  │  - threaded  │   │                                         │  │
│  │              │   │                                         │  │
│  │ Download     │   │                                         │  │
│  │  - batched   │   │                                         │  │
│  │  - progress  │   │                                         │  │
│  │  - stoppable │   │                                         │  │
│  │  - ETag/LM   │   │                                         │  │
│  │              │   │                                         │  │
│  │ Respect      │   │                                         │  │
│  │  - robots.txt│   │                                         │  │
│  │  - delays    │   │                                         │  │
│  │  - browser UA│   │                                         │  │
│  └──────────────┘   └────────────────┬────────────────────────┘  │
│                                      │                            │
│  ┌───────────────────────────────────▼────────────────────────┐  │
│  │       DuckDB (plain catalog) + DuckLake (lakehouse)         │  │
│  │                                                             │  │
│  │  catalog/vworld_catalog.db (DuckDB — app metadata):         │  │
│  │   crawl_state  (URL/ETag/Last-Modified tracking)            │  │
│  │                                                             │  │
│  │  catalog/ducklake_metadata.ducklake (DuckLake — data):      │  │
│  │   roads        (snapshots: v1=raw, v2=clean, ...)          │  │
│  │   roads_rejects (geometry validation failures)              │  │
│  │   buildings    (snapshots: v1=raw, v2=clean, ...)           │  │
│  │   buildings_rejects                                         │  │
│  │                                                             │  │
│  │  Per-row: geom_wkb, bbox_xmin/ymin/xmax/ymax, data_date    │  │
│  └─────────────────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────────────────┘
                           │
                           ▼ Parquet files on disk
┌──────────────────────────────────────────────────────────────────┐
│               Wails Desktop (Go) — Full GIS Viewer               │
│  Reads DuckLake tables directly via DuckDB. Consumes WKB.        │
└──────────────────────────────────────────────────────────────────┘
```

## 2. Data Flow

### 2.1 Discovery Phase

Two entry paths — **public** (no credentials) and **login** (authenticated):

```
User chooses mode in GUI:
  │
  ├── [Public Access]  →  enter URL → CrawlSession(auth_required=False) → plain httpx.Client
  │
  └── [Login Required] →  enter URL + credentials → CrawlSession(auth_required=True) → POST login → cookie store
                                   │
                                   ▼
                    Env var fallback: VWORLD_URL, VWORLD_USERNAME, VWORLD_PASSWORD
```

```
User clicks Discover
         │
         ▼ (runs in background thread — event loop stays responsive)
┌──────────────────────────────────────────┐
│ Paginated walk (auto or manual)          │
│ Page 1 → Page 2 → ... → Page N (≤100)   │
│ Visited-URL tracking prevents loops      │
│ 1–3s randomized delay between pages      │
│ robots.txt honored (disallowed → refuse) │
│ Accumulate file list with sizes + ETag   │
└──────────────────┬───────────────────────┘
                   │
                   ▼
┌──────────────────────────────────────────┐
│ File selection grid                      │
│ - Checkboxes per file                    │
│ - Select all / filter by name            │
│ - Shows: name, size, date, status        │
└──────────────────┬───────────────────────┘
                   │
                   ▼
┌──────────────────────────────────────────┐
│ Download queue                           │
│ - Batch size N (user-configurable, def=5)│
│ - Concurrent downloads with progress     │
│ - Queue waits for slot availability      │
└──────────────────┬───────────────────────┘
                   │ .zip/.shp/.geojson/gpkg/.parquet on disk
                   ▼
┌──────────────────────────────────────────┐
│ Read file → detect format                │
│ - .zip: unzip to first spatial inside    │
│ - .shp/.geojson/.gpkg: ST_Read           │
│ - .parquet/.geoparquet: read_parquet     │
│   + WKB decode or native GEOMETRY        │
│ Detect CRS from geometry metadata        │
│ Detect schema (columns + types)          │
└──────────────────┬───────────────────────┘
                   │
                   ▼
         Schema Editor (GUI)
    ┌──────────────────────────┐
    │ Raw columns:             │
    │  RN          → [road_id] │
    │  ROAD_NAME   → [name___] │
    │  LENG        → [length_] │
    │  INTERNAL_ID → [✕ drop]  │
    │                          │
    │ [▶ Run Pipeline]         │
    └──────────────────────────┘
```

### 2.2 Pipeline Phase (per dataset, all provinces)
```
For each province file in dataset:
  │
  ▼
[GDAL formats: .shp, .geojson, .gpkg]
  src.spatial(path=province_file)

[GeoParquet: .parquet, .geoparquet]
  src.parquet(path=province_file)
  → code.sql(EXCLUDE geom_col, ST_GeomFromWKB(geom_col) AS geom)
  │
  ▼
CRS detection + reproject to EPSG:4326 (step 1.5)
  ST_Transform(geom, detected_crs, 'EPSG:4326', always_xy:=true)
  Only when source CRS ≠ 4326 (WGS84); skipped for unknown CRS with warning
  │
  ▼
xf.rename(RN="road_id", ROAD_NAME="name", LENG="length_m")
  │
  ▼
xf.project(columns=["road_id", "name", "length_m", "width_m", "geom"])
  │
  ▼
code.sql(  -- WKB + bbox enrichment
  SELECT *, ST_AsWKB(geom) AS geom_wkb,
         ST_XMin(geom) AS bbox_xmin, ST_YMin(geom) AS bbox_ymin,
         ST_XMax(geom) AS bbox_xmax, ST_YMax(geom) AS bbox_ymax
  FROM input
)
  │
  ▼
qa.geomvalidate()
  ├── valid ──▶ snk.ducklake(table="roads",
                mode=write_mode,                 ← whether "append" or "upsert" (delta)
                conflictColumns=[upsert_key_col])  ← upsert key from UI
  └── reject ─▶ snk.ducklake(table="roads_rejects",
                 mode="append")
                   │
                   ▼ (after all provinces appended)
         Post-crawl operations:
           CALL ducklake_merge_adjacent_files('vworld', 'roads')
           CALL ducklake_rewrite_data_files('vworld', 'roads')
```

### 2.3 Re-crawl (Incremental)
```
User clicks Re-crawl (CrawlerPanel, same connected session)
    │
    ▼
Re-discover all files (same paginated walk)
    │
    ▼
Split discovered URLs:
  ├── Not in crawl_state → "new" (no HEAD needed)
  └── In crawl_state → HEAD request per URL (max 50, 0.2s pace)
         │
         ▼ Real ETag/Last-Modified from file response
    check_changes() compares against stored crawl_state values
         │
         ├── ETag + Last-Modified match → "unchanged" (skip)
         └── ETag / Last-Modified differ wear → "updated"
              │
              ▼
  GUI: change summary panel (new / updated / unchanged counts)
  new + updated files pre-selected for download
    │
    ▼
  Download only changed files
    │
    ▼
  For each file:
     ├── Full file → pipeline append (write_mode="append")
    └── Delta file → pipeline upsert (write_mode="upsert",
                 data_date from filename, conflict_columns from UI)
         │
         └── data_date column propagated from file date metadata
             (file mtime used as fallback for local-mode files)
```

## 3. GUI Screens

### Screen 1: Crawler Connect & Discovery
- Public / Login mode toggle
- URL input (public) or URL + credentials (login)
- Env var auto-fill: VWORLD_URL, VWORLD_USERNAME, VWORLD_PASSWORD
- Session status indicator: Connected (green) / Disconnected (grey)
- 30-second session health polling
- Pagination: "Page N of M" with auto/manual toggle + "Next Page"
- File grid: checkbox, name, size, last-modified, status
- File names extracted from URL when link text is generic ("download")
- Select All / Filter by name
- Batch size selector (default 3)
- Download queue with per-file progress bars (indeterminate for unknown-size)
- **Re-crawl** button (green): re-discovers all files, HEAD-checks known URLs against crawl_state, shows new/updated/unchanged summary, pre-selects changes
- "Stop" to cancel mid-crawl (both discovery and download)

### Screen 2: Schema Editor
- Raw columns from shapefile (auto-detected)
- Per-column: readable new name, toggle to drop
- **Advanced — delta load** section: checkbox to enable upsert-merge, date picker for data_date, dropdown for upsert key column (from mapped columns)
- Preview: first N rows with current mapping applied
- "Run Pipeline" button → WebSocket progress

### Screen 3: Pipeline Progress
- Real-time node status: `src.spatial ✓ (234,051 rows)` / `qa.geomvalidate ⏳`
- Reject counter: "47 rows rejected → roads_rejects"
- Per-node SQL preview (expandable)
- Final summary after completion

### Screen 4: DuckLake Console
- Table list: name, latest snapshot, feature count, total size
- Snapshot timeline per table (click to expand)
- **Originals in staging** per table: source files with Loaded/Cleared status dots, Clear staging, Re-download
- **Download staging** section: total files/size, not-yet-loaded list with Load-into-table and Delete
- Table actions: Compact, Rewrite Data Files, Expire Snapshots, **View on Map** (green map-icon per row)
- Reject table rider with Inspect button

### Screen 5: Map Preview
- **deck.gl** overlay with **CARTO dark** blastermap tiles (`dark_all`)
- Full-table bounding box: dashed yellow outline, fits viewport on open
- **Draw mode** toggle button (box-select icon, cyan when active): map scrolling disabled during draws; drag a rectangle to generate spatial filter
- Auto row limits per geometry type: points 10,000, linestrings 2,000, polygons 500
- **Points** → cyan ScatterplotLayer, **Lines** → fuscia GeoJsonLayer, **Polygons** → lime GeoJsonLayer
- Side panel (1/4 width, 2 tabs):
  - **Statistics tab**: per-column rows/distinct/null, avg/median, in-line (min/max); each row expands to a histogram (numeric: 15-bin equal-width bars; decategorical: top-12 category bars)
  - **Attributes tab**: digital table (50 rows per page) with spatial filter
- Map information bar: table name, matching rows (actual vs filtered), geometry type, limit
- Top controls: draw method (toggle), whole selection, fit to table

## 4. Pipeline Per-Row Columns

Every DuckLake table row includes:

| Column | Source | Purpose |
|--------|--------|---------|
| `geom` | Shapefile (native) | DuckDB spatial functions |
| `geom_wkb` | `ST_AsWKB(geom)` | Go/Wails desktop consumption |
| `bbox_xmin` | `ST_XMin(geom)` | Fast bounding-box filter |
| `bbox_ymin` | `ST_YMin(geom)` | Fast bounding-box filter |
| `bbox_xmax` | `ST_XMax(geom)` | Fast bounding-box filter |
| `bbox_ymax` | `ST_YMax(geom)` | Fast bounding-box filter |
| `data_date` | Delta file metadata | Time-series queries |
| `...user columns...` | Shapefile + renames | Domain data |

## 5. Technology Stack

| Layer | Technology | Version |
|-------|-----------|---------|
| Backend | Python FastAPI + WebSocket | 3.11+ |
| Crawler | httpx + BeautifulSoup4 | latest |
| Pipeline Engine | duckle (Python API) | >=0.5.9,<0.6 |
| Database | DuckDB + DuckLake extension | 1.5.2+ |
| Data Format | Parquet (via DuckLake) | — |
| Frontend | React + TypeScript + shadcn/ui | React 19 |
| Map Preview | deck.gl + CARTO dark tiles | React 19 |
| GIS Viewer | Go/Wails desktop (existing) | — |
| Design | Figma + shadcn/ui Figma kit | — |

## 6. Open Questions (Tabled)

| # | Question | Status |
|---|----------|--------|
| 1 | Concrete VWorld URLs for first crawl | Tabled |
| 2 | Authentication mechanism (form login vs API key) | Tabled — credentials via env vars |
| 3 | How to detect delta vs full files | Tabled — depends on VWorld's file naming/metadata |
| 4 | File sizes by province | To be discovered on first crawl |

## 7. Project Structure

```
vworld-crawl/
├── CONTEXT.md                     # Domain glossary
├── docs/
│   └── adr/
│       ├── 0001-duckle-as-library.md
│       ├── 0002-duckdb-catalog.md
│       ├── 0003-one-table-per-dataset.md
│       ├── 0004-web-console-wails-viewer.md
│       └── 0005-crawler-architecture.md
├── backend/
│   ├── main.py                    # FastAPI + WebSocket (all API + WS endpoints)
│   ├── crawler/
│   │   ├── __init__.py
│   │   ├── session.py             # CrawlSession (login + public mode, re-auth, cookie persistence)
│   │   ├── discover.py            # Paginated link discovery (auto/manual, threaded, spatial-filtered)
│   │   ├── download.py            # Batched download with queue, per-file progress, ETag capture
│   │   ├── respect.py             # robots.txt honoring, browser UA, randomized delays
│   │   └── state.py               # Crawl state persistence in plain DuckDB catalog
│   ├── pipeline/
│   │   ├── __init__.py
│   │   ├── runner.py              # Duckle pipeline builder + CRS detection + reproject step 1.5
│   │   └── progress.py            # Pipeline progress parser
│   ├── map_api.py                 # Spatial queries: bounds, stats, features, histograms
│   ├── db.py                      # Shared DuckDB connection helper
│   ├── catalog.py                 # Catalog abstraction (DuckDB | PostgreSQL)
│   ├── ducklake_ops.py            # DuckLake extension setup + ATTACH
│   ├── ducklake_console.py        # Table list, snapshots, compact, reindex, expire
│   ├── schema_detector.py         # File scanning + schema/CRS detection via DuckDB
│   ├── requirements.txt           # Python deps
│   └── catalog/                   # Catalog files (created at runtime)
│       ├── vworld_catalog.db      # Plain DuckDB for app metadata (crawl_state)
│       └── ducklake_metadata.ducklake  # DuckLake catalog (pipeline output)
└── frontend/
    ├── src/
    │   ├── App.tsx                # Main app: routing, state, nav
    │   ├── components/
    │   │   ├── DirectoryPicker.tsx  # Local directory scan
    │   │   ├── CrawlerPanel.tsx    # Login/public connect, discovery, file grid, download, re-crawl
    │   │   ├── FileGrid.tsx        # Scanned file list with selection
    │   │   ├── SchemaEditor.tsx    # Column rename/drop + delta load mode (upsert, data_date)
    │   │   ├── PipelineProgress.tsx # Real-time node status + rejects
    │   │   ├── DuckLakeConsole.tsx # Tables, snapshots, staging, compact, map button
│   │   └── MapPreview.tsx       # deck.gl map: draw-mode rectangle select, histograms, attrs
    │   └── lib/
    │       ├── api.ts             # API base URL + wsUrl helper
    │       └── utils.ts           # Utility functions
    └── ...
```
