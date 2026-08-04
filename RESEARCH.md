# VWorld Geospatial Crawl — Research & Architecture Document

> **Status:** Research complete. Feed this document to the project to begin implementation.
> **Date:** August 2025

---

## 1. Objective

Build a **geospatial data operations console** that:

1. **Crawls** web pages to discover and download zipped shapefiles (`.zip` containing `.shp`/`.dbf`/`.prj`)
2. **Processes** geospatial data through a composable ELT pipeline (validate, reproject, transform)
3. **Loads** results into **DuckLake** — a lakehouse table format on DuckDB + Parquet — for querying, time travel, and snapshot management
4. **Presents** a sophisticated GUI for crawler configuration, map preview, and DuckLake operations

The project targets Korean VWorld geospatial data but is designed to work with any web-hosted shapefile repository.

---

## 2. Architecture

### 2.1 Overview

```
┌────────────────────────────────────────────────────────────────────┐
│               Custom GUI (React + shadcn/ui + DeckGL)              │
│                                                                    │
│  ┌──────────────────┐   ┌────────────────┐   ┌─────────────────┐  │
│  │ 🕷️ Crawler Panel │   │ 🗺️ Map Preview │   │ 🏔️ DuckLake Ops │  │
│  └────────┬─────────┘   └────────────────┘   └─────────────────┘  │
│           │                                                        │
│           │  FastAPI (REST + WebSocket)                            │
│           ▼                                                        │
│  ┌─────────────────────────────────────────────────────────────┐  │
│  │              Python Backend (FastAPI)                         │  │
│  │                                                               │  │
│  │  ┌──────────┐   ┌────────────────────────────────────────┐  │  │
│  │  │ Crawler  │   │  duckle Python API (library)            │  │  │
│  │  │ (httpx + │   │                                        │  │  │
│  │  │  bs4)    │   │  src.spatial → xf.geo.* → qa.geom*    │  │  │
│  │  └────┬─────┘   │       → snk.ducklake                   │  │  │
│  │       │         └────────────────┬───────────────────────┘  │  │
│  │       │                          │                          │  │
│  │       │  unzip .zip → .shp paths │                          │  │
│  │       └──────────────────────────┘                          │  │
│  └─────────────────────────────────────────────────────────────┘  │
│                           │                                        │
│                           ▼                                        │
│  ┌─────────────────────────────────────────────────────────────┐  │
│  │              DuckDB + DuckLake                                │  │
│  │                                                               │  │
│  │  Catalog: DuckDB file (single-user, zero external deps)      │  │
│  │  Data:    Parquet files on disk (vworld_data/)               │  │
│  │                                                               │  │
│  │  Features: time travel, ACID, compaction, snapshot expiry    │  │
│  └─────────────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────────┘
```

### 2.2 Key Technology Decision: Duckle as Library

