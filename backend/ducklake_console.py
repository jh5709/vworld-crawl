"""
DuckLake console operations — table stats, snapshots, compaction, expire.

All connections go through db.py (timezone-pinned, path-anchored).
Expire is catalog-wide: DuckLake expires snapshots for the whole catalog,
not per table, and ducklake_cleanup_old_files reclaims the disk space.
"""

import logging

from db import CATALOG_ALIAS, ducklake_db, quote_ident

logger = logging.getLogger(__name__)


def _metadata_db(db) -> str:
    """Internal metadata database name inside the attached catalog."""
    rows = db.execute(
        "SELECT database_name FROM duckdb_databases() "
        "WHERE database_name LIKE '__ducklake%'"
    ).fetchall()
    if not rows:
        raise RuntimeError("DuckLake metadata database not found")
    return rows[0][0]


def _user_tables(db) -> list[str]:
    """Names of user tables in the catalog (excludes ducklake internals)."""
    return [
        r[0]
        for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'ducklake_%' AND name NOT LIKE '__ducklake%'"
        ).fetchall()
    ]


def _table_change_map(db, md: str) -> dict[str, list[dict]]:
    """Map table name → snapshots that actually changed it.

    ducklake_snapshot_changes.changes_made looks like:
      created_table:"main"."buildings"  /  inserted_into_table:1
    """
    # table_id → current table name
    id_to_name = {
        str(tid): name
        for tid, name in db.execute(
            f"SELECT table_id, table_name FROM {md}.ducklake_table"
        ).fetchall()
    }

    changes: dict[str, list[dict]] = {}
    rows = db.execute(
        f"SELECT s.snapshot_id, s.snapshot_time, c.changes_made "
        f"FROM {md}.ducklake_snapshot_changes c "
        f"JOIN {md}.ducklake_snapshot s USING (snapshot_id) "
        f"ORDER BY s.snapshot_id DESC"
    ).fetchall()

    for snap_id, snap_time, change in rows:
        if not change:
            continue
        for part in change.split(","):
            part = part.strip()
            target = None
            if part.startswith("created_table:"):
                # created_table:"main"."name" → strip schema, unquote
                target = part.split(":", 1)[1].split(".")[-1].strip('"')
            elif "_table:" in part:
                # inserted_into_table:1 / dropped_table:1 / altered_table:1 …
                tid = part.rsplit(":", 1)[1]
                target = id_to_name.get(tid)
            if target:
                changes.setdefault(target, []).append({
                    "snapshot_id": snap_id,
                    "timestamp": str(snap_time) if snap_time else "",
                    "change": part,
                })
    return changes


def table_list() -> list[dict]:
    """List all user tables with stats and latest snapshot version."""
    with ducklake_db() as db:
        md = _metadata_db(db)
        change_map = _table_change_map(db, md)

        result = []
        for name in _user_tables(db):
            q = quote_ident(name)
            count = db.execute(
                f"SELECT count(*) FROM {CATALOG_ALIAS}.{q}"
            ).fetchone()[0]

            file_count, total_size = 0, 0
            try:
                stats = db.execute(
                    f"SELECT count(*), COALESCE(sum(df.file_size_bytes), 0) "
                    f"FROM {md}.ducklake_data_file df "
                    f"JOIN {md}.ducklake_table t USING (table_id) "
                    f"WHERE t.table_name = ? AND df.end_snapshot IS NULL",
                    [name],
                ).fetchone()
                if stats:
                    file_count, total_size = stats[0] or 0, stats[1] or 0
            except Exception:
                pass

            table_changes = change_map.get(name, [])
            result.append({
                "name": name,
                "rows": count,
                "file_count": file_count,
                "total_size": total_size,
                "is_reject": name.endswith("_rejects"),
                "latest_snapshot": table_changes[0]["snapshot_id"]
                    if table_changes else None,
                "last_modified": table_changes[0]["timestamp"]
                    if table_changes else None,
            })
        return result


