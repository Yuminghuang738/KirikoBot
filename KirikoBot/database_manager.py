from __future__ import annotations

import logging
import sqlite3
from typing import Any

logger = logging.getLogger(__name__)

VALID_TABLES = {
    "history", "tarot_history", "tarot_content",
    "group_messages", "user_profiles", "tool_usage",
    "reminders", "learning_log", "feature_requests",
    "app_versions", "changelog", "stickers",
    "user_affection", "user_affection_log",
    "feature_settings",
}


class DatabaseManager:
    def __init__(self, db_file: str = "robot.db") -> None:
        self.db_file = db_file
        self._member_cache: dict[str, list[dict[str, str]]] = {}
        self._create_table()

    def get_connect(self) -> sqlite3.Connection:
        try:
            conn = sqlite3.connect(self.db_file)
            conn.execute("PRAGMA journal_mode=WAL")
            return conn
        except sqlite3.Error:
            logger.exception("Failed to connect to database %s", self.db_file)
            raise

    def _create_table(self) -> None:
        try:
            with self.get_connect() as connect:
                connect.execute(
                    """CREATE TABLE IF NOT EXISTS history(
                        id           INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id      TEXT NOT NULL,
                        group_id     TEXT,
                        role         TEXT NOT NULL,
                        content      TEXT NOT NULL,
                        tool_calls   TEXT,
                        tool_call_id TEXT,
                        timestamp DATETIME DEFAULT (datetime('now', 'localtime'))
                    )"""
                )
                connect.execute(
                    """CREATE TABLE IF NOT EXISTS tarot_history(
                        id        INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id   TEXT NOT NULL,
                        card_name TEXT NOT NULL,
                        timestamp DATETIME DEFAULT (datetime('now', 'localtime'))
                    )"""
                )
                connect.execute(
                    """CREATE TABLE IF NOT EXISTS tarot_content(
                        id        INTEGER PRIMARY KEY AUTOINCREMENT,
                        card_name TEXT NOT NULL,
                        card_text TEXT NOT NULL,
                        card_path TEXT NOT NULL
                    )"""
                )
                connect.execute(
                    """CREATE TABLE IF NOT EXISTS group_messages(
                        id         INTEGER PRIMARY KEY AUTOINCREMENT,
                        group_id   TEXT NOT NULL,
                        user_id    TEXT NOT NULL,
                        user_name  TEXT NOT NULL,
                        user_role  TEXT DEFAULT '',
                        content    TEXT NOT NULL,
                        msg_type   TEXT DEFAULT 'text',
                        timestamp  DATETIME DEFAULT (datetime('now', 'localtime'))
                    )"""
                )
                connect.execute(
                    """CREATE TABLE IF NOT EXISTS user_profiles(
                        id           INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id      TEXT NOT NULL UNIQUE,
                        group_id     TEXT NOT NULL,
                        user_name    TEXT NOT NULL,
                        profile_json TEXT NOT NULL DEFAULT '{}',
                        message_count INTEGER DEFAULT 0,
                        last_updated DATETIME DEFAULT (datetime('now', 'localtime'))
                    )"""
                )
                connect.execute(
                    """CREATE TABLE IF NOT EXISTS reminders(
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id     TEXT NOT NULL,
                        group_id    TEXT,
                        user_name   TEXT NOT NULL,
                        content     TEXT NOT NULL,
                        remind_time TEXT NOT NULL,
                        fired       INTEGER DEFAULT 0,
                        created_at  DATETIME DEFAULT (datetime('now', 'localtime'))
                    )"""
                )
                # Migration: add repeat_daily column for daily recurring reminders
                try:
                    connect.execute(
                        "ALTER TABLE reminders ADD COLUMN repeat_daily INTEGER DEFAULT 0"
                    )
                except sqlite3.OperationalError:
                    pass  # Column already exists
                connect.execute(
                    """CREATE TABLE IF NOT EXISTS tool_usage(
                        id         INTEGER PRIMARY KEY AUTOINCREMENT,
                        tool_name  TEXT NOT NULL,
                        user_id    TEXT NOT NULL,
                        group_id   TEXT,
                        timestamp  DATETIME DEFAULT (datetime('now', 'localtime'))
                    )"""
                )
                connect.execute(
                    """CREATE TABLE IF NOT EXISTS learning_log(
                        id         INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id    TEXT NOT NULL,
                        note       TEXT NOT NULL,
                        user_msg   TEXT DEFAULT '',
                        ai_text    TEXT DEFAULT '',
                        tool_name  TEXT DEFAULT '',
                        timestamp  DATETIME DEFAULT (datetime('now', 'localtime'))
                    )"""
                )
                # Migration: add context columns if they don't exist yet
                for col, col_type in [("user_msg", "TEXT DEFAULT ''"), ("ai_text", "TEXT DEFAULT ''"), ("tool_name", "TEXT DEFAULT ''")]:
                    try:
                        connect.execute(f"ALTER TABLE learning_log ADD COLUMN {col} {col_type}")
                    except sqlite3.OperationalError:
                        pass  # Column already exists
                connect.execute(
                    """CREATE TABLE IF NOT EXISTS feature_requests(
                        id           INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id      TEXT NOT NULL,
                        user_name    TEXT NOT NULL,
                        group_id     TEXT,
                        request_text TEXT NOT NULL,
                        category     TEXT DEFAULT '未分类',
                        priority     TEXT DEFAULT 'normal',
                        status       TEXT DEFAULT 'pending',
                        ai_summary   TEXT DEFAULT '',
                        timestamp    DATETIME DEFAULT (datetime('now', 'localtime'))
                    )"""
                )
                connect.execute(
                    """CREATE TABLE IF NOT EXISTS app_versions(
                        id           INTEGER PRIMARY KEY AUTOINCREMENT,
                        version      TEXT NOT NULL UNIQUE,
                        release_date TEXT NOT NULL,
                        description  TEXT DEFAULT '',
                        author       TEXT DEFAULT 'developer',
                        digest_sent  INTEGER DEFAULT 0,
                        created_at   DATETIME DEFAULT (datetime('now', 'localtime'))
                    )"""
                )
                # Migration: add digest_sent column if not present
                try:
                    connect.execute(
                        "ALTER TABLE app_versions ADD COLUMN digest_sent INTEGER DEFAULT 0"
                    )
                except sqlite3.OperationalError:
                    pass
                connect.execute(
                    """CREATE TABLE IF NOT EXISTS changelog(
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        version_id  INTEGER NOT NULL,
                        entry_type  TEXT NOT NULL DEFAULT 'feature',
                        title       TEXT NOT NULL,
                        description TEXT DEFAULT '',
                        author      TEXT DEFAULT 'developer',
                        created_at  DATETIME DEFAULT (datetime('now', 'localtime')),
                        FOREIGN KEY (version_id) REFERENCES app_versions(id)
                    )"""
                )
                connect.execute(
                    """CREATE TABLE IF NOT EXISTS stickers(
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        filename        TEXT NOT NULL UNIQUE,
                        file_hash       TEXT NOT NULL,
                        category        TEXT DEFAULT '未分类',
                        content_desc    TEXT DEFAULT '',
                        emotion         TEXT DEFAULT '',
                        file_size       INTEGER DEFAULT 0,
                        source_group_id TEXT,
                        source_user_id  TEXT,
                        categorized_at  DATETIME,
                        collected_at    DATETIME DEFAULT (datetime('now', 'localtime'))
                    )"""
                )
                connect.execute(
                    """CREATE TABLE IF NOT EXISTS user_affection(
                        id                INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id           TEXT NOT NULL,
                        group_id          TEXT NOT NULL,
                        user_name         TEXT NOT NULL,
                        affection_score   REAL DEFAULT 50.0,
                        interaction_count INTEGER DEFAULT 0,
                        positive_count    INTEGER DEFAULT 0,
                        negative_count    INTEGER DEFAULT 0,
                        last_interaction  DATETIME,
                        relationship      TEXT DEFAULT 'neutral',
                        notes             TEXT DEFAULT '',
                        created_at        DATETIME DEFAULT (datetime('now', 'localtime')),
                        updated_at        DATETIME DEFAULT (datetime('now', 'localtime')),
                        UNIQUE(user_id, group_id)
                    )"""
                )
                connect.execute(
                    """CREATE TABLE IF NOT EXISTS user_affection_log(
                        id         INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id    TEXT NOT NULL,
                        group_id   TEXT NOT NULL,
                        date       TEXT NOT NULL,
                        delta      REAL DEFAULT 0,
                        timestamp  DATETIME DEFAULT (datetime('now', 'localtime'))
                    )"""
                )
                # Per-group / per-user feature toggles. settings_json only stores
                # DISABLED keys; missing key or missing row = enabled.
                connect.execute(
                    """CREATE TABLE IF NOT EXISTS feature_settings(
                        scope_type    TEXT NOT NULL,
                        scope_id      TEXT NOT NULL,
                        settings_json TEXT NOT NULL DEFAULT '{}',
                        updated_at    DATETIME DEFAULT (datetime('now', 'localtime')),
                        PRIMARY KEY (scope_type, scope_id)
                    )"""
                )
                # Index for fast lookups
                connect.execute(
                    "CREATE INDEX IF NOT EXISTS idx_gm_user ON group_messages(user_id, group_id)"
                )
                connect.execute(
                    "CREATE INDEX IF NOT EXISTS idx_up_user ON user_profiles(user_id, group_id)"
                )
                connect.execute(
                    "CREATE INDEX IF NOT EXISTS idx_ua_user ON user_affection(user_id, group_id)"
                )
                connect.execute(
                    "CREATE INDEX IF NOT EXISTS idx_ual_user ON user_affection_log(user_id, group_id, date)"
                )
        except sqlite3.Error:
            logger.exception("Failed to create database tables")
            raise

    def fetch_data(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        try:
            with self.get_connect() as connect:
                cursor = connect.execute(sql, params)
                return cursor.fetchall()
        except sqlite3.Error:
            logger.exception("Database query failed: %s", sql)
            raise

    def execute_action(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        try:
            with self.get_connect() as connect:
                connect.execute(sql, params)
        except sqlite3.Error:
            logger.exception("Database write failed: %s", sql)
            raise

    def _validate_table(self, table: str) -> None:
        if table not in VALID_TABLES:
            raise ValueError(f"Invalid table name: {table}")

    def takeout(
        self, table: str, column: str,
        table_format: str | None = None, params: tuple[Any, ...] = (),
    ) -> list[tuple[Any, ...]]:
        self._validate_table(table)
        if table_format:
            sql = f"SELECT {column} FROM {table} WHERE {table_format} ORDER BY ID DESC LIMIT 12"
        else:
            sql = f"SELECT {column} FROM {table} ORDER BY ID DESC LIMIT 12"
        return self.fetch_data(sql, params)

    def deposit(
        self, table: str, column: str,
        table_format: str, params: tuple[Any, ...],
    ) -> None:
        self._validate_table(table)
        sql = f"INSERT INTO {table} {column} VALUES {table_format}"
        self.execute_action(sql, params)

    # ── Chat history ───────────────────────────────────

    def deposit_chat_history(
        self, role: str, user_id: str, group_id: str | None,
        content: str, tool_calls: str, tool_call_id: str,
    ) -> None:
        self.deposit(
            "history", "(role, user_id, group_id, content, tool_calls, tool_call_id)",
            "(?, ?, ?, ?, ?, ?)",
            (role, user_id, group_id, content, tool_calls, tool_call_id),
        )

    def deposit_tarot_history(self, user_id: str, card_name: str) -> None:
        self.deposit("tarot_history", "(user_id, card_name)", "(?, ?)", (user_id, card_name))

    def takeout_chat_history(self, user_id: str, group_id: str | None) -> list[tuple[Any, ...]]:
        if group_id:
            rows = self.takeout(
                "history", "role, content, tool_calls, tool_call_id",
                "user_id = ? AND group_id = ?", (user_id, group_id),
            )
        else:
            rows = self.takeout(
                "history", "role, content, tool_calls, tool_call_id",
                "user_id = ? AND group_id IS NULL", (user_id,),
            )
        rows.reverse()
        return rows

    def takeout_tarot_history(self, user_id: str) -> list[tuple[Any, ...]]:
        return self.takeout("tarot_history", "card_name, timestamp", "user_id = ?", (user_id,))

    # ── Group message recording ────────────────────────

    def record_group_message(
        self, group_id: str, user_id: str, user_name: str,
        content: str, user_role: str = "", msg_type: str = "text",
    ) -> None:
        self.deposit(
            "group_messages",
            "(group_id, user_id, user_name, user_role, content, msg_type)",
            "(?, ?, ?, ?, ?, ?)",
            (group_id, user_id, user_name, user_role, content, msg_type),
        )

    def get_user_messages(
        self, user_id: str, group_id: str, limit: int = 50,
    ) -> list[tuple[Any, ...]]:
        return self.fetch_data(
            "SELECT content, timestamp FROM group_messages "
            "WHERE user_id = ? AND group_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, group_id, limit),
        )

    def get_recent_group_messages(
        self, group_id: str, limit: int = 100,
    ) -> list[tuple[Any, ...]]:
        if group_id:
            return self.fetch_data(
                "SELECT user_id, user_name, content, timestamp FROM group_messages "
                "WHERE group_id = ? ORDER BY id DESC LIMIT ?",
                (group_id, limit),
            )
        return self.fetch_data(
            "SELECT user_id, user_name, content, timestamp FROM group_messages "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        )

    def clean_orphaned_history(self, user_id: str, group_id: str | None) -> int:
        """Remove assistant messages with tool_calls but no follow-up tool response.
        Returns number of rows deleted."""
        try:
            # Find assistant rows that have tool_calls but no matching tool row after them
            if group_id:
                rows = self.fetch_data(
                    "SELECT id, tool_calls FROM history WHERE user_id=? AND group_id=? ORDER BY id",
                    (user_id, group_id),
                )
            else:
                rows = self.fetch_data(
                    "SELECT id, tool_calls FROM history WHERE user_id=? AND group_id IS NULL ORDER BY id",
                    (user_id,),
                )
            deleted = 0
            for i, (row_id, tc) in enumerate(rows):
                if tc and i + 1 < len(rows):
                    next_row = rows[i + 1]
                    # Next row should be a tool response; if tool_calls is empty, it's orphaned
                    # We can't easily check the next row's role here, so just flag rows
                    pass
                # Simple approach: delete tool rows with empty tool_call_id
                if not tc:  # no tool_calls, skip
                    continue
            return deleted
        except Exception:
            logger.exception("clean_orphaned_history failed")
            return 0

    def validate_and_clean_history(self, user_id: str, group_id: str | None) -> None:
        """Remove history entries that would cause DeepSeek 400 errors."""
        try:
            if group_id:
                rows = self.fetch_data(
                    "SELECT id, role, tool_calls, tool_call_id FROM history "
                    "WHERE user_id=? AND group_id=? ORDER BY id",
                    (user_id, group_id),
                )
            else:
                rows = self.fetch_data(
                    "SELECT id, role, tool_calls, tool_call_id FROM history "
                    "WHERE user_id=? AND group_id IS NULL ORDER BY id",
                    (user_id,),
                )
            ids_to_delete: list[int] = []

            for i, row in enumerate(rows):
                row_id, role, tc, tci = row[0], row[1], row[2], row[3]
                if role == "assistant" and tc:
                    nxt = rows[i + 1] if i + 1 < len(rows) else None
                    # nxt = (id, role, tool_calls, tool_call_id), index 1=role, 3=tci
                    if not nxt or nxt[1] != "tool" or not nxt[3]:
                        ids_to_delete.append(row_id)
                elif role == "tool":
                    if not tci:
                        ids_to_delete.append(row_id)
                    elif i == 0:
                        ids_to_delete.append(row_id)
                    else:
                        prev = rows[i - 1]
                        if prev[1] != "assistant" or not prev[2]:
                            ids_to_delete.append(row_id)

            for rid in ids_to_delete:
                self.execute_action("DELETE FROM history WHERE id=?", (rid,))
            if ids_to_delete:
                logger.warning(
                    "Cleaned %d orphaned history rows for user %s", len(ids_to_delete), user_id,
                )
        except Exception:
            logger.exception("validate_and_clean_history failed")

    # ── Group member cache ─────────────────────────────

    def seed_group_members(
        self, group_id: str, members: list[dict[str, Any]],
    ) -> None:
        """Cache group member list from LLBot API."""
        self._member_cache[group_id] = [
            {
                "user_id": str(m.get("user_id", "")),
                "user_name": str(m.get("nickname", "") or m.get("card", "") or ""),
                "role": str(m.get("role", "")),
            }
            for m in members
            if m.get("user_id")
        ]
        logger.info("Cached %d members for group %s", len(self._member_cache[group_id]), group_id)

    def find_member_by_name(
        self, group_id: str, name: str,
    ) -> tuple[str | None, str | None]:
        """Search cached members + DB for a name. Returns (qq, display_name)."""
        # Try DB first (real messages with accurate names)
        result = self.find_user_by_name(group_id, name)
        if result:
            # Get QQ number from DB
            rows = self.fetch_data(
                "SELECT user_id FROM group_messages WHERE group_id=? AND user_name=? ORDER BY id DESC LIMIT 1",
                (group_id, result),
            )
            if rows:
                return (rows[0][0], result)

        # Try cached member list
        cached = self._member_cache.get(group_id, [])
        for m in cached:
            if name in m["user_name"] or m["user_name"] == name:
                return (m["user_id"], m["user_name"])

        return (None, None)

    def find_member_by_role(
        self, group_id: str, role: str,
    ) -> tuple[str | None, str | None]:
        """Search cached members + DB for a role. Returns (qq, display_name)."""
        # Try DB first
        result = self.find_user_by_role(group_id, role)
        if result:
            rows = self.fetch_data(
                "SELECT user_id FROM group_messages WHERE group_id=? AND user_name=? ORDER BY id DESC LIMIT 1",
                (group_id, result),
            )
            if rows:
                return (rows[0][0], result)

        # Try cached member list
        cached = self._member_cache.get(group_id, [])
        for m in cached:
            if m["role"] == role:
                return (m["user_id"], m["user_name"])
        # Owner fallback for admin role
        if role == "admin":
            for m in cached:
                if m["role"] == "owner":
                    return (m["user_id"], m["user_name"])

        return (None, None)

    def find_user_by_role(
        self, group_id: str, role: str,
    ) -> str | None:
        """Find a user in group by role (owner/admin)."""
        try:
            rows = self.fetch_data(
                "SELECT DISTINCT user_name FROM group_messages "
                "WHERE group_id=? AND user_role=? ORDER BY id DESC LIMIT 1",
                (group_id, role),
            )
            return rows[0][0] if rows else None
        except Exception:
            return None

    def find_user_by_name(
        self, group_id: str, name: str,
    ) -> str | None:
        """Fuzzy find a user name in group messages."""
        try:
            rows = self.fetch_data(
                "SELECT user_name, COUNT(*) as cnt FROM group_messages "
                "WHERE group_id=? AND (user_name LIKE ? OR user_name = ?) "
                "GROUP BY user_name ORDER BY cnt DESC LIMIT 3",
                (group_id, f"%{name}%", name),
            )
            return rows[0][0] if rows else None
        except Exception:
            return None

    def get_active_users(
        self, group_id: str, min_messages: int = 10,
    ) -> list[tuple[Any, ...]]:
        return self.fetch_data(
            "SELECT user_id, user_name, COUNT(*) as cnt FROM group_messages "
            "WHERE group_id = ? GROUP BY user_id HAVING cnt >= ? ORDER BY cnt DESC",
            (group_id, min_messages),
        )

    def get_latest_user_name(self, user_id: str, group_id: str | None = None) -> str | None:
        """Return the most recent user_name for a user_id from group_messages."""
        if group_id:
            rows = self.fetch_data(
                "SELECT user_name FROM group_messages WHERE user_id=? AND group_id=? "
                "ORDER BY id DESC LIMIT 1",
                (user_id, group_id),
            )
        else:
            rows = self.fetch_data(
                "SELECT user_name FROM group_messages WHERE user_id=? "
                "ORDER BY id DESC LIMIT 1",
                (user_id,),
            )
        return rows[0][0] if rows else None

    # ── User profiles ──────────────────────────────────

    def save_user_profile(
        self, user_id: str, group_id: str, user_name: str,
        profile_json: str, message_count: int,
    ) -> None:
        # Use the latest user_name from group_messages if available
        latest = self.get_latest_user_name(user_id, group_id)
        effective_name = latest or user_name
        self.execute_action(
            "INSERT INTO user_profiles (user_id, group_id, user_name, profile_json, message_count, last_updated) "
            "VALUES (?, ?, ?, ?, ?, datetime('now', 'localtime')) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "user_name=excluded.user_name, profile_json=excluded.profile_json, "
            "message_count=excluded.message_count, last_updated=datetime('now', 'localtime')",
            (user_id, group_id, effective_name, profile_json, message_count),
        )

    def get_user_profile(self, user_id: str) -> dict[str, Any] | None:
        rows = self.fetch_data(
            "SELECT profile_json, user_name, message_count, last_updated, group_id FROM user_profiles WHERE user_id = ?",
            (user_id,),
        )
        if not rows:
            return None
        import json
        try:
            profile = json.loads(rows[0][0])
        except (json.JSONDecodeError, TypeError):
            profile = {}
        # Resolve current user name from group_messages (handles nick changes)
        latest_name = self.get_latest_user_name(user_id, rows[0][4])
        return {
            "profile": profile,
            "user_name": latest_name or rows[0][1],
            "message_count": rows[0][2],
            "last_updated": rows[0][3],
        }

    # ── Tool usage tracking ────────────────────────────

    def record_tool_usage(self, tool_name: str, user_id: str, group_id: str | None) -> None:
        self.deposit(
            "tool_usage", "(tool_name, user_id, group_id)", "(?, ?, ?)",
            (tool_name, user_id, group_id),
        )

    def get_tool_stats(self) -> list[tuple[Any, ...]]:
        return self.fetch_data(
            "SELECT tool_name, COUNT(*) as cnt FROM tool_usage GROUP BY tool_name ORDER BY cnt DESC"
        )

    def get_total_stats(self) -> dict[str, Any]:
        try:
            msgs = self.fetch_data("SELECT COUNT(*) FROM group_messages")[0][0]
        except Exception:
            msgs = 0
        try:
            chats = self.fetch_data("SELECT COUNT(*) FROM history")[0][0]
        except Exception:
            chats = 0
        try:
            tarots = self.fetch_data("SELECT COUNT(*) FROM tarot_history")[0][0]
        except Exception:
            tarots = 0
        try:
            profiles = self.fetch_data("SELECT COUNT(*) FROM user_profiles")[0][0]
        except Exception:
            profiles = 0
        try:
            stickers = self.fetch_data("SELECT COUNT(*) FROM tarot_content")[0][0]
        except Exception:
            stickers = 0
        return {
            "group_messages": msgs,
            "chat_turns": chats,
            "tarot_draws": tarots,
            "user_profiles": profiles,
            "tarot_cards": stickers,
        }

    def get_all_history(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.fetch_data(
            "SELECT user_id, group_id, role, substr(content,1,200), tool_calls, timestamp "
            "FROM history ORDER BY id DESC LIMIT ?", (limit,)
        )
        return [
            {"user_id": r[0], "group_id": r[1], "role": r[2],
             "content": r[3], "has_tools": bool(r[4]), "time": r[5]}
            for r in rows
        ]

    def get_all_tarot_history(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.fetch_data(
            "SELECT user_id, card_name, timestamp FROM tarot_history ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [{"user_id": r[0], "card": r[1], "time": r[2]} for r in rows]

    def get_all_profiles(self) -> list[dict[str, Any]]:
        rows = self.fetch_data(
            "SELECT user_id, group_id, user_name, profile_json, message_count, last_updated FROM user_profiles ORDER BY message_count DESC"
        )
        import json
        result = []
        for r in rows:
            try:
                p = json.loads(r[3])
            except (json.JSONDecodeError, TypeError):
                p = {}
            result.append({
                "user_id": r[0], "group_id": r[1], "user_name": r[2],
                "profile": p, "msg_count": r[4], "updated": r[5],
            })
        return result

    # ── Stickers ──────────────────────────────────────

    def insert_sticker(self, filename: str, file_hash: str, file_size: int,
                       source_group_id: str = "", source_user_id: str = "") -> None:
        self.execute_action(
            "INSERT OR IGNORE INTO stickers (filename, file_hash, file_size, source_group_id, source_user_id) VALUES (?, ?, ?, ?, ?)",
            (filename, file_hash, file_size, source_group_id or None, source_user_id or None),
        )

    def update_sticker_category(self, filename: str, category: str, content_desc: str = "", emotion: str = "") -> None:
        self.execute_action(
            "UPDATE stickers SET category=?, content_desc=?, emotion=?, categorized_at=datetime('now','localtime') WHERE filename=?",
            (category, content_desc, emotion, filename),
        )

    def get_stickers(self, category: str = "") -> list[dict[str, Any]]:
        if category:
            rows = self.fetch_data(
                "SELECT filename, file_hash, category, content_desc, emotion, file_size, collected_at FROM stickers WHERE category=? ORDER BY collected_at DESC",
                (category,),
            )
        else:
            rows = self.fetch_data(
                "SELECT filename, file_hash, category, content_desc, emotion, file_size, collected_at FROM stickers ORDER BY collected_at DESC"
            )
        return [{"filename": r[0], "file_hash": r[1], "category": r[2],
                 "content_desc": r[3], "emotion": r[4], "file_size": r[5],
                 "collected_at": r[6]} for r in rows]

    def get_uncategorized_stickers(self) -> list[tuple[Any, ...]]:
        return self.fetch_data(
            "SELECT filename, file_hash FROM stickers WHERE category='未分类' OR category='' ORDER BY collected_at DESC"
        )

    def count_stickers_by_category(self) -> list[tuple[Any, ...]]:
        return self.fetch_data(
            "SELECT category, COUNT(*) FROM stickers GROUP BY category ORDER BY COUNT(*) DESC"
        )

    def get_group_profiles(self, group_id: str) -> list[dict[str, Any]]:
        rows = self.fetch_data(
            "SELECT user_id, user_name, profile_json, message_count FROM user_profiles "
            "WHERE group_id = ? ORDER BY message_count DESC LIMIT 20",
            (group_id,),
        )
        import json
        profiles = []
        for user_id, user_name, pj, cnt in rows:
            try:
                p = json.loads(pj)
            except (json.JSONDecodeError, TypeError):
                p = {}
            # Resolve current user name from group_messages (handles nick changes)
            latest_name = self.get_latest_user_name(user_id, group_id)
            profiles.append({
                "user_id": user_id,
                "user_name": latest_name or user_name,
                "profile": p,
                "message_count": cnt,
            })
        return profiles

    def cleanup_orphan_stickers(self, sticker_dir: str) -> int:
        """Remove DB entries for stickers whose files no longer exist on disk.
        Returns number of removed records."""
        import os as _os
        try:
            rows = self.fetch_data("SELECT filename FROM stickers")
            disk_files = set(_os.listdir(sticker_dir))
            orphans = [r[0] for r in rows if r[0] not in disk_files]
            for fname in orphans:
                self.execute_action("DELETE FROM stickers WHERE filename = ?", (fname,))
            if orphans:
                logger.info("Cleaned %d orphan sticker DB records", len(orphans))
            return len(orphans)
        except Exception:
            logger.exception("Failed to clean orphan stickers")
            return 0
