# DuckDB as DuckLake Catalog (Single-User)

DuckLake uses a DuckDB file as its catalog database rather than PostgreSQL. This enables zero-dependency, single-user operation. A catalog abstraction layer (`VWORLD_CATALOG_TYPE` env var) allows swapping to PostgreSQL later if multi-user access is needed.

**Status:** accepted

**Considered Options:**
- PostgreSQL: required for multi-user lakehouses with remote clients. Adds operational overhead (Postgres 12+ server).
- SQLite: supports multiple local clients via retry-timeout. Similar zero-dependency profile to DuckDB but adds complexity.
- DuckDB: embedded, single-client, zero external dependencies. `ATTACH 'ducklake:metadata.ducklake' AS vworld (DATA_PATH 'vworld_data/')`.

**Consequences:**
- MVP needs no external database server. DuckDB and DuckLake run in-process.
- Data stored as open Parquet files — portable to Iceberg/Delta if DuckLake is ever swapped out.
- If multi-user becomes required, the catalog abstraction means one environment variable and one `ATTACH` line change, plus standing up a PostgreSQL instance.