def table_snapshots(table_name: str) -> list[dict]:
    """Snapshot timeline of changes for one table (not the whole catalog)."""
    with ducklake_db() as db:
        md = _metadata_db(db)
        return _table_change_map(db, md).get(table_name, [])


def table_preview(table_name: str, limit: int = 20, offset: int = 0) -> dict:
    """Preview rows, hiding geometry/blob columns but keeping numerics."""
    with ducklake_db() as db:
        q = quote_ident(table_name)
        total = db.execute(
            f"SELECT count(*) FROM {CATALOG_ALIAS}.{q}"
        ).fetchone()[0]

        cols = db.execute(f"DESCRIBE {CATALOG_ALIAS}.{q}").fetchall()
        display_cols = [c[0] for c in cols if c[1] not in ("GEOMETRY", "BLOB")]
        col_sql = ", ".join(quote_ident(c) for c in display_cols)

        rows = db.execute(
            f"SELECT {col_sql} FROM {CATALOG_ALIAS}.{q} LIMIT ? OFFSET ?",
            [limit, offset],
        ).fetchall()

        return {
            "table": table_name,
            "total_rows": total,
            "columns": display_cols,
            "rows": [dict(zip(display_cols, r)) for r in rows],
        }


def compact_table(table_name: str) -> dict:
    """Merge adjacent Parquet files for a table."""
    try:
        with ducklake_db() as db:
            db.execute(
                f"CALL ducklake_merge_adjacent_files(?, ?)",
                [CATALOG_ALIAS, table_name],
            )
        return {"success": True, "message": f"Compacted {table_name}"}
    except Exception as e:
        logger.warning("Compact failed for %s: %s", table_name, e)
        return {"success": False, "error": str(e)}


def reindex_table(table_name: str) -> dict:
    """Rewrite data files to rebuild metadata/stats (closest to reindex)."""
    try:
        with ducklake_db() as db:
            db.execute(
                f"CALL ducklake_rewrite_data_files(?, ?)",
                [CATALOG_ALIAS, table_name],
            )
        return {"success": True, "message": f"Rewrote data files for {table_name}"}
    except Exception as e:
        logger.warning("Reindex failed for %s: %s", table_name, e)
        return {"success": False, "error": str(e)}


def expire_snapshots(days: int = 30, dry_run: bool = False) -> dict:
    """Expire catalog snapshots older than N days, then reclaim disk space.

    Catalog-wide by design (DuckLake expires per catalog, not per table).
    dry_run=True previews which snapshots would be expired.
    """
    try:
        with ducklake_db() as db:
            md = _metadata_db(db)
            threshold = f"now() - INTERVAL '{int(days)} days'"

            if dry_run:
                rows = db.execute(
                    f"CALL ducklake_expire_snapshots(?, "
                    f"older_than => {threshold}, dry_run => true)",
                    [CATALOG_ALIAS],
                ).fetchall()
                return {
                    "success": True,
                    "dry_run": True,
                    "snapshots_expired": len(rows),
                    "snapshot_ids": [r[0] for r in rows],
                    "message": f"{len(rows)} snapshot(s) older than {days} "
                               f"day(s) would be expired.",
                }

            rows = db.execute(
                f"CALL ducklake_expire_snapshots(?, older_than => {threshold})",
                [CATALOG_ALIAS],
            ).fetchall()
            expired = len(rows)

            # Bytes scheduled for deletion → freed by cleanup
            freed = db.execute(
                f"SELECT COALESCE(sum(df.file_size_bytes), 0) "
                f"FROM {md}.ducklake_files_scheduled_for_deletion fsd "
                f"JOIN {md}.ducklake_data_file df USING (data_file_id)"
            ).fetchone()[0] or 0

            db.execute(f"CALL ducklake_cleanup_old_files(?)", [CATALOG_ALIAS])

            return {
                "success": True,
                "dry_run": False,
                "snapshots_expired": expired,
                "freed_bytes": freed,
                "message": f"Expired {expired} snapshot(s), reclaimed "
                           f"{freed:,} bytes.",
            }
    except FileNotFoundError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.warning("Expire snapshots failed: %s", e)
        return {"success": False, "error": str(e)}
