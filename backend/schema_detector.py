"""
Shapefile schema detection using DuckDB Spatial extension.

Reads .shp and .zip (unzipped internally) files via DuckDB's ST_Read,
returning CRS, column names/types, geometry type, row count, and preview rows.

Zero extra dependencies — DuckDB is already in the stack per ADR #2.
"""

import os
import zipfile
import tempfile
import shutil
import logging
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

import duckdb

logger = logging.getLogger(__name__)

# In-memory DuckDB connection singleton (created once, reused)
_conn: duckdb.DuckDBPyConnection | None = None


def _get_conn() -> duckdb.DuckDBPyConnection:
    """Return (or create) an in-memory DuckDB connection with spatial loaded."""
    global _conn
    if _conn is None:
        _conn = duckdb.connect(":memory:")
        _conn.execute("INSTALL spatial;")
        _conn.execute("LOAD spatial;")
    return _conn


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class ColumnInfo:
    name: str
    type: str  # Python-friendly: "VARCHAR", "INTEGER", "DOUBLE", "DATE", etc.
    width: int = 0  # display width hint


@dataclass
class FileInfo:
    name: str
    size: int
    date: str  # ISO format
    path: str


@dataclass
class SchemaResult:
    path: str
    crs: str = ""           # e.g. "EPSG:5186"
    crs_description: str = ""
    columns: list[ColumnInfo] = field(default_factory=list)
    geometry_type: str = ""  # e.g. "LINESTRING", "POINT", "POLYGON"
    row_count: int = 0
    valid_count: int = 0      # rows with valid geometry
    invalid_count: int = 0    # rows with invalid geometry
    invalid_sample: str = ""  # ST_IsValidReason for first invalid row
    error: str = ""


# ---------------------------------------------------------------------------
# Unzip utilities
# ---------------------------------------------------------------------------

class UnzipError(Exception):
    """Signal an unzip failure with a user-facing message."""


def _unzip_shapefile(zip_path: str) -> str:
    """
    Extract a zip file to a temp directory and return the path to the .shp inside.

    Raises UnzipError for all failure modes (corrupt zip, no .shp, permissions, etc.).
    Caller is responsible for cleaning up the temp directory.
    """
    # Check existence + readability
    if not os.path.exists(zip_path):
        raise UnzipError(f"File not found: {zip_path}")
    if not os.access(zip_path, os.R_OK):
        raise UnzipError(f"Cannot read file (permission denied): {zip_path}")

    tmpdir = tempfile.mkdtemp(prefix="vworld_unzip_")

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            # Check for zip bombs: total uncompressed size
            total_size = sum(info.file_size for info in zf.infolist())
            max_size = 500 * 1024 * 1024  # 500 MB limit
            if total_size > max_size:
                raise UnzipError(
                    f"Zip contents too large ({total_size / 1024 / 1024:.0f} MB). "
                    f"Maximum is {max_size / 1024 / 1024:.0f} MB."
                )

            # Check file count
            if len(zf.infolist()) == 0:
                raise UnzipError("Zip file is empty (no files inside).")

            zf.extractall(tmpdir)

    except zipfile.BadZipFile:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise UnzipError("Corrupt or invalid zip file. The file may be truncated or not a zip archive.")
    except NotImplementedError:
        # e.g. ZIP64 with unsupported compression
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise UnzipError("Unsupported zip format (e.g. ZIP64 or unknown compression method).")
    except PermissionError:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise UnzipError(f"Cannot write extracted files to temp directory: {tmpdir}")
    except OSError as e:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise UnzipError(f"Failed to read zip file: {e}")
    except UnzipError:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise

    # Find the .shp file (walk directory tree for nested zips)
    shp_path: str | None = None
    for root, _dirs, files in os.walk(tmpdir):
        for fname in files:
            if fname.lower().endswith(".shp") and not fname.startswith("._"):
                if shp_path is not None:
                    # Multiple .shp files — take the first one and warn
                    logger.warning(
                        "Multiple .shp files in zip, using first: %s", shp_path
                    )
                    break
                shp_path = os.path.join(root, fname)
        if shp_path:
            break

    if shp_path is None:
        # List what WAS in the zip to help debugging
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                contents = ", ".join(zf.namelist()[:20])
            detail = f" Found: {contents}"
        except Exception:
            detail = ""
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise UnzipError(f"No shapefile (.shp) found in zip archive.{detail}")

    return shp_path


