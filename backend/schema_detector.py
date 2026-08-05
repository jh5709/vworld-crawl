"""
Geospatial file schema detection using DuckDB Spatial extension.

Supported formats:
  .shp, .geojson, .gpkg          — read via ST_Read (GDAL)
  .parquet, .geoparquet          — read via read_parquet + WKB decode
  .zip                           — unzipped internally, first supported file inside

Returns CRS, column names/types, geometry type, row count, and preview rows.

Zero extra dependencies — DuckDB is already in the stack per ADR #2.
"""

import os
import json
import zipfile
import tempfile
import shutil
import logging
from pathlib import Path
from dataclasses import dataclass, field

import duckdb

logger = logging.getLogger(__name__)

# Extensions recognized inside zips and by scan_directory, in priority order.
SPATIAL_EXTENSIONS = (".shp", ".gpkg", ".geojson", ".parquet", ".geoparquet")
SCAN_EXTENSIONS = (".zip",) + SPATIAL_EXTENSIONS

PARQUET_EXTENSIONS = (".parquet", ".geoparquet")

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


def _is_parquet(path: str) -> bool:
    return path.lower().endswith(PARQUET_EXTENSIONS)


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


def _unzip_spatial(zip_path: str) -> tuple[str, str]:
    """
    Extract a zip file to a temp directory.

    Returns:
        (path to the first supported spatial file inside, temp dir root).
        The caller MUST clean up the temp dir root.

    Raises UnzipError for all failure modes (corrupt zip, no spatial file, etc.).
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

    # Find the first supported spatial file (priority: SPATIAL_EXTENSIONS order)
    found: str | None = None
    for ext in SPATIAL_EXTENSIONS:
        for root, _dirs, files in os.walk(tmpdir):
            for fname in files:
                if fname.lower().endswith(ext) and not fname.startswith("._"):
                    found = os.path.join(root, fname)
                    break
            if found:
                break
        if found:
            if ext != SPATIAL_EXTENSIONS[0]:
                logger.info("Zip contains %s (no .shp), using %s", ext, found)
            break

    if found is None:
        # List what WAS in the zip to help debugging
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                contents = ", ".join(zf.namelist()[:20])
            detail = f" Found: {contents}"
        except Exception:
            detail = ""
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise UnzipError(
            f"No supported spatial file ({', '.join(SPATIAL_EXTENSIONS)}) "
            f"found in zip archive.{detail}"
        )

    return found, tmpdir


# Backward-compatible alias (single return value, leaks nothing new —
# temp dir is still the mkdtemp root's child; prefer _unzip_spatial).
def _unzip_shapefile(zip_path: str) -> str:
    return _unzip_spatial(zip_path)[0]


# ---------------------------------------------------------------------------
# GeoParquet helpers
# ---------------------------------------------------------------------------

def _parquet_geo_metadata(db: duckdb.DuckDBPyConnection, path: str) -> dict:
    """Parse the GeoParquet 'geo' key-value metadata, if present."""
    try:
        rows = db.execute(
            "SELECT key, value FROM parquet_kv_metadata(?)", [path]
        ).fetchall()
    except Exception:
        return {}
    for key, value in rows:
        k = key.decode() if isinstance(key, bytes) else str(key)
        if k == "geo":
            try:
                v = value.decode() if isinstance(value, bytes) else str(value)
                return json.loads(v)
            except (ValueError, TypeError):
                return {}
    return {}


def parquet_geometry_info(
    db: duckdb.DuckDBPyConnection, path: str
) -> tuple[str, bool] | None:
    """
    Find the geometry column of a (Geo)Parquet file.

    Returns (column_name, needs_wkb_decode) or None if no candidate.
    needs_wkb_decode is False when the column is already a GEOMETRY type
    (DuckDB-native parquet) rather than a WKB BLOB (standard GeoParquet).
    """
    cols = db.execute(
        f"DESCRIBE SELECT * FROM read_parquet('{_sql_escape(path)}')"
    ).fetchall()
    by_name = {c[0]: c[1] for c in cols}

    geo = _parquet_geo_metadata(db, path)
    primary = geo.get("primary_column")
    if primary and primary in by_name:
        return primary, by_name[primary] == "BLOB"

    blob_cols = [c[0] for c in cols if c[1] == "BLOB"]
    geom_typed = [c[0] for c in cols if c[1].startswith("GEOMETRY")]
    for candidate in ("geometry", "geom", "wkb", "geom_wkb"):
        if candidate in blob_cols:
            return candidate, True
        if candidate in geom_typed:
            return candidate, False
    if blob_cols:
        return blob_cols[0], True
    if geom_typed:
        return geom_typed[0], False
    return None


def parquet_geometry_column(db: duckdb.DuckDBPyConnection, path: str) -> str | None:
    """Geometry column name only (see parquet_geometry_info)."""
    info = parquet_geometry_info(db, path)
    return info[0] if info else None


def _parquet_crs(geo_meta: dict) -> str:
    """CRS string from GeoParquet metadata (default per spec: OGC:CRS84)."""
    if not geo_meta:
        return ""
    primary = geo_meta.get("primary_column", "")
    col_meta = geo_meta.get("columns", {}).get(primary, {})
    crs = col_meta.get("crs")
    if crs is None:
        # GeoParquet spec: missing crs means OGC:CRS84
        return "OGC:CRS84"
    if isinstance(crs, dict):
        return crs.get("id", {}).get("code") and f"EPSG:{crs['id']['code']}" or \
               crs.get("name", "")
    return str(crs)


# ---------------------------------------------------------------------------
# DuckDB-based schema detection
# ---------------------------------------------------------------------------

def _create_read_view(db: duckdb.DuckDBPyConnection, view_name: str, path: str) -> str:
    """
    Create a view over any supported file with a GEOMETRY column named 'geom'.

    Returns the CRS string for GeoParquet files ('' otherwise — CRS comes
    from ST_CRS for GDAL formats).
    """
    if _is_parquet(path):
        info = parquet_geometry_info(db, path)
        if not info:
            raise ValueError(
                f"No geometry column found in {os.path.basename(path)} "
                f"(expected WKB BLOB or GEOMETRY column — is this a GeoParquet file?)"
            )
        geom_col, needs_decode = info
        geom_expr = (f'ST_GeomFromWKB("{geom_col}")' if needs_decode
                     else f'"{geom_col}"')
        db.execute(
            f"CREATE OR REPLACE VIEW {view_name} AS "
            f"SELECT * EXCLUDE (\"{geom_col}\"), {geom_expr} AS geom "
            f"FROM read_parquet('{_sql_escape(path)}')"
        )
        return _parquet_crs(_parquet_geo_metadata(db, path))

    # GDAL formats: .shp, .geojson, .gpkg (and anything else ST_Read handles).
    # COLUMNS lambda — EXCLUDE would fail on files without OGC_FID (GPKG).
    db.execute(
        f"CREATE OR REPLACE VIEW {view_name} AS "
        f"SELECT COLUMNS(c -> c != 'OGC_FID') FROM ST_Read('{_sql_escape(path)}')"
    )
    return ""


def _read_with_duckdb(path: str, conn: duckdb.DuckDBPyConnection) -> SchemaResult:
    """
    Read schema from a supported spatial file on disk (not a zip).

    Args:
        path: Absolute path to the file.
        conn: DuckDB connection with spatial extension loaded.
    """
    result = SchemaResult(path=path)
    view_name = f"__vworld_schema_{os.getpid()}"

    try:
        parquet_crs = _create_read_view(conn, view_name, path)

        # Row count
        row_count = conn.execute(f"SELECT count(*) FROM {view_name}").fetchone()[0]
        result.row_count = row_count

        # Schema from DESCRIBE (skip geometry + GDAL artifacts)
        cols = conn.execute(f"DESCRIBE {view_name}").fetchall()
        for col_name, col_type, _, _, _, _ in cols:
            if col_name in ("geom", "OGC_FID"):
                continue
            result.columns.append(ColumnInfo(
                name=col_name,
                type=col_type.upper(),
                width=_type_width(col_type),
            ))

        if row_count == 0:
            result.geometry_type = "(empty)"
            result.crs = parquet_crs or "(empty)"
            conn.execute(f"DROP VIEW IF EXISTS {view_name}")
            return result

        # Geometry type from first row
        geom_type_row = conn.execute(
            f"SELECT ST_GeometryType(geom) FROM {view_name} LIMIT 1"
        ).fetchone()
        if geom_type_row:
            result.geometry_type = _normalize_geom_type(geom_type_row[0])

        # CRS: parquet metadata, else from the geometry itself
        if parquet_crs:
            result.crs = result.crs_description = parquet_crs
        else:
            crs_row = conn.execute(
                f"SELECT ST_CRS(geom) FROM {view_name} LIMIT 1"
            ).fetchone()
            if crs_row and crs_row[0]:
                result.crs = _short_crs(crs_row[0])
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


def _short_crs(raw: str) -> str:
    """Shorten a CRS string for display (PROJJSON from GPKG is verbose)."""
    if not raw:
        return ""
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
            if "id" in data and "code" in data["id"]:
                return f"EPSG:{data['id']['code']}"
            return data.get("name", raw)[:60]
        except (ValueError, TypeError):
            return raw[:60]
    return raw


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
        return (f"Unsupported file format. Expected one of: "
                f"{', '.join(SUPPORTED_EXTENSIONS)}.")
    # Truncate long DuckDB errors
    if len(msg) > 300:
        return msg[:300] + "..."
    return msg


# Public, display-oriented list of supported file types
SUPPORTED_EXTENSIONS = SCAN_EXTENSIONS


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_schema(path: str, conn: duckdb.DuckDBPyConnection | None = None) -> SchemaResult:
    """
    Detect schema from a supported spatial file.

    .zip files are unzipped to a temp directory first; .parquet/.geoparquet
    files are read via read_parquet with WKB geometry decoding; everything
    else goes through ST_Read.

    Args:
        path: Path to the file.
        conn: Optional DuckDB connection (uses a shared in-memory instance if omitted).

    Returns:
        SchemaResult with CRS, columns, geometry type, and row count.
    """
    db = conn if conn is not None else _get_conn()
    path_lower = path.lower()
    tmpdir: str | None = None

    try:
        if path_lower.endswith(".zip"):
            read_path, tmpdir = _unzip_spatial(path)
            result = _read_with_duckdb(read_path, db)
            # Client sees the original zip path
            result.path = path
        elif path_lower.endswith(SPATIAL_EXTENSIONS):
            result = _read_with_duckdb(path, db)
        else:
            return SchemaResult(
                path=path,
                error=f"Unsupported file type: {path}. "
                      f"Expected one of: {', '.join(SUPPORTED_EXTENSIONS)}",
            )
    except UnzipError as e:
        result = SchemaResult(path=path, error=str(e))
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)

    return result


def scan_directory(
    dir_path: str,
    extensions: tuple = SUPPORTED_EXTENSIONS,
) -> list[FileInfo]:
    """
    Scan a directory for supported spatial files.

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
    Read first N rows from a supported spatial file and apply column mapping.

    Args:
        path: Path to the file (.zip unzipped internally).
        columns: List of {original, renamed, drop} objects.
        limit: Maximum rows to return.
        conn: Optional DuckDB connection.

    Returns:
        { columns: [...output column names...], rows: [...row dicts...], total_rows: int }
          or { error: "..." } on failure.
    """
    db = conn if conn is not None else _get_conn()
    tmpdir: str | None = None
    view_name = f"__vworld_preview_{os.getpid()}"

    try:
        # Resolve path (unzip if needed)
        read_path = path
        if path.lower().endswith(".zip"):
            read_path, tmpdir = _unzip_spatial(path)

        # Build rename + drop maps
        rename_map: dict[str, str] = {}
        drop_cols: set[str] = set()
        for c in columns:
            orig = c.get("original", "")
            if c.get("drop", False):
                drop_cols.add(orig)
            elif c.get("renamed", ""):
                rename_map[orig] = c["renamed"]

        # Read all columns (view always has a decoded 'geom' column)
        _create_read_view(db, view_name, read_path)
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
            db.execute(f"DROP VIEW IF EXISTS {view_name}")
        except Exception:
            pass
        return {"error": _friendly_db_error(str(e), path), "columns": [], "rows": []}
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
