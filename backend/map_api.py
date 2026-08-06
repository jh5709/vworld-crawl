"""
Map preview API — spatial queries against DuckLake tables for deck.gl map.

Endpoints (wired in main.py):
  GET  /api/tables/{name}/bounds     → table-wide bounding box
  GET  /api/tables/{name}/stats      → per-column statistics (whole table, one pass)
  POST /api/tables/{name}/histogram  → value distribution for one column
  POST /api/tables/{name}/features   → GeoJSON features in bounding box, row-limited
  POST /api/tables/{name}/attributes → paginated attribute rows within bbox

Row limits per geometry type (configurable):
  - Points:       10 000
  - LineStrings:   2 000
  - Polygons:        500
These protect against rendering huge tables; the user draws a rectangle to
zoom into areas of interest.
"""

from dataclasses import dataclass
from typing import Optional

from db import ducklake_db, quote_ident

# Row limits per geometry type — tuned for deck.gl rendering performance
DEFAULT_LIMIT_POINT = 10_000
DEFAULT_LIMIT_LINESTRING = 2_000
DEFAULT_LIMIT_POLYGON = 500

NUMERIC_TYPES = {
    "TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT",
    "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT",
    "FLOAT", "DOUBLE", "DECIMAL", "REAL",
}
HISTOGRAM_BINS = 15
CATEGORICAL_TOP_N = 12


@dataclass
class Bounds:
    xmin: float
    ymin: float
    xmax: float
    ymax: float


def _geometry_type(db, table: str) -> str:
    """Detect the dominant geometry type in the table."""
    qt = quote_ident(table)
    db.execute("LOAD spatial")
    results = db.execute(f"""
        SELECT ST_GeometryType(geom) AS gtype, count(*) AS cnt
        FROM (SELECT geom FROM vworld.{qt} WHERE geom IS NOT NULL LIMIT 100)
        GROUP BY gtype ORDER BY cnt DESC LIMIT 1
    """).fetchall()
    if not results:
        return "OTHER"
    return results[0][0].replace("MULTI", "")


def get_bounds(table_name: str) -> Optional[Bounds]:
    """Compute the full-table bounding box from bbox_* columns."""
    with ducklake_db() as db:
        qt = quote_ident(table_name)
        row = db.execute(f"""
            SELECT
                MIN(bbox_xmin) AS xmin,
                MIN(bbox_ymin) AS ymin,
                MAX(bbox_xmax) AS xmax,
                MAX(bbox_ymax) AS ymax
            FROM vworld.{qt}
        """).fetchone()
        if row and row[0] is not None:
            return Bounds(xmin=float(row[0]), ymin=float(row[1]),
                          xmax=float(row[2]), ymax=float(row[3]))
    return None


def get_stats(table_name: str) -> dict:
    """Per-column statistics for the whole table, in a single SUMMARIZE pass."""
    with ducklake_db() as db:
        qt = quote_ident(table_name)
        rows = db.execute(f"SUMMARIZE vworld.{qt}").fetchall()
        # SUMMARIZE columns: column_name, column_type, min, max, approx_unique,
        #                    avg, std, q25, q50, q75, count, null_percentage
        stats: list[dict] = []
        for r in rows:
            name, ctype = r[0], r[1]
            if name in ("geom", "geom_wkb"):
                continue
            count = _to_int(r[10])
            null_pct = _to_float(r[11])
            stats.append({
                "name": name,
                "type": ctype.upper() if ctype else "VARCHAR",
                "count": count,
                "non_null": round(count * (1 - null_pct / 100)) if count is not None and null_pct is not None else None,
                "nulls": round(count * null_pct / 100) if count is not None and null_pct is not None else None,
                "distinct": _to_int(r[4]),
                "min": None if r[2] is None else str(r[2]),
                "max": None if r[3] is None else str(r[3]),
                "avg": _to_float(r[5]),
                "std": _to_float(r[6]),
                "q25": _to_float(r[7]),
                "q50": _to_float(r[8]),
                "q75": _to_float(r[9]),
            })
        return {"table": table_name, "columns": stats}