**Duckle** ([github.com/slothflowlabs/duckle](https://github.com/slothflowlabs/duckle)) is an open-source (MIT OR Apache 2.0), local-first ETL/ELT studio built on DuckDB. It ships:

- **363 components** — sources, transforms, sinks, validators, control-flow, code runners
- **17 geospatial components** — shapefile/GeoJSON/GeoPackage readers, reprojection, buffer, clip, erase, spatial join, geometry validation/repair
- **4 DuckLake components** — read, write, CDC changes feed, snapshot diff
- **Python API** (`pip install duckle`) — fluent builder for pipelines
- **Plugin SDK** (Rust) — for custom components if needed

**Decision:** Use Duckle as a Python library via `pip install duckle`. Do NOT fork or modify Duckle source. All existing geospatial and DuckLake functionality is consumed through Duckle's Python API. Only the web crawler is custom-built.

**Licensing:** Duckle is dual-licensed MIT OR Apache 2.0. Attribution required: include `Copyright (c) 2026 Sourav Roy, licensed under MIT OR Apache 2.0`. No written permission needed. Can be used commercially, modified, sublicensed, and distributed. Trademark rights are NOT granted — do not imply SlothFlowLabs endorsement.

### 2.3 DuckLake Catalog Decision

DuckLake requires a **catalog database** for metadata. Options:

| Catalog | Dependencies | Use Case |
|---------|-------------|----------|
| **DuckDB** | Zero (embedded) | Single user, local — **chosen** |
| SQLite | Zero (embedded) | Multiple local clients |
| PostgreSQL | PostgreSQL 12+ required | Multi-user lakehouse with remote clients |

**Decision:** Use DuckDB as the catalog. `ATTACH 'ducklake:vworld_metadata.ducklake' AS vworld (DATA_PATH 'vworld_data/')`. Zero external dependencies. Can swap to PostgreSQL later with one `ATTACH` line change.

---

## 3. Component Inventory

### 3.1 What Duckle Already Provides (Do Not Build)

**Geospatial Sources:**
- `src.spatial` — Read shapefiles, GeoJSON, GeoPackage, KML, GPX, GML via DuckDB spatial extension (`ST_Read`)
- `src.gdb` — Read Esri File Geodatabase
- `src.http` — Read CSV/Parquet/JSON from HTTP URLs via httpfs

**Geospatial Transforms:**
- `xf.geo.reproject` — Reproject to new CRS (`ST_Transform`)
- `xf.geo.buffer` — Buffer geometry (`ST_Buffer`)
- `xf.geo.intersects` — Boolean spatial test (`ST_Intersects`)
- `xf.geo.distance` — Distance calculation
- `xf.geo.area` / `.length` / `.perimeter` — Geometric measurements
- `xf.geo.create` — Build geometry from X/Y, WKT, WKB
- `xf.geo.flip` — Swap X/Y (fix lat/lon vs lon/lat)
- `xf.geo.setcrs` — Assign CRS without transforming (`ST_SetCRS`)
- `xf.geo.clip` — Clip overlay (two-input)
- `xf.geo.erase` — Erase overlay (two-input)
- `xf.join.spatial` — Spatial join (`ST_Intersects`, `Contains`, `Within`, `Touches`, `Crosses`, `Overlaps`, `Equals`, `Covers`, `Covered by`)

**Geometry Quality:**
- `qa.geomvalidate` — Flag invalid geometries (`ST_IsValid`), route to valid/invalid ports
- `qa.geomrepair` — Repair invalid geometries in place (`ST_MakeValid`)
- `qa.geomempty` — Flag empty geometries (`ST_IsEmpty`)

**DuckLake Components:**
- `snk.ducklake` — Write table to DuckLake catalog (supports append/overwrite/truncate/upsert)
- `src.ducklake` — Read tables from DuckLake catalog
- `src.ducklake.changes` — CDC feed: `table_changes()` since last consumed snapshot
- `src.ducklake.diff` — Row-level diff between two snapshots

**General Components:**
- `src.parquet`, `src.csv`, `src.json` — File sources
- `xf.filter`, `xf.project`, `xf.sort`, `xf.limit`, `xf.dedupe` — Standard transforms
- `snk.parquet`, `snk.csv`, `snk.json` — File sinks
- `ctl.checkpoint` — Snapshot pipeline state for incremental runs
- `ctl.schedule` — Cron/interval/file-watch scheduling

### 3.2 Custom Components to Build

| Component | Language | Description |
|-----------|----------|-------------|
| **Web Crawler** | Python (`httpx` + `BeautifulSoup4`) | Discover and download `.zip` files from a target web page. Input: URL, CSS selectors, file pattern, max depth. Output: list of local `.zip` file paths |
| **Unzipper** | Python (`zipfile` stdlib) | Extract `.zip` → `.shp`/`.dbf`/`.prj` paths. Part of the crawler module |
| **CRS Detector** | Python (`geopandas`/`fiona`) | Read `.prj` or probe `.shp` to detect CRS. Used to populate `xf.geo.reproject(source_crs=...)` |

These DO NOT need to be Duckle plugin components (which would require Rust + the Plugin SDK). They are plain Python functions that run BEFORE the Duckle pipeline. The pipeline starts at `src.spatial(path=extracted_shp_path)`.

---

## 4. Method

### 4.1 Data Flow

```
Web page URL
    │
    ▼
┌─────────────────────────────────────────────┐
│ 1. Crawler (custom Python)                   │
│    - Fetch page with httpx                   │
│    - Parse HTML with BeautifulSoup4           │
│    - Discover links matching *.zip pattern    │
│    - Download .zip files to local cache       │
│    - Extract .shp/.dbf/.prj from each .zip   │
│    - Detect CRS from .prj file               │
└──────────────────┬──────────────────────────┘
                   │ List[(shp_path, source_crs)]
                   ▼
┌─────────────────────────────────────────────┐
│ 2. Duckle Pipeline (library call)            │
│                                               │
│    for each shp_path:                        │
│      duckle.src.spatial(path=shp_path)       │
│        .xf.geo.reproject(                    │
│          source_crs=detected_crs,            │
│          target_crs="EPSG:4326"              │
│        )                                     │
│        .qa.geomrepair()                      │
│        .qa.geomvalidate()                    │
│        .snk.ducklake(                        │
│          table=table_name,                   │
│          data_path="vworld_data/"            │
│        )                                     │
│        .run()                                │
└──────────────────┬──────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────┐
│ 3. DuckLake (DuckDB extension)               │
│    - Schema auto-inferred from GeoDataFrame  │
│    - Data written as Parquet files           │
│    - Metadata in DuckDB catalog              │
│    - Snapshots created per run               │
│    - Time travel: query any snapshot         │
│    - Compaction: merge small files           │
└─────────────────────────────────────────────┘
```

### 4.2 Duckle Python API Patterns

```python
import duckle

# ---- Simple linear pipeline ----
(duckle.src.spatial(path="roads.shp")
    .xf.geo.reproject(source_crs="EPSG:5186", target_crs="EPSG:4326")
    .qa.geomrepair()
    .snk.ducklake(table="roads", data_path="vworld_data/")
    .run())

# ---- Pipeline with branching (valid/invalid split) ----
# Use from_json to load a pre-built pipeline JSON with branching

# ---- Read back from DuckLake ----
(duckle.src.ducklake(table="roads")
    .xf.geo.buffer(distance=100, unit="meters")
    .snk.spatial(path="buffered_roads.geojson")
    .run())

# ---- CDC / incremental ----
(duckle.src.ducklake.changes(table="roads")
    .snk.parquet(path="roads_changes.parquet")
    .run())
```

### 4.3 Catalog Database Setup (DuckDB catalog, no PostgreSQL)

```sql
-- One-time setup
INSTALL ducklake;
INSTALL spatial;

ATTACH 'ducklake:vworld_metadata.ducklake' AS vworld
    (DATA_PATH 'vworld_data/');
USE vworld;

-- Tables created by duckle.snk.ducklake() appear here
-- Time travel:
SELECT * FROM roads AT SNAPSHOT 'v17';

-- Compaction:
CALL ducklake_compact('roads');

-- Snapshot management:
CALL ducklake_expire_snapshots('roads', older_than => '7 days');
```

---

## 5. GUI Design (Figma)

### 5.1 Tooling

- **Design tool:** Figma (free tier — 3 files, unlimited drafts, Dev Mode)
- **Component kit:** [shadcn/ui Figma kit](https://www.figma.com/community/file/1342711299332111612) — 1:1 mapping to React components
- **Map library:** DeckGL (deck.gl) or kepler.gl for React
- **Pipeline library:** Duckle Python API (no visual canvas needed in GUI — Duckle handles execution)

### 5.2 Screens to Design

#### Screen 1: Crawler Discovery & Configuration
```
┌──────────────────────────────────────────────────────────┐
│  🕷️ Web Crawler                                          │
│                                                          │
│  Target URL         [https://example.com/maps        ]   │
│  File Pattern       [*.zip                     ]         │
│  Link Selector      [a[href$='.zip']           ]         │
│  Max Depth          [3]  [🔍 Discover]                   │
│                                                          │
│  ┌─ Discovered Files ────────────────────────────────┐  │
│  │ ☑ roads.zip         45 MB   shapefile    ✅ valid │  │
│  │ ☑ buildings.zip    120 MB   shapefile    ✅ valid │  │
│  │ ☐ parcels.zip      340 MB   shapefile    ⬜ new   │  │
│  │ ☐ water.zip         12 MB   shapefile    ⬜ new   │  │
│  └────────────────────────────────────────────────────┘  │
│                                                          │
│  Target Table: [roads____________]                       │
│  Target CRS:   [EPSG:4326________]                       │
│                                                          │
│  [🗑 Deselect All]  [🗺 Preview Selected]  [▶ Load All]  │
└──────────────────────────────────────────────────────────┘
```

#### Screen 2: Map Preview
```
┌──────────────────────────────────────────────────────────┐
│  🗺️ Preview: roads.shp                                   │
│  ┌──────────────────────────────────────────────────────┐│
│  │                                                      ││
│  │              DeckGL / kepler.gl map                  ││
│  │              with layer from selected file           ││
│  │                                                      ││
│  └──────────────────────────────────────────────────────┘│
│  Features: 234,051  │  CRS: EPSG:5186  │  Bounds: ...    │
│                                                          │
│  ┌─ Attribute Preview ──────────────────────────────┐   │
│  │ id │ name       │ length_km │ geometry_type      │   │
│  │ 1  │ 강남대로    │ 3.2       │ LineString        │   │
│  │ 2  │ 테헤란로    │ 2.8       │ LineString        │   │
│  └──────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────┘
```

#### Screen 3: DuckLake Operations Console
```
┌──────────────────────────────────────────────────────────┐
│  🏔️ DuckLake Tables                                      │
│                                                          │
│  ┌────────────────────────────────────────────────────┐  │
│  │ Table      │ Version │ Files │ Size   │ Status     │  │
│  │ roads      │ v17     │ 12    │ 2.1 GB │ ✅ ready   │  │
│  │ buildings  │ v5      │ 8     │ 890 MB │ ✅ ready   │  │
│  │ parcels    │ v3      │ 4     │ 3.4 GB │ 🔄 loading │  │
│  └────────────────────────────────────────────────────┘  │
│                                                          │
│  ┌─ Snapshots: roads ───────────────────────────────┐   │
│  │ v17 (current)  2025-08-04 14:32  ← latest crawl  │   │
│  │ v16            2025-08-03 09:15                   │   │
│  │ v15            2025-08-02 22:01                   │   │
│  │ [⏪ Time Travel]  [🔄 Compact]  [🗑 Expire v1-v14] │   │
│  └──────────────────────────────────────────────────┘   │
│                                                          │
│  ┌─ Pipeline Run History ───────────────────────────┐   │
│  │ 2025-08-04 14:32  crawl → repair → ducklake  ✅   │   │
│  │ 2025-08-03 09:15  crawl → repair → ducklake  ✅   │   │
│  │ 2025-08-02 22:01  crawl → reproject → ducklake ✅ │   │
│  └──────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────┘
```

### 5.3 Figma Quick-Start for First-Time Users

1. **Sign up** at [figma.com](https://figma.com) → Free plan
2. **Install shadcn/ui kit** from Figma Community → Duplicate to your drafts
3. **Learn basics** (30 min):
   - `F` = Frame (your screen canvas)
   - `R` = Rectangle
   - `T` = Text
   - `V` = Move/Select
   - `Shift+A` = Auto Layout (flexbox for designers — the most important concept)
   - Left sidebar = layers (DOM tree)
   - Right sidebar = properties (CSS)
4. **Assemble screens** by dragging shadcn/ui components into frames (like LEGO)
5. **Dev Mode** (`Shift+D`) → auto-generates CSS/Tailwind classes and React code

---

## 6. Technology Stack

| Layer | Technology | Rationale |
|-------|-----------|-----------|
| **Design** | Figma + shadcn/ui kit | Free tier, 1:1 mapping to React components |
| **Frontend** | React 19 + TypeScript + shadcn/ui + DeckGL | Matches Duckle's own stack (React 19 + TS); shadcn for consistent UI; DeckGL for map layers |
| **Frontend DAG** | React Flow (optional, for pipeline visualization) | If pipeline visualization is needed; otherwise just status cards |
| **Backend** | Python 3.11+ FastAPI + WebSocket | Async, typed, real-time progress via WebSocket |
| **Crawler** | httpx + BeautifulSoup4 + zipfile (stdlib) | Lightweight, no headless browser needed for link discovery |
| **CRS Detection** | fiona or geopandas | Read `.prj` file to detect source CRS |
| **Pipeline Engine** | **duckle** (`pip install duckle`) | 360+ components, compiles to DuckDB SQL, Python fluent API |
| **Database** | DuckDB v1.5.2+ | Embedded columnar engine, spatial extension |
| **Lakehouse** | DuckLake v1.0 (DuckDB extension) | ACID, time travel, Parquet data files, DuckDB catalog |
| **Data Format** | Parquet (via DuckLake) | Open format, no vendor lock-in |
| **Package Mgmt** | uv or pip | Fast Python package management |

---

## 7. Project Structure (Proposed)

```
vworld-crawl/
├── RESEARCH.md                    # This document
├── pyproject.toml                 # Python project config
├── backend/
│   ├── main.py                    # FastAPI app entry point
│   ├── crawler/
│   │   ├── __init__.py
│   │   ├── discover.py            # Web page link discovery
│   │   ├── download.py            # File download with progress
│   │   └── extract.py             # Zip extraction, CRS detection
│   ├── pipeline/
│   │   ├── __init__.py
│   │   └── runner.py              # Duckle pipeline builder & executor
│   ├── ducklake/
│   │   ├── __init__.py
│   │   └── ops.py                 # DuckLake table management (compact, expire, time travel)
│   └── ws/
│       └── __init__.py            # WebSocket handlers for real-time progress
├── frontend/
│   ├── package.json
│   ├── src/
│   │   ├── App.tsx
│   │   ├── components/
│   │   │   ├── CrawlerPanel.tsx
│   │   │   ├── MapPreview.tsx
│   │   │   ├── DuckLakeConsole.tsx
│   │   │   └── PipelineStatus.tsx
│   │   ├── hooks/
│   │   └── lib/
│   └── ...
└── figma/
    └── (exported design specs, screenshots)
```

---

## 8. Implementation Roadmap

### Phase 1: Backend Core (Week 1-2)
- [ ] Python project setup with `pyproject.toml`
- [ ] `crawler/` module — discover, download, extract, CRS detection
- [ ] `pipeline/` module — Duckle pipeline builder wrapping `duckle` library
- [ ] `ducklake/` module — ATTACH, table listing, snapshot listing, compact, expire
- [ ] FastAPI routes: `/api/crawl/discover`, `/api/crawl/download`, `/api/pipeline/run`, `/api/ducklake/tables`, `/api/ducklake/snapshots`
- [ ] WebSocket endpoint for pipeline progress streaming

### Phase 2: Frontend (Week 2-4)
- [ ] React project with shadcn/ui setup
- [ ] **CrawlerPanel** — URL input, discovery results grid, file selection, load trigger
- [ ] **MapPreview** — DeckGL map with layer from selected file/table
- [ ] **DuckLakeConsole** — table list, snapshot timeline, compact/expire actions
- [ ] **PipelineStatus** — run history, real-time progress via WebSocket
- [ ] API client hooks

### Phase 3: Figma Design (Week 1, parallel)
- [ ] Set up Figma account
- [ ] Import shadcn/ui kit
- [ ] Design Crawler Discovery screen
- [ ] Design Map Preview screen
- [ ] Design DuckLake Console screen
- [ ] Export specs to frontend implementation

### Phase 4: Integration & Polish (Week 4-5)
- [ ] End-to-end flow: crawl → preview → load → time travel
- [ ] Error handling and dead-letter queue for failed geometries
- [ ] Incremental crawl scheduling
- [ ] DuckLake compaction automation

---

## 9. Key References

| Resource | URL |
|----------|-----|
| Duckle GitHub | https://github.com/slothflowlabs/duckle |
| Duckle Docs | https://duckle.org/docs/index.html |
| Duckle Component Reference | https://duckle.org/docs/components.html |
| Duckle Python API | `pip install duckle` — see `duckle.api` and `duckle._ns` |
| DuckLake Docs | https://ducklake.select/docs/stable/duckdb/usage/choosing_a_catalog_database.html |
| DuckLake GitHub | https://github.com/duckdb/ducklake |
| DuckDB Spatial Extension | https://duckdb.org/docs/extensions/spatial.html |
| shadcn/ui Figma Kit | https://www.figma.com/community/file/1342711299332111612 |
| DeckGL | https://deck.gl |
| React Flow | https://reactflow.dev |

---

## 10. Risk Assessment

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| Duckle v0.5.x API instability (beta) | Medium | Pin version; API is simple enough to adapt if it changes |
| DuckLake v1.0 ecosystem immaturity | Medium | Data is Parquet files — portable to Iceberg/Delta if needed |
| Large shapefile download failures | High | Chunked downloads, resume support, checksum verification |
| CRS detection ambiguity (.prj missing) | Medium | Fallback to common Korean CRS (EPSG:5186); allow manual override in GUI |
| Web page structure changes breaking crawler | High | Configurable selectors in GUI; preview before download |

---

*End of research document. Ready for implementation.*
