# VWorld Crawl — Architecture (Post-Grilling)

> Sharpened through `/grill-with-docs` session. Read alongside [CONTEXT.md](./CONTEXT.md) and [docs/adr/](./docs/adr/).

## 1. System Overview

```
┌──────────────────────────────────────────────────────────────────┐
│               Web Console (React + FastAPI)                      │
│                                                                   │
│  ┌─────────────┐  ┌───────────────┐  ┌────────────────────────┐  │
│  │ Crawler     │  │ Schema Editor │  │ DuckLake Console       │  │
│  │ Discovery   │  │ (rename/drop  │  │ Tables, snapshots,     │  │
│  │ + Download  │  │  columns)     │  │ compact, reindex,      │  │
│  │ + Queue     │  │               │  │ spatial index           │  │
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
│  │ httpx + bs4  │   │                                         │  │
│  │              │   │  src.spatial → xf.rename → xf.project   │  │
│  │ - Paginated  │   │    → qa.geomvalidate                    │  │
│  │   discovery  │   │      → valid → snk.ducklake (append)   │  │
│  │ - Batch dl   │   │      → reject → snk.ducklake (rejects) │  │
│  │ - Queue mgmt │   │                                         │  │
│  │ - Credentials│   │  + code.sql (WKB + bbox columns)        │  │
│  │   via env    │   │                                         │  │
│  └──────────────┘   └────────────────┬────────────────────────┘  │
│                                      │                            │
│  ┌───────────────────────────────────▼────────────────────────┐  │
│  │              DuckDB + DuckLake (DuckDB catalog)             │  │
│  │                                                             │  │
│  │  Tables:                                                     │  │
│  │   roads        (snapshots: v1=raw, v2=clean, ...)          │  │
│  │   roads_rejects (geometry validation failures)              │  │
│  │   buildings    (snapshots: v1=raw, v2=clean, ...)           │  │
│  │   buildings_rejects                                         │  │
│  │   crawl_state  (URL/ETag/Last-Modified tracking)            │  │
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
```
User enters VWorld URL
         │
         ▼
┌──────────────────────────────────────────┐
│ Paginated walk (auto or manual)          │
│ Page 1 → Page 2 → ... → Page N          │
│ Accumulate file list with sizes          │
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
                   │ .zip files on disk
                   ▼
┌──────────────────────────────────────────┐
│ Unzip → .shp / .dbf / .prj              │
│ Detect CRS from .prj                     │
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
For each province .shp in dataset:
  │
  ▼
src.spatial(path=province.shp)
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
  ├── valid ──▶ snk.ducklake(table="roads", write_mode="append")
  └── reject ─▶ snk.ducklake(table="roads_rejects", write_mode="append")
                   │
                   ▼ (after all provinces appended)
         Post-crawl operations:
           CALL ducklake_compact('roads')
           CALL ducklake_reindex('roads')
           CREATE INDEX roads_rtree ON roads USING RTREE(geom)
```

### 2.3 Re-crawl (Incremental)
```
Re-crawl discovery → Compare against crawl_state table
  │
  ├── Unchanged files → skip
  │
  └── Changed files:
        ├── Full file → replace/append snapshot
        └── Delta file → upsert merge (identified by date metadata)
                            │
                            ▼
                  data_date column populated from file metadata
```

## 3. GUI Screens

### Screen 1: Crawler Discovery & Download
- URL input with credentials from env
- Pagination: "Page N of M" with auto/manual toggle + "Next Page"
- File grid: checkbox, name, size, last-modified, status
- Select All / Filter by name / Province grouping
- Batch size selector (default 5)
- Download queue with per-file progress bars
- "Stop" to cancel mid-crawl

### Screen 2: Schema Editor
- Raw columns from shapefile (auto-detected)
- Per-column: editable new name, toggle to drop
- Preview: first N rows with current mapping applied
- "Run Pipeline" button → WebSocket progress

### Screen 3: Pipeline Progress
- Real-time node status: `src.spatial ✓ (234,051 rows)` / `qa.geomvalidate ⏳`
- Reject counter: "47 rows rejected → roads_rejects"
- Per-node SQL preview (expandable)
- Final summary after completion

### Screen 4: DuckLake Console
- Table list: name, latest snapshot, file count, total size
- Snapshot timeline per table (click to expand)
- Table actions: Compact, Reindex, Spatial Index, Expire Snapshots
- Reject table browser: inspect failed rows with error reasons

### Screen 5: Map Preview (minimal)
- Lightweight map (Leaflet or minimal DeckGL)
- Feature count + bounding box overlay
- Purpose: confirm pipeline output, not full GIS

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
| Map Preview | Leaflet (or minimal DeckGL) | — |
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
│       └── 0004-web-console-wails-viewer.md
├── backend/
│   ├── main.py                    # FastAPI + WebSocket
│   ├── crawler/
│   │   ├── discover.py            # Paginated link discovery
│   │   ├── download.py            # Batched download with queue
│   │   └── session.py             # Auth session (env credentials)
│   ├── pipeline/
│   │   └── runner.py              # Duckle pipeline builder (thin abstraction)
│   ├── ducklake/
│   │   └── ops.py                 # Compact, reindex, spatial index, snapshot mgmt
│   └── ws/
│       └── progress.py            # WebSocket progress streaming
└── frontend/
    ├── src/
    │   ├── components/
    │   │   ├── CrawlerPanel.tsx    # Discovery, pagination, file grid, queue
    │   │   ├── SchemaEditor.tsx    # Column rename/drop with preview
    │   │   ├── PipelineProgress.tsx # Real-time node status + rejects
    │   │   ├── DuckLakeConsole.tsx # Tables, snapshots, operations
    │   │   └── MapPreview.tsx      # Minimal leaflet/deckgl confirmation
    │   └── hooks/
    │       └── useWebSocket.ts     # Pipeline progress subscription
    └── ...
```
