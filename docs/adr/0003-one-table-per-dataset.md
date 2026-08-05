# One Dataset = One DuckLake Table with Schema Evolution

Each VWorld dataset (roads, buildings, parcels) maps to a single DuckLake table. Schema evolves across DuckLake snapshots within the same table — the raw VWorld schema (v1) and the cleaned schema (v2) are different snapshots of the same table, not separate tables. Province files are appended into the dataset table.

**Status:** accepted

**Considered Options:**
- `{dataset}_raw` and `{dataset}_clean` as separate tables: clear separation but duplicates geometry data and complicates lineage.
- One table per province: fragments the data and requires a union step for country-wide queries.
- One table per dataset with schema evolution: single source of truth, time travel to raw schema, no geometry duplication.

**Consequences:**
- Each dataset table starts with the VWorld schema and evolves as the user renames/drops columns.
- Time travel (`AT SNAPSHOT 'v1'`) recovers the raw schema at any time.
- Province append is a simple `write_mode="append"` — no merge node needed.
- Post-crawl compact + rewrite data files applies once per dataset table. Spatial indexes are not supported by DuckLake; the per-row `bbox_*` columns and Parquet zone maps provide the spatial access path.
- The DuckLake console shows snapshot history per table with the column changes between versions.
