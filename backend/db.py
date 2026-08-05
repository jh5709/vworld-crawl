"""
Shared DuckDB/DuckLake connection helpers.

Single place that knows how to open a connection with the DuckLake
catalog attached, the session timezone pinned, and identifiers quoted.
"""

import os
from contextlib import contextmanager
from pathlib import Path

import duckdb

# Backend directory — all relative paths anchor here, not the CWD.
BASE_DIR = Path(__file__).resolve().parent

CATALOG_ALIAS = "vworld"
TIMEZONE = "Asia/Seoul"


def _resolve(env_var: str, default: Path) -> Path:
    """Resolve a path env var; relative values anchor at the backend dir."""
    raw = os.getenv(env_var)
    p = Path(raw) if raw else default
    return p if p.is_absolute() else (BASE_DIR / p)


def metadata_path() -> Path:
    return _resolve(
        "VWORLD_DUCKLAKE_METADATA_PATH",
        Path("catalog") / "ducklake_metadata.ducklake",
    )


def data_path() -> Path:
    return _resolve("VWORLD_DUCKLAKE_DATA_PATH", Path("vworld_data"))


def connect(*, attach: bool = True, spatial: bool = False,
            require_catalog: bool = True,
            catalog: str | os.PathLike | None = None) -> duckdb.DuckDBPyConnection:
    """Open a DuckDB in-memory connection with extensions + DuckLake ATTACH.

    Args:
        attach: ATTACH the DuckLake catalog as ``vworld``.
        spatial: Also load the spatial extension.
        require_catalog: Raise if the catalog file doesn't exist instead of
            letting DuckLake silently initialize a new empty catalog there.
        catalog: Explicit catalog path override (defaults to metadata_path()).
    """
    db = duckdb.connect(":memory:")
    db.execute(f"SET TimeZone='{TIMEZONE}'")
    db.execute("INSTALL ducklake; LOAD ducklake;")
    if spatial:
        db.execute("INSTALL spatial; LOAD spatial;")
    if attach:
        md = Path(catalog) if catalog else metadata_path()
        if require_catalog and not md.exists():
            db.close()
            raise FileNotFoundError(
                f"DuckLake catalog not found at {md}. Run a pipeline first."
            )
        db.execute(f"ATTACH 'ducklake:{md}' AS {CATALOG_ALIAS}")
    return db


@contextmanager
def ducklake_db(**kwargs):
    """Context manager yielding a connected DuckDB, always closed."""
    db = connect(**kwargs)
    try:
        yield db
    finally:
        db.close()


def quote_ident(name: str) -> str:
    """Quote a SQL identifier (table/column), escaping embedded quotes."""
    return '"' + name.replace('"', '""') + '"'