# ---------------------------------------------------------------------------
# DuckDB-based schema detection
# ---------------------------------------------------------------------------

def _read_with_duckdb(path: str, conn: duckdb.DuckDBPyConnection) -> SchemaResult:
    """
    Read schema from a .shp file using DuckDB's ST_Read.

    Args:
        path: Absolute path to a .shp file (NOT inside a zip — must be on disk).
        conn: DuckDB connection with spatial extension loaded.
    """
    result = SchemaResult(path=path)

    try:
        # DuckDB creates a virtual table from the shapefile
        # Use a temp view name to avoid collisions
        view_name = f"__vworld_schema_{os.getpid()}"

        # Get row count + geometry type from a single scan
        conn.execute(
            f"CREATE OR REPLACE VIEW {view_name} AS "
            f"SELECT * EXCLUDE (OGC_FID) FROM ST_Read('{_sql_escape(path)}')",
        )

        # Row count
        row_count = conn.execute(f"SELECT count(*) FROM {view_name}").fetchone()[0]
        result.row_count = row_count

        if row_count == 0:
            # Empty shapefile — still get schema from DESCRIBE
            cols = conn.execute(f"DESCRIBE {view_name}").fetchall()
            for col_name, col_type, _, _, _, _ in cols:
                if col_name not in ("geom", "OGC_FID"):
                    result.columns.append(ColumnInfo(
                        name=col_name,
                        type=col_type.upper(),
                        width=_type_width(col_type),
                    ))
            result.geometry_type = "(empty)"
            result.crs = "(empty)"
            conn.execute(f"DROP VIEW IF EXISTS {view_name}")
            return result

        # Schema from DESCRIBE
        cols = conn.execute(f"DESCRIBE {view_name}").fetchall()
        for col_name, col_type, _, _, _, _ in cols:
            # Skip geometry column and GDAL artifact columns
            if col_name in ("geom", "OGC_FID"):
                continue
            result.columns.append(ColumnInfo(
                name=col_name,
                type=col_type.upper(),
                width=_type_width(col_type),
            ))

        # Geometry type from first row
        geom_type_row = conn.execute(
            f"SELECT ST_GeometryType(geom) FROM {view_name} LIMIT 1"
        ).fetchone()
        if geom_type_row:
            result.geometry_type = _normalize_geom_type(geom_type_row[0])

        # CRS from first row's geometry
        crs_row = conn.execute(
            f"SELECT ST_CRS(geom) FROM {view_name} LIMIT 1"
        ).fetchone()
        if crs_row and crs_row[0]:
            result.crs = crs_row[0]
            result.crs_description = crs_row[0]

        # Geometry validity: count valid/invalid + sample reason
        try:
            valid_row = conn.execute(
                f"SELECT count(*) FILTER (WHERE ST_IsValid(geom)), "
                f"       count(*) FILTER (WHERE NOT ST_IsValid(geom)) "
                f"FROM {view_name}"
            ).fetchone()
            if valid_row:
                result.valid_count = valid_row[0] or 0
                result.invalid_count = valid_row[1] or 0
            if result.invalid_count > 0:
                reason_row = conn.execute(
                    f"SELECT ST_IsValidReason(geom) FROM {view_name} "
                    f"WHERE NOT ST_IsValid(geom) LIMIT 1"
                ).fetchone()
                if reason_row and reason_row[0]:
                    result.invalid_sample = reason_row[0]
        except Exception:
            pass  # ST_IsValid may not be available in all spatial builds

        conn.execute(f"DROP VIEW IF EXISTS {view_name}")

    except Exception as e:
        # Clean up view if it exists
        try:
            conn.execute(f"DROP VIEW IF EXISTS {view_name}")
        except Exception:
            pass
        logger.error("DuckDB failed to read %s: %s", path, e)
        result.error = _friendly_db_error(str(e), path)
        result.columns = []
        result.row_count = 0

    return result


