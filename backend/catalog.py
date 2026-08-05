"""
Catalog abstraction layer for DuckLake metadata storage.

Default: DuckDB file (zero-dependency, single-user).
PostgreSQL: enabled via VWORLD_CATALOG_TYPE=postgresql env var.

Usage:
    from catalog import get_catalog

    catalog = get_catalog()
    conn = catalog.connect()
    # ... use conn ...
    catalog.close()
"""

import os
import logging
import duckdb

logger = logging.getLogger(__name__)

CATALOG_TYPE = os.getenv("VWORLD_CATALOG_TYPE", "duckdb").lower()
CATALOG_PATH = os.getenv("VWORLD_CATALOG_PATH", "catalog/vworld_catalog.db")


class CatalogConnection:
    """Abstract catalog connection. Wraps DuckDB or PostgreSQL."""

    def __init__(self, conn: duckdb.DuckDBPyConnection, backend: str):
        self._conn = conn
        self._backend = backend

    def execute(self, sql: str, params=None):
        return self._conn.execute(sql, params)

    def sql(self, query: str):
        return self._conn.sql(query)

    def close(self):
        self._conn.close()

    @property
    def backend(self) -> str:
        return self._backend


def get_catalog() -> CatalogConnection:
    """
    Return a catalog connection.

    - duckdb (default): opens/creates CATALOG_PATH as a DuckDB file.
    - postgresql: connects via VWORLD_PG_HOST/PORT/DB/USER/PASSWORD env vars.
    """
    if CATALOG_TYPE == "postgresql":
        return _connect_postgresql()
    return _connect_duckdb()


def _connect_duckdb() -> CatalogConnection:
    """Open or create the DuckDB catalog file."""
    os.makedirs(os.path.dirname(CATALOG_PATH), exist_ok=True)
    conn = duckdb.connect(CATALOG_PATH)
    logger.info("Catalog: DuckDB at %s", CATALOG_PATH)
    return CatalogConnection(conn, backend="duckdb")


def _connect_postgresql() -> CatalogConnection:
    """Connect to PostgreSQL as DuckLake catalog backend."""
    host = os.getenv("VWORLD_PG_HOST", "localhost")
    port = os.getenv("VWORLD_PG_PORT", "5432")
    dbname = os.getenv("VWORLD_PG_DB", "vworld_catalog")
    user = os.getenv("VWORLD_PG_USER", "vworld")
    password = os.getenv("VWORLD_PG_PASSWORD", "")

    pg_url = f"postgresql://{user}:{password}@{host}:{port}/{dbname}"

    conn = duckdb.connect()
    conn.execute("INSTALL postgres_scanner;")
    conn.execute("LOAD postgres_scanner;")
    conn.execute(f"ATTACH '{pg_url}' AS catalog (TYPE postgres, READ_ONLY false);")

    logger.info("Catalog: PostgreSQL at %s:%s/%s", host, port, dbname)
    return CatalogConnection(conn, backend="postgresql")


def health_check() -> dict:
    """Verify catalog connectivity. Returns status dict."""
    catalog = None
    try:
        catalog = get_catalog()
        catalog.execute("SELECT 1")
        return {"status": "ok", "backend": catalog.backend}
    except Exception as e:
        return {"status": "error", "backend": CATALOG_TYPE, "error": str(e)}
    finally:
        if catalog:
            catalog.close()