def get_histogram(table_name: str, column: str) -> dict:
    """Value distribution for one column.

    Numeric columns with many distinct values → equal-width histogram bins.
    Low-cardinality / text columns → top-N categorical counts.
    """
    with ducklake_db() as db:
        qt = quote_ident(table_name)
        qc = quote_ident(column)

        # Validate column exists and get its type
        cols = db.execute(f"DESCRIBE vworld.{qt}").fetchall()
        col_row = next((c for c in cols if c[0] == column), None)
        if col_row is None:
            return {"error": f"Column '{column}' not found"}
        ctype = (col_row[1] or "").upper()
        base_type = ctype.split("(")[0]  # DECIMAL(10,2) → DECIMAL

        total = db.execute(f'SELECT COUNT({qc}) FROM vworld.{qt}').fetchone()[0]
        distinct = db.execute(
            f'SELECT COUNT(DISTINCT {qc}) FROM vworld.{qt}'
        ).fetchone()[0]

        is_numeric = base_type in NUMERIC_TYPES

        if is_numeric and distinct > HISTOGRAM_BINS:
            # Numeric histogram — equal-width bins
            mm = db.execute(
                f'SELECT MIN({qc})::DOUBLE, MAX({qc})::DOUBLE FROM vworld.{qt}'
            ).fetchone()
            lo, hi = float(mm[0]), float(mm[1])
            if lo == hi:
                return {
                    "column": column, "kind": "numeric",
                    "total": total, "distinct": distinct,
                    "bins": [{"label": str(lo), "count": total}],
                }
            bin_rows = db.execute(f"""
                SELECT WIDTH_BUCKET({qc}::DOUBLE, ?, ?, {HISTOGRAM_BINS}) AS bucket,
                       COUNT(*) AS cnt
                FROM vworld.{qt}
                WHERE {qc} IS NOT NULL
                GROUP BY bucket ORDER BY bucket
            """, [lo, hi]).fetchall()
            width = (hi - lo) / HISTOGRAM_BINS
            counts = {int(b): int(c) for b, c in bin_rows}
            bins = []
            for b in range(1, HISTOGRAM_BINS + 1):
                b_lo = lo + (b - 1) * width
                b_hi = lo + b * width
                bins.append({
                    "label": f"{b_lo:g}–{b_hi:g}",
                    "count": counts.get(b, 0),
                })
            return {
                "column": column, "kind": "numeric",
                "total": total, "distinct": distinct,
                "min": lo, "max": hi, "bins": bins,
            }

        # Categorical — top-N values
        val_rows = db.execute(f"""
            SELECT CAST({qc} AS VARCHAR) AS val, COUNT(*) AS cnt
            FROM vworld.{qt}
            GROUP BY val ORDER BY cnt DESC
            LIMIT {CATEGORICAL_TOP_N + 1}
        """).fetchall()
        top = val_rows[:CATEGORICAL_TOP_N]
        other = sum(int(r[1]) for r in val_rows[CATEGORICAL_TOP_N:])
        categories = [{"label": r[0] if r[0] is not None else "(null)",
                       "count": int(r[1])} for r in top]
        if other > 0:
            categories.append({"label": "(other)", "count": other})
        return {
            "column": column, "kind": "categorical",
            "total": total, "distinct": distinct,
            "categories": categories,
        }