def _normalize_geom_type(raw: str) -> str:
    """Normalize DuckDB geometry type string to standard form."""
    if not raw:
        return ""
    # ST_GeometryType returns e.g. "LINESTRING", "MULTIPOLYGON", "POINT"
    return raw.strip().upper()


def _type_width(col_type: str) -> int:
    """Suggested display width for a DuckDB column type."""
    t = col_type.upper()
    if "VARCHAR" in t:
        return 80
    if t in ("INTEGER", "BIGINT", "SMALLINT", "TINYINT", "HUGEINT"):
        return 12
    if t in ("DOUBLE", "FLOAT", "REAL", "DECIMAL"):
        return 16
    if t == "DATE":
        return 12
    if t == "TIMESTAMP" or "TIMESTAMP" in t:
        return 20
    if t == "BOOLEAN":
        return 8
    return 20


def _sql_escape(s: str) -> str:
    """Escape a path for safe embedding in a DuckDB SQL string literal."""
    return s.replace("'", "''")


def _friendly_db_error(msg: str, path: str) -> str:
    """Convert DuckDB error messages to user-friendly strings."""
    lower = msg.lower()
    if "no such file" in lower or "cannot open" in lower or "could not open" in lower:
        return f"File not found or cannot be opened: {path}"
    if "permission denied" in lower:
        return f"Permission denied reading: {path}"
    if "not recognized" in lower or "unsupported" in lower:
        return f"Unsupported file format. Expected a valid shapefile (.shp)."
    # Truncate long DuckDB errors
    if len(msg) > 300:
        return msg[:300] + "..."
    return msg


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_schema(path: str, conn: duckdb.DuckDBPyConnection | None = None) -> SchemaResult:
    """
    Detect schema from a shapefile (.shp) or zipped shapefile (.zip).

    Uses DuckDB's ST_Read for .shp files. For .zip files, unzips to a
    temp directory first, then reads the extracted .shp.

    Args:
        path: Path to a .shp or .zip file.
        conn: Optional DuckDB connection (uses a shared in-memory instance if omitted).

    Returns:
        SchemaResult with CRS, columns, geometry type, and row count.
    """
    db = conn if conn is not None else _get_conn()

    path_lower = path.lower()
    tmpdir: str | None = None

    try:
        if path_lower.endswith(".zip"):
            shp_path = _unzip_shapefile(path)
            tmpdir = os.path.dirname(shp_path)
            # The temp dir is the grandparent if the zip has a subdirectory
            # Walk up to find the tmpdir we created
            while tmpdir and not tmpdir.startswith(tempfile.gettempdir()):
                tmpdir = os.path.dirname(tmpdir)
            result = _read_with_duckdb(shp_path, db)
            # Override path so the client sees the original zip path
            result.path = path
        elif path_lower.endswith(".shp"):
            result = _read_with_duckdb(path, db)
        else:
            return SchemaResult(
                path=path,
                error=f"Unsupported file type: {path}. Expected .shp or .zip",
            )
    except UnzipError as e:
        result = SchemaResult(path=path, error=str(e))
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)

    return result


def scan_directory(
    dir_path: str,
    extensions: tuple = (".zip", ".shp"),
) -> list[FileInfo]:
    """
    Scan a directory for supported files.

    Args:
        dir_path: Path to scan.
        extensions: File extensions to include.

    Returns:
        List of FileInfo sorted by name.

    Raises:
        FileNotFoundError: If dir_path does not exist.
        NotADirectoryError: If dir_path is not a directory.
    """
    p = Path(dir_path)
    if not p.exists():
        raise FileNotFoundError(f"Directory not found: {dir_path}")
    if not p.is_dir():
        raise NotADirectoryError(f"Not a directory: {dir_path}")

    files: list[FileInfo] = []
    for ext in extensions:
        for fpath in sorted(p.glob(f"*{ext}")):
            try:
                stat = fpath.stat()
                files.append(FileInfo(
                    name=fpath.name,
                    size=stat.st_size,
                    date=_format_ts(stat.st_mtime),
                    path=str(fpath),
                ))
            except OSError as e:
                logger.warning("Skipping unreadable file %s: %s", fpath, e)

    files.sort(key=lambda f: f.name)
    return files


