"""
Map preview API — spatial queries against DuckLake tables for deck.gl map.

Endpoints (wired in main.py):
  GET /api/tables/{name}/bounds     → table-wide bounding box
  GET /api/tables/{name}/stats      → per-column statistics (whole table)
  GET /api/tables/{name}/features   → GeoJSON features in bounding box, row-limited
  GET /api/tables/{name}/attributes → paginated attribute rows within bbox

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


@dataclass
class Bounds:
    xmin: float
    ymin: float
    xmax: float
    ymax: float


def _geometry_type(db, table: str) -> str:
    """Detect the dominant geometry type in the table.

    Returns one of 'POINT', 'LINESTRING', 'POLYGON', 'MULTI*', or 'MIXED'.
    """
    qt = quote_ident(table)
    db.execute("LOAD spatial")
    results = db.execute(f"""
        WITH types AS (
            SELECT ST_GeometryType(geom) AS gtype
            FROM vworld.{qt}
            WHERE geom IS NOT NULL
            LIMIT 100
        )
        SELECT gtype, count(*) AS cnt FROM types
        GROUP BY gtype ORDER BY cnt DESC LIMIT 3
    """).fetchall()
    if not results:
        return "OTHER"
    # Simplification: collapse MULTI* prefixes
    primary = results[0][0].replace("MULTI", "")
    mapping = {
        "POINT": "POINT",
        "LINESTRING": "LINESTRING",
        "POLYGON": "POLYGON",
    }
    return mapping.get(primary, primary)


def get_bounds(table_name: str) -> Optional[Bounds]:
    """Compute the full-table bounding box from bbox_* columns."""
    with ducklake_db() as db:
        db.execute("LOAD spatial")
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
    """Per-column statistics for the whole table."""
    with ducklake_db() as db:
        qt = quote_ident(table_name)
        # Get column names and types (DESCRIBE returns: cid, name, type, notnull, dflt_value, pk)
        cols = db.execute(f"DESCRIBE vworld.{qt}").fetchall()
        stats: list[dict] = []
        for row in cols:
            col_name = row[0]  # name is index 0
            col_type = row[1]  # type is index 1
            if col_name in ("geom", "geom_wkb"):
                continue  # skip geometry columns
            qc = quote_ident(col_name)
            try:
                row = db.execute(f"""
                    SELECT
                        COUNT(*) AS cnt,
                        COUNT("{qc}") AS non_null,
                        COUNT(*) - COUNT("{qc}") AS nulls,
                        COUNT(DISTINCT "{qc}") AS distinct_vals,
                        MIN("{qc}")::VARCHAR AS min_val,
                        MAX("{qc}")::VARCHAR AS max_val
                    FROM vworld.{qt}
                """).fetchone()
                if row:
                    stats.append({
                        "name": col_name,
                        "type": col_type.upper() if col_type else "VARCHAR",
                        "count": row[0],
                        "non_null": row[1],
                        "nulls": row[2],
                        "distinct": row[3],
                        "min": row[4],
                        "max": row[5],
                    })
            except Exception:
                # Some types can't be MIN/MAX'd — include what we can
                try:
                    row = db.execute(f"""
                        SELECT COUNT(*) AS cnt,
                               COUNT("{qc}") AS non_null,
                               COUNT(DISTINCT "{qc}") AS distinct_vals
                        FROM vworld.{qt}
                    """).fetchone()
                    if row:
                        stats.append({
                            "name": col_name,
                            "type": col_type.upper() if col_type else "VARCHAR",
                            "count": row[0],
                            "non_null": row[1],
                            "nulls": row[0] - row[1],
                            "distinct": row[2],
                            "min": None,
                            "max": None,
                        })
                except Exception:
                    # Even more basic — just count + type
                    stats.append({
                        "name": col_name,
                        "type": col_type.upper() if col_type else "VARCHAR",
                        "count": None,
                        "non_null": None,
                        "nulls": None,
                        "distinct": None,
                        "min": None,
                        "max": None,
                    })
        return {"table": table_name, "columns": stats}


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

        # Count total matching rows
        count_row = db.execute(
            f"SELECT COUNT(*) FROM vworld.{qt} {where_clause}",
            params,
        ).fetchone()
        total_matching = count_row[0] if count_row else 0

        # Fetch features — build GeoJSON in Python from DESCRIBE columns
        cols_info = db.execute(f"DESCRIBE vworld.{qt}").fetchall()
        attr_cols = [
            c[0] for c in cols_info  # name at index 0
            if c[0] not in ("geom", "geom_wkb", "bbox_xmin", "bbox_ymin", "bbox_xmax", "bbox_ymax")
        ]

        # Build query to get GeoJSON geometries + attribute columns
        col_list = ", ".join(quote_ident(c) for c in attr_cols)
        sql = f"""
            SELECT ST_AsGeoJSON(geom)::VARCHAR AS geojson,
                   {col_list}
            FROM vworld.{qt}
            {where_clause}
            LIMIT {limit}
        """
        data = db.execute(sql, params).fetchall()

        features = []
        for row in data:
            geojson_str = row[0]
            props = {}
            for i, col in enumerate(attr_cols):
                val = row[i + 1]
                # Convert Python types to JSON-compatible
                if isinstance(val, (int, float, str, bool, type(None))):
                    props[col] = val
                else:
                    props[col] = str(val)
            features.append({
                "type": "Feature",
                "geometry": None,  # will parse below
                "properties": props,
                "_geojson_geom": geojson_str,
            })
            # Parse the GeoJSON geometry
            import json as _json
            try:
                features[-1]["geometry"] = _json.loads(geojson_str)
            except Exception:
                features[-1]["geometry"] = None
            del features[-1]["_geojson_geom"]

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
        db.execute("LOAD spatial")
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
            f"ORDER BY 1 LIMIT {limit} OFFSET {offset}",
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