def get_features(
    table_name: str,
    xmin: float | None = None,
    ymin: float | None = None,
    xmax: float | None = None,
    ymax: float | None = None,
    limit: int | None = None,
) -> dict:
    """GeoJSON FeatureCollection from the table, optionally filtered by bbox.

    Row limit is auto-determined by geometry type if not specified.
    """
    with ducklake_db() as db:
        db.execute("LOAD spatial")
        qt = quote_ident(table_name)
        geo_type = _geometry_type(db, table_name)

        if limit is None:
            if geo_type == "POINT":
                limit = DEFAULT_LIMIT_POINT
            elif geo_type == "LINESTRING":
                limit = DEFAULT_LIMIT_LINESTRING
            else:
                limit = DEFAULT_LIMIT_POLYGON

        # Build spatial filter
        where_clause = ""
        params: list = []
        if xmin is not None and ymin is not None and xmax is not None and ymax is not None:
            where_clause = """
                WHERE bbox_xmin IS NOT NULL
                  AND bbox_xmax >= ? AND bbox_xmin <= ?
                  AND bbox_ymax >= ? AND bbox_ymin <= ?
            """
            params = [xmin, xmax, ymin, ymax]

        count_row = db.execute(
            f"SELECT COUNT(*) FROM vworld.{qt} {where_clause}",
            params,
        ).fetchone()
        total_matching = count_row[0] if count_row else 0

        cols_info = db.execute(f"DESCRIBE vworld.{qt}").fetchall()
        attr_cols = [
            c[0] for c in cols_info
            if c[0] not in ("geom", "geom_wkb", "bbox_xmin", "bbox_ymin", "bbox_xmax", "bbox_ymax")
        ]

        col_list = ", ".join(quote_ident(c) for c in attr_cols)
        sql = f"""
            SELECT ST_AsGeoJSON(geom)::VARCHAR AS geojson,
                   {col_list}
            FROM vworld.{qt}
            {where_clause}
            LIMIT {limit}
        """
        data = db.execute(sql, params).fetchall()

        import json as _json
        features = []
        for row in data:
            geojson_str = row[0]
            props = {}
            for i, col in enumerate(attr_cols):
                props[col] = _safe_value(row[i + 1])
            try:
                geom = _json.loads(geojson_str)
            except Exception:
                geom = None
            features.append({
                "type": "Feature",
                "geometry": geom,
                "properties": props,
            })

        return {
            "type": "FeatureCollection",
            "features": features,
            "total_matching": total_matching,
            "returned": len(features),
            "bounded": total_matching > len(features),
            "geometry_type": geo_type,
            "limit": limit,
        }


def get_attributes(
    table_name: str,
    xmin: float | None = None,
    ymin: float | None = None,
    xmax: float | None = None,
    ymax: float | None = None,
    offset: int = 0,
    limit: int = 100,
) -> dict:
    """Paginated attribute rows for the attribute table view."""
    with ducklake_db() as db:
        qt = quote_ident(table_name)
        cols_info = db.execute(f"DESCRIBE vworld.{qt}").fetchall()
        attr_cols = [
            c[0] for c in cols_info
            if c[0] not in ("geom", "geom_wkb", "bbox_xmin", "bbox_ymin", "bbox_xmax", "bbox_ymax")
        ]

        where_clause = ""
        params: list = []
        if xmin is not None and ymin is not None and xmax is not None and ymax is not None:
            where_clause = """
                WHERE bbox_xmin IS NOT NULL
                  AND bbox_xmax >= ? AND bbox_xmin <= ?
                  AND bbox_ymax >= ? AND bbox_ymin <= ?
            """
            params = [xmin, xmax, ymin, ymax]

        col_list = ", ".join(quote_ident(c) for c in attr_cols)
        total_row = db.execute(
            f"SELECT COUNT(*) FROM vworld.{qt} {where_clause}", params
        ).fetchone()
        total = total_row[0] if total_row else 0

        rows = db.execute(
            f"SELECT {col_list} FROM vworld.{qt} {where_clause} "
            f"LIMIT {limit} OFFSET {offset}",
            params,
        ).fetchall()

        return {
            "table": table_name,
            "columns": attr_cols,
            "rows": [
                {attr_cols[i]: _safe_value(v) for i, v in enumerate(row)}
                for row in rows
            ],
            "total": total,
            "offset": offset,
            "limit": limit,
        }


def _safe_value(val):
    """Convert DuckDB values to JSON-serializable types."""
    if val is None:
        return None
    if isinstance(val, (int, float, str, bool)):
        return val
    return str(val)


def _to_int(val) -> int | None:
    if val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _to_float(val) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None