def _format_ts(ts: float) -> str:
    """Format a timestamp as ISO date string."""
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------

def preview_rows(
    path: str,
    columns: list[dict],
    limit: int = 10,
    conn: duckdb.DuckDBPyConnection | None = None,
) -> dict:
    """
    Read first N rows from a shapefile/zip and apply column mapping.

    Args:
        path: Path to .shp or .zip.
        columns: List of {original, renamed, drop} objects.
        limit: Maximum rows to return.
        conn: Optional DuckDB connection.

    Returns:
        { columns: [...output column names...], rows: [...row dicts...], total_rows: int }
          or { error: "..." } on failure.
    """
    db = conn if conn is not None else _get_conn()
    tmpdir: str | None = None

    try:
        # Resolve path (unzip if needed)
        read_path = path
        if path.lower().endswith(".zip"):
            read_path = _unzip_shapefile(path)
            tmpdir = os.path.dirname(read_path)
            while tmpdir and not tmpdir.startswith(tempfile.gettempdir()):
                tmpdir = os.path.dirname(tmpdir)

        view_name = f"__vworld_preview_{os.getpid()}"

        # Build rename + drop maps
        rename_map: dict[str, str] = {}
        drop_cols: set[str] = set()
        for c in columns:
            orig = c.get("original", "")
            if c.get("drop", False):
                drop_cols.add(orig)
            elif c.get("renamed", ""):
                rename_map[orig] = c["renamed"]

        # Read all columns from shapefile
        db.execute(
            f"CREATE OR REPLACE VIEW {view_name} AS "
            f"SELECT * EXCLUDE (OGC_FID) FROM ST_Read('{_sql_escape(read_path)}')",
        )
        all_cols = db.execute(f"DESCRIBE {view_name}").fetchall()

        # Build output column list and SELECT expression
        output_cols: list[str] = []
        select_parts: list[str] = []
        for col_name, _col_type, _, _, _, _ in all_cols:
            if col_name in ("geom", "OGC_FID"):
                continue
            if col_name in drop_cols:
                continue
            new_name = rename_map.get(col_name, col_name)
            output_cols.append(new_name)
            if new_name != col_name:
                select_parts.append(f'"{col_name}" AS "{new_name}"')
            else:
                select_parts.append(f'"{col_name}"')

        if not select_parts:
            db.execute(f"DROP VIEW IF EXISTS {view_name}")
            return {"columns": [], "rows": [], "total_rows": 0}

        # Get row count
        total = db.execute(f"SELECT count(*) FROM {view_name}").fetchone()[0]

        # Read preview rows
        select_sql = f"SELECT {', '.join(select_parts)} FROM {view_name} LIMIT {limit}"
        rows_raw = db.execute(select_sql).fetchall()

        # Convert to list of dicts
        rows = []
        for row in rows_raw:
            row_dict: dict[str, object] = {}
            for i, col_name in enumerate(output_cols):
                val = row[i]
                # Convert non-JSON-serializable types
                if isinstance(val, bytes):
                    val = val.hex()[:40] + ("..." if len(val) > 20 else "")
                elif hasattr(val, "isoformat"):
                    val = val.isoformat()
                row_dict[col_name] = val
            rows.append(row_dict)

        db.execute(f"DROP VIEW IF EXISTS {view_name}")

        return {
            "columns": output_cols,
            "rows": rows,
            "total_rows": total,
        }

    except UnzipError as e:
        return {"error": str(e), "columns": [], "rows": []}
    except Exception as e:
        logger.exception("Preview failed for %s", path)
        try:
            db.execute(f"DROP VIEW IF EXISTS __vworld_preview_{os.getpid()}")
        except Exception:
            pass
        return {"error": _friendly_db_error(str(e), path), "columns": [], "rows": []}
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
