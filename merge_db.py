#!/usr/bin/env python3
"""Merge Windows robot.db into Linux robot.db (incremental, idempotent).

Run:  python3 merge_db.py --dry-run    # preview only
      python3 merge_db.py              # execute merge
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from typing import Any

# Defaults are resolved relative to this file (the project has moved before,
# which silently broke this script). Both are overridable via CLI flags,
# so `--src` / `--dst` is the reliable way to use it.
_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
LINUX_DB = os.path.join(_PROJECT_DIR, "KirikoBot", "robot.db")
WINDOWS_DB = os.path.join(_PROJECT_DIR, "..", "KirikoBot_windows", "KirikoBot", "robot.db")


def _row_exists(cur: sqlite3.Cursor, table: str, where: str, params: tuple) -> bool:
    cur.execute(f"SELECT 1 FROM \"{table}\" WHERE {where} LIMIT 1", params)
    return cur.fetchone() is not None


# ── Table merge defs: (table, dedup_columns, extra_columns) ──
#  dedup_columns: columns that must match to consider a row a duplicate
#  extra_columns: additional columns to copy (beyond dedup columns)

MERGE_TABLES: list[dict[str, Any]] = [
    {
        "table": "group_messages",
        "dedup": ["group_id", "user_id", "user_name", "content", "timestamp"],
        "extra": ["user_role", "msg_type"],
    },
    {
        "table": "history",
        "dedup": ["user_id", "group_id", "role", "content", "timestamp"],
        "extra": ["tool_calls", "tool_call_id"],
    },
    {
        "table": "tool_usage",
        "dedup": ["tool_name", "user_id", "group_id", "timestamp"],
        "extra": [],
    },
    {
        "table": "learning_log",
        "dedup": ["user_id", "note", "timestamp"],
        "extra": ["user_msg", "ai_text", "tool_name"],
    },
    {
        "table": "tarot_history",
        "dedup": ["user_id", "card_name", "timestamp"],
        "extra": [],
    },
    {
        "table": "stickers",
        "dedup": ["filename"],
        "extra": ["file_hash", "category", "content_desc", "emotion",
                  "file_size", "source_group_id", "source_user_id",
                  "collected_at", "categorized_at"],
    },
    {
        "table": "user_profiles",
        "dedup": ["user_id"],  # special: upsert with latest last_updated
        "extra": ["group_id", "user_name", "profile_json", "message_count", "last_updated"],
        "upsert": True,
    },
    {
        "table": "reminders",
        "dedup": ["user_id", "group_id", "content", "remind_time"],
        "extra": ["user_name", "fired", "repeat_daily", "created_at"],
    },
    {
        "table": "feature_requests",
        "dedup": ["user_id", "request_text"],
        "extra": ["user_name", "group_id", "category", "priority", "status",
                  "ai_summary", "timestamp"],
    },
    {
        "table": "app_versions",
        "dedup": ["version"],
        "extra": ["release_date", "description", "author", "digest_sent", "created_at"],
    },
    {
        "table": "changelog",
        "dedup": [],  # special: join via version string
        "extra": [],
        "special": "changelog",
    },
]


def merge_table(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
    spec: dict[str, Any],
    dry_run: bool,
    stats: dict[str, int],
) -> None:
    table = spec["table"]

    if spec.get("special") == "changelog":
        _merge_changelog(src, dst, dry_run, stats)
        return

    dedup = spec["dedup"]
    extra = spec["extra"]
    all_cols = dedup + extra

    # Read all rows from source
    cols_str = ", ".join(f'"{c}"' for c in all_cols)
    src_rows = src.execute(f"SELECT {cols_str} FROM \"{table}\"").fetchall()

    dst_cur = dst.cursor()
    inserted = 0
    updated = 0
    skipped = 0

    for row in src_rows:
        row_dict = dict(zip(all_cols, row))

        # Build dedup WHERE clause
        where_parts = []
        where_vals = []
        for c in dedup:
            val = row_dict[c]
            if val is None:
                where_parts.append(f"\"{c}\" IS NULL")
            else:
                where_parts.append(f"\"{c}\" = ?")
                where_vals.append(val)

        where = " AND ".join(where_parts)
        exists = _row_exists(dst_cur, table, where, tuple(where_vals))

        if exists:
            if spec.get("upsert"):
                # Check if source is newer
                dst_row = dst.execute(
                    f"SELECT last_updated FROM \"{table}\" WHERE {where}",
                    tuple(where_vals),
                ).fetchone()
                src_updated = row_dict.get("last_updated", "")
                dst_updated = dst_row[0] if dst_row else ""
                if src_updated and (not dst_updated or src_updated > dst_updated):
                    # Update with source data
                    set_parts = [f"\"{c}\" = ?" for c in extra]
                    set_vals = [row_dict[c] for c in extra]
                    if not dry_run:
                        dst.execute(
                            f"UPDATE \"{table}\" SET {', '.join(set_parts)} WHERE {where}",
                            tuple(set_vals + where_vals),
                        )
                    updated += 1
                else:
                    skipped += 1
            else:
                skipped += 1
            continue

        # Insert new row
        placeholders = ", ".join("?" for _ in all_cols)
        if not dry_run:
            dst.execute(
                f"INSERT INTO \"{table}\" ({cols_str}) VALUES ({placeholders})",
                tuple(row_dict[c] for c in all_cols),
            )
        inserted += 1

    if not dry_run:
        dst.commit()

    stats[table] = {"inserted": inserted, "updated": updated, "skipped": skipped}
    print(f"  {table}: +{inserted} inserted, ~{updated} updated, −{skipped} skipped  "
          f"(of {len(src_rows)} source rows)")


def _merge_changelog(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
    dry_run: bool,
    stats: dict[str, int],
) -> None:
    """Merge changelog by resolving version_id via version string."""
    # Build version string → id mapping from destination
    dst_ver_map = {}
    for row in dst.execute("SELECT id, version FROM app_versions"):
        dst_ver_map[row[1]] = row[0]

    src_ver_map = {}
    for row in src.execute("SELECT id, version FROM app_versions"):
        src_ver_map[row[0]] = row[1]  # src_id → version_string

    src_rows = src.execute(
        "SELECT id, version_id, entry_type, title, description, author, created_at "
        "FROM changelog"
    ).fetchall()

    dst_existing = set()
    for row in dst.execute(
        "SELECT c.version_id, c.entry_type, c.title, a.version "
        "FROM changelog c JOIN app_versions a ON c.version_id = a.id"
    ):
        dst_existing.add((row[0], row[1], row[2]))  # (version_id, entry_type, title)

    inserted = 0
    skipped = 0
    for row in src_rows:
        src_cl_id, src_ver_id, entry_type, title, desc, author, created_at = row
        ver_str = src_ver_map.get(src_ver_id, "")
        dst_ver_id = dst_ver_map.get(ver_str)
        if not dst_ver_id:
            skipped += 1
            continue

        # Check duplicate by (dst_version_id, entry_type, title)
        if (dst_ver_id, entry_type, title) in dst_existing:
            skipped += 1
            continue

        if not dry_run:
            dst.execute(
                "INSERT INTO changelog (version_id, entry_type, title, description, author, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (dst_ver_id, entry_type, title, desc, author, created_at),
            )
        inserted += 1
        dst_existing.add((dst_ver_id, entry_type, title))  # track in this batch too

    if not dry_run:
        dst.commit()

    stats["changelog"] = {"inserted": inserted, "updated": 0, "skipped": skipped}
    print(f"  changelog: +{inserted} inserted, −{skipped} skipped  "
          f"(of {len(src_rows)} source rows)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge Windows robot.db into Linux robot.db")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, no writes")
    parser.add_argument("--src", default=WINDOWS_DB, help=f"Source DB (default: {WINDOWS_DB})")
    parser.add_argument("--dst", default=LINUX_DB, help=f"Target DB (default: {LINUX_DB})")
    args = parser.parse_args()

    for label, path in (("source", args.src), ("target", args.dst)):
        if not os.path.isfile(path):
            print(f"✗ {label} database not found: {path}")
            print("  Pass --src / --dst explicitly if it lives elsewhere.")
            sys.exit(1)

    src = sqlite3.connect(args.src)
    dst = sqlite3.connect(args.dst)

    print(f"Source (Windows): {args.src}")
    print(f"Target (Linux):   {args.dst}")
    print(f"Mode: {'DRY-RUN (no writes)' if args.dry_run else 'LIVE MERGE'}")
    print()

    all_stats: dict[str, dict[str, int]] = {}
    total_inserted = 0
    total_updated = 0

    for spec in MERGE_TABLES:
        merge_table(src, dst, spec, args.dry_run, all_stats)

    for s in all_stats.values():
        total_inserted += s["inserted"]
        total_updated += s.get("updated", 0)

    print()
    print(f"{'Would insert' if args.dry_run else 'Inserted'} {total_inserted} rows across all tables")
    if total_updated:
        print(f"{'Would update' if args.dry_run else 'Updated'} {total_updated} rows")

    if args.dry_run:
        print()
        print("✅ Dry-run complete. Run without --dry-run to execute the merge.")

    src.close()
    dst.close()


if __name__ == "__main__":
    main()
