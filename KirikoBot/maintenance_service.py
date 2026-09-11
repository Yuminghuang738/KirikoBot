"""Daily housekeeping: data retention + database backups.

Both jobs used to be manual (a hand-kept ``KirikoBotDatabaseBackup/`` folder and
an ever-growing ``group_messages`` table), which means in practice they never
happened. The scheduler calls :func:`run_daily` once per day.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any

from config import Config

logger = logging.getLogger(__name__)

# Avoids re-running on every scheduler tick (the loop wakes every few seconds).
_last_run: str = ""


def prune_old_data(db: Any, days: int | None = None) -> dict[str, int]:
    """Delete rows older than `days` from the high-volume tables.

    Keeps aggregated data (profiles, affection scores, tool_usage counts)
    intact — only the raw, unbounded tables are trimmed. `days <= 0` disables
    pruning entirely.
    """
    days = Config.RETENTION_DAYS if days is None else days
    if days <= 0:
        return {}
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    deleted: dict[str, int] = {}
    for table in ("group_messages", "history", "user_affection_log", "ai_calls"):
        try:
            before = db.fetch_data(f"SELECT COUNT(*) FROM {table}")[0][0]
            db.execute_action(f"DELETE FROM {table} WHERE timestamp < ?", (cutoff,))
            after = db.fetch_data(f"SELECT COUNT(*) FROM {table}")[0][0]
            if before - after:
                deleted[table] = before - after
        except sqlite3.Error:
            logger.exception("Retention: failed to prune %s", table)

    if deleted:
        logger.info("保留策略：已清理 %d 天前的数据 %s", days, deleted)
    return deleted


def backup_database(db_file: str, dest_dir: str, keep: int = 14) -> str | None:
    """Consistent snapshot of the live DB (WAL-safe) + rotation.

    Uses sqlite3's online backup API rather than copying the file, because a
    plain copy of a WAL database can miss committed pages.
    """
    try:
        os.makedirs(dest_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = os.path.join(dest_dir, f"robot_{stamp}.db")
        # Two backups inside the same second would otherwise overwrite each other.
        suffix = 1
        while os.path.exists(dest):
            dest = os.path.join(dest_dir, f"robot_{stamp}_{suffix}.db")
            suffix += 1

        src = sqlite3.connect(db_file)
        try:
            dst = sqlite3.connect(dest)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()

        _rotate_backups(dest_dir, keep)
        logger.info("数据库已备份：%s", dest)
        return dest
    except (sqlite3.Error, OSError):
        logger.exception("自动备份失败")
        return None


def _rotate_backups(dest_dir: str, keep: int) -> None:
    """Keep only the newest `keep` snapshots."""
    if keep <= 0:
        return
    try:
        snaps = sorted(
            (f for f in os.listdir(dest_dir)
             if f.startswith("robot_") and f.endswith(".db")),
        )
    except OSError:
        return
    for stale in snaps[:-keep]:
        try:
            os.remove(os.path.join(dest_dir, stale))
        except OSError:
            logger.debug("Could not remove old backup %s", stale, exc_info=True)


def run_daily(db: Any, db_file: str) -> None:
    """Run retention + backup at most once per calendar day."""
    global _last_run
    today = date.today().isoformat()
    if _last_run == today:
        return
    _last_run = today

    if Config.RETENTION_DAYS > 0:
        prune_old_data(db, Config.RETENTION_DAYS)
    if Config.BACKUP_ENABLED:
        backup_database(db_file, Config.BACKUP_DIR, Config.BACKUP_KEEP)
