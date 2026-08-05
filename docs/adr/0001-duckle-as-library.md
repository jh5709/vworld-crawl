# Duckle as Python Library (Not Fork)

VWorld Crawl uses Duckle (`pip install duckle`) as a Python library rather than forking its source. A thin abstraction layer (`pipeline/runner.py`) wraps all Duckle calls. The library is pinned to `>=0.5.9,<0.6`.

**Status:** accepted

**Considered Options:**
- Fork Duckle: full control over components, but ongoing maintenance burden from upstream changes.
- Library: zero maintenance, but vulnerable to API changes in Duckle's beta releases.

**Consequences:**
- Version pin prevents surprise breakage. The abstraction layer means API changes are fixed in one file.
- If Duckle adds a needed component we can't express via the Python API, we can always fork later — the abstraction layer makes the switch point obvious.
- Duckle's 17 geospatial components and 4 DuckLake components are consumed as-is. Only the web crawler is custom code.
- Multi-format pipeline support fits within the component graph: `src.spatial` (ST_Read) for `.shp`, `.geojson`, `.gpkg`; `src.parquet` (read_parquet) + a `code.sql` WKB-decode transform for `.parquet`/`.geoparquet`.
- Licensing is MIT OR Apache 2.0 — no barriers to commercial use or redistribution.
