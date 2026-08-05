"""
DuckLake operations: extension setup, ATTACH, health verification.

DuckLake provides a lakehouse catalog on top of DuckDB, managing
Parquet-backed tables with snapshot/versioning support.

Usage:
    from ducklake_ops import setup_ducklake

    conn = duckdb.connect(":memory:")
    setup_ducklake(conn, data_path="vworld_data/")
"""

import os
import logging
import duckdb

logger = logging.getLogger(__name__)

DUCKLAKE_CATALOG_NAME = os.getenv("VWORLD_DUCKLAKE_CATALOG", "vworld")
DUCKLAKE_DATA_PATH = os.getenv("VWORLD_DUCKLAKE_DATA_PATH", "vworld_data/")
DUCKLAKE_METADATA_PATH = os.getenv(
    "VWORLD_DUCKLAKE_METADATA_PATH", "catalog/ducklake_metadata.ducklake"
)


def setup_ducklake(
    conn: duckdb.DuckDBPyConnection,
    catalog_name: str = DUCKLAKE_CATALOG_NAME,
    data_path: str = DUCKLAKE_DATA_PATH,
    metadata_path: str = DUCKLAKE_METADATA_PATH,
) -> None:
    """
    Install DuckLake extension (first run) and ATTACH the catalog.

    On first invocation, DuckDB downloads and installs the ducklake
    extension binary. Subsequent calls are no-ops.

    Args:
        conn: An open DuckDB connection.
        catalog_name: Database name for the attached catalog (default: "vworld").
        data_path: Directory where DuckLake stores Parquet data files.
        metadata_path: Path to DuckLake metadata file.
    """
    # Ensure data and metadata directories exist
    os.makedirs(data_path, exist_ok=True)
    os.makedirs(os.path.dirname(metadata_path), exist_ok=True)

    # Install and load DuckLake extension (first-run download; cached thereafter)
    try:
        conn.execute("INSTALL ducklake;")
        conn.execute("LOAD ducklake;")
        logger.info("DuckLake extension loaded successfully.")
    except Exception as e:
        logger.error("Failed to install/load DuckLake extension: %s", e)
        raise

    # ATTACH DuckLake catalog
    attach_sql = (
        f"ATTACH 'ducklake:{metadata_path}' "
        f"AS {catalog_name} (DATA_PATH '{data_path}')"
    )
    try:
        conn.execute(attach_sql)
        logger.info(
            "DuckLake catalog '%s' attached: data=%s, metadata=%s",
            catalog_name,
            data_path,
            metadata_path,
        )
    except Exception as e:
        logger.error("Failed to ATTACH DuckLake catalog: %s", e)
        raise


def ducklake_health(conn: duckdb.DuckDBPyConnection) -> dict:
    """
    Verify DuckLake extension is loaded.

    The catalog is managed by duckle's pipeline runner, not by the app.
    """
    try:
        result = conn.execute(
            "SELECT extension_name FROM duckdb_extensions() "
            "WHERE installed AND loaded AND extension_name = 'ducklake'"
        ).fetchone()
        extension_ok = result is not None

        return {
            "status": "ok" if extension_ok else "degraded",
            "ducklake_extension": extension_ok,
            "note": "Catalog managed by pipeline runner",
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}
