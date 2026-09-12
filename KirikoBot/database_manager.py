from __future__ import annotations

import logging
import sqlite3
import threading
import time
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

VALID_TABLES = {
    "history", "tarot_history", "tarot_content",
    "group_messages", "user_profiles", "tool_usage",
    "reminders", "learning_log", "feature_requests",
    "app_versions", "changelog", "stickers",
    "user_affection", "user_affection_log",
    "feature_settings", "bot_messages", "ai_calls", "group_subscriptions", "profile_history",
}


def _thread_title(messages: list[dict[str, Any]]) -> str:
    """A short label for a thread — first non-empty member line."""
    for m in messages:
        text = (m.get("content") or "").strip()
        if text and not m.get("is_bot"):
            return text[:24] + ("…" if len(text) > 24 else "")
    return "(机器人发言)"


class DatabaseManager:
    def __init__(self, db_file: str = "robot.db") -> None:
        self.db_file = db_file
        self._member_cache: dict[str, list[dict[str, str]]] = {}
        # One connection per thread. Previously every call opened a new
        # sqlite3 connection and — because `with conn:` only commits, it does
        # not close — leaked it until GC. The app runs a 16-thread WSGI pool
        # plus a 12-thread worker pool, so those added up.
        self._local = threading.local()
        self._create_table()

    def get_connect(self) -> sqlite3.Connection:
        """Thread-local connection. Use as a context manager:

            with db.get_connect() as conn:   # commits / rolls back on exit
                ...
        """
        conn = getattr(self._local, "conn", None)
        if conn is None:
            try:
                conn = sqlite3.connect(self.db_file, timeout=15)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                self._local.conn = conn
            except sqlite3.Error:
                logger.exception("Failed to connect to database %s", self.db_file)
                raise
        return conn

    def close(self) -> None:
        """Close this thread's connection (call on shutdown / in tests)."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            finally:
                self._local.conn = None

    @staticmethod
    def _migrate_user_profiles(connect: sqlite3.Connection) -> None:
        """Rebuild user_profiles with a composite (user_id, group_id) key.

        The original schema was `user_id TEXT NOT NULL UNIQUE` plus a single
        group_id column, so a user active in several groups could only ever
        keep ONE profile — it was overwritten each time they spoke elsewhere,
        and get_group_profiles() silently lost them in the other groups.

        SQLite cannot drop a UNIQUE constraint, so the table is rebuilt. The
        index is dropped explicitly because renaming a table keeps its indexes
        attached to the renamed table, which would make the later
        `CREATE INDEX IF NOT EXISTS` a no-op.
        """
        row = connect.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='user_profiles'"
        ).fetchone()
        table_sql = (row[0] or "") if row else ""
        if "(user_id, group_id)" in table_sql or "UNIQUE(user_id, group_id)" in table_sql:
            return

        logger.info("Migrating user_profiles to UNIQUE(user_id, group_id)")
        connect.execute("ALTER TABLE user_profiles RENAME TO user_profiles_old")
        connect.execute("DROP INDEX IF EXISTS idx_up_user")
        connect.execute(
            """CREATE TABLE user_profiles(
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      TEXT NOT NULL,
                group_id     TEXT NOT NULL,
                user_name    TEXT NOT NULL,
                profile_json TEXT NOT NULL DEFAULT '{}',
                message_count INTEGER DEFAULT 0,
                last_updated DATETIME DEFAULT (datetime('now', 'localtime')),
                UNIQUE(user_id, group_id)
            )"""
        )
        connect.execute(
            """INSERT OR IGNORE INTO user_profiles
                   (user_id, group_id, user_name, profile_json, message_count, last_updated)
               SELECT user_id, group_id, user_name, profile_json, message_count, last_updated
               FROM user_profiles_old"""
        )
        connect.execute("DROP TABLE user_profiles_old")
        logger.info("user_profiles migration done")

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
                # Migration: message id + quote linkage. Enables quote-aware
                # context ("user B is replying to what you said") and recall.
                # message_seq is the QQ seq that a reply segment references;
                # message_id is LLBot's short id used by delete_msg/get_msg.
                # ts_exact is a sub-second epoch stamp: `timestamp` only has
                # second resolution, which is too coarse to interleave a bot
                # reply with the member message it answers.
                for col, col_type in [
                    ("message_id", "INTEGER"),
                    ("message_seq", "INTEGER"),
                    ("reply_to_seq", "INTEGER"),
                    ("ts_exact", "REAL"),
                ]:
                    try:
                        connect.execute(
                            f"ALTER TABLE group_messages ADD COLUMN {col} {col_type}"
                        )
                    except sqlite3.OperationalError:
                        pass  # Column already exists
                connect.execute(
                    """CREATE TABLE IF NOT EXISTS profile_history(
                        id           INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id      TEXT NOT NULL,
                        group_id     TEXT NOT NULL,
                        profile_json TEXT NOT NULL,
                        message_count INTEGER DEFAULT 0,
                        recorded_at  DATETIME DEFAULT (datetime('now', 'localtime'))
                    )"""
                )
                connect.execute(
                    """CREATE TABLE IF NOT EXISTS group_subscriptions(
                        group_id        TEXT NOT NULL,
                        topic           TEXT NOT NULL,
                        push_time       TEXT DEFAULT '07:00',
                        enabled         INTEGER DEFAULT 1,
                        last_fired_date TEXT DEFAULT '',
                        PRIMARY KEY (group_id, topic)
                    )"""
                )
                connect.execute(
                    """CREATE TABLE IF NOT EXISTS ai_calls(
                        id                INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp         DATETIME DEFAULT (datetime('now', 'localtime')),
                        source            TEXT DEFAULT '',
                        kind              TEXT DEFAULT 'chat',
                        model             TEXT DEFAULT '',
                        group_id          TEXT DEFAULT '',
                        prompt_tokens     INTEGER DEFAULT 0,
                        completion_tokens INTEGER DEFAULT 0,
                        reasoning_tokens  INTEGER DEFAULT 0,
                        cache_hit_tokens  INTEGER DEFAULT 0,
                        cache_miss_tokens INTEGER DEFAULT 0,
                        latency_ms        INTEGER DEFAULT 0,
                        success           INTEGER DEFAULT 1,
                        error             TEXT DEFAULT '',
                        utc_hour          INTEGER,
                        utc_weekday       INTEGER
                    )"""
                )
                connect.execute(
                    "CREATE INDEX IF NOT EXISTS idx_ai_ts ON ai_calls(timestamp)"
                )
                connect.execute(
                    """CREATE TABLE IF NOT EXISTS bot_messages(
                        id         INTEGER PRIMARY KEY AUTOINCREMENT,
                        group_id   TEXT NOT NULL,
                        message_id INTEGER,
                        text       TEXT DEFAULT '',
                        recalled   INTEGER DEFAULT 0,
                        ts_exact   REAL,
                        created_at DATETIME DEFAULT (datetime('now', 'localtime'))
                    )"""
                )
                connect.execute(
                    """CREATE TABLE IF NOT EXISTS user_profiles(
                        id           INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id      TEXT NOT NULL,
                        group_id     TEXT NOT NULL,
                        user_name    TEXT NOT NULL,
                        profile_json TEXT NOT NULL DEFAULT '{}',
                        message_count INTEGER DEFAULT 0,
                        last_updated DATETIME DEFAULT (datetime('now', 'localtime')),
                        UNIQUE(user_id, group_id)
                    )"""
                )
                self._migrate_user_profiles(connect)
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
                # Migration: keep enough of each invocation to replay the chain
                # ("which tool with what args, and what came back"), so the bot
                # can explain what it just did when asked.
                for col, col_type in [
                    ("arguments", "TEXT DEFAULT ''"),
                    ("result", "TEXT DEFAULT ''"),
                    ("reasoning", "TEXT DEFAULT ''"),
                ]:
                    try:
                        connect.execute(f"ALTER TABLE tool_usage ADD COLUMN {col} {col_type}")
                    except sqlite3.OperationalError:
                        pass  # Column already exists
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
        message_id: int | None = None, message_seq: int | None = None,
        reply_to_seq: int | None = None,
    ) -> None:
        self.deposit(
            "group_messages",
            "(group_id, user_id, user_name, user_role, content, msg_type, "
            "message_id, message_seq, reply_to_seq, ts_exact)",
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (group_id, user_id, user_name, user_role, content, msg_type,
             message_id, message_seq, reply_to_seq, time.time()),
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

    # ── Bot's own messages (recall + "is this mine?") ────

    # Sort key used to interleave two tables by real time. `timestamp` is only
    # second-precision, so ts_exact (epoch float) is preferred, falling back to
    # a converted julianday for rows written before the column existed.
    _MEMBER_SORT = "IFNULL(ts_exact, (julianday(timestamp) - 2440587.5) * 86400.0)"
    _BOT_SORT = "IFNULL(ts_exact, (julianday(created_at) - 2440587.5) * 86400.0)"

    def record_bot_message(self, group_id: str, message_id: int | None,
                           text: str = "") -> None:
        if message_id is None:
            return
        self.execute_action(
            "INSERT INTO bot_messages (group_id, message_id, text, ts_exact) "
            "VALUES (?, ?, ?, ?)",
            (group_id, message_id, text[:500], time.time()),
        )

    def get_last_bot_message(self, group_id: str, max_age_seconds: int = 110) -> dict[str, Any] | None:
        """Most recent message the bot sent to this group, within the recall window.

        QQ lets a normal member recall their own message for roughly two
        minutes; older ids are useless, so they are filtered out here rather
        than failing at the API.
        """
        try:
            rows = self.fetch_data(
                "SELECT message_id, text, created_at FROM bot_messages "
                "WHERE group_id = ? AND recalled = 0 AND message_id IS NOT NULL "
                "AND created_at >= datetime('now', 'localtime', ?) "
                "ORDER BY id DESC LIMIT 1",
                (group_id, f"-{int(max_age_seconds)} seconds"),
            )
        except sqlite3.Error:
            logger.exception("get_last_bot_message failed")
            return None
        if not rows:
            return None
        return {"message_id": rows[0][0], "text": rows[0][1], "created_at": rows[0][2]}

    def mark_bot_message_recalled(self, message_id: int) -> None:
        self.execute_action(
            "UPDATE bot_messages SET recalled = 1 WHERE message_id = ?", (message_id,)
        )

    # ── Group activity analysis ──────────────────────────

    def get_daily_group_stats(self, group_id: str, day: str | None = None) -> dict[str, Any]:
        """One day of activity for a group: totals, top speakers, hourly spread.

        `day` is YYYY-MM-DD; defaults to today. Note `timestamp` is the time the
        event was stored (localtime), which is what "today" means to the group.
        """
        day = day or datetime.now().strftime("%Y-%m-%d")

        def scalar(sql: str, params: tuple = ()) -> int:
            try:
                return self.fetch_data(sql, params)[0][0] or 0
            except (sqlite3.Error, IndexError, TypeError):
                logger.exception("daily stats query failed")
                return 0

        base = "FROM group_messages WHERE group_id = ? AND date(timestamp) = ?"
        args = (group_id, day)

        try:
            top_rows = self.fetch_data(
                f"SELECT user_name, COUNT(*) c, user_id {base} GROUP BY user_id "
                "ORDER BY c DESC LIMIT 10", args,
            )
        except sqlite3.Error:
            logger.exception("daily stats top-speaker query failed")
            top_rows = []

        hourly = [0] * 24
        try:
            for hour, count in self.fetch_data(
                f"SELECT CAST(strftime('%H', timestamp) AS INTEGER), COUNT(*) {base} "
                "GROUP BY 1", args,
            ):
                if isinstance(hour, int) and 0 <= hour < 24:
                    hourly[hour] = count
        except sqlite3.Error:
            logger.exception("daily stats hourly query failed")

        return {
            "date": day,
            "total": scalar(f"SELECT COUNT(*) {base}", args),
            "active_users": scalar(f"SELECT COUNT(DISTINCT user_id) {base}", args),
            "images": scalar(f"SELECT COUNT(*) {base} AND content = '[图片消息]'", args),
            "top": [{"user_name": r[0], "count": r[1], "user_id": r[2]} for r in top_rows],
            "hourly": hourly,
        }

    # Label used for the bot's own lines when merging transcripts. The bot's
    # outgoing messages live in `bot_messages` (they are not echoed back by
    # LLBot unless reportSelfMessage is on), so both tables are unioned.
    BOT_DISPLAY_NAME = "Kiriko"

    @staticmethod
    def _clamp_int(value: Any, default: int, lo: int, hi: int) -> int:
        """Coerce a caller-supplied number into range.

        `value or default` would be wrong here: 0 is a legitimate (if useless)
        input, and the model may also hand us a string.
        """
        try:
            n = int(value)
        except (TypeError, ValueError):
            n = default
        return max(lo, min(n, hi))

    def get_recent_group_context(
        self, group_id: str, minutes: int = 30, limit: int = 40,
        exclude_user: str | None = None,
    ) -> list[dict[str, Any]]:
        """Recent group transcript, oldest-first, for the AI's context tool.

        Includes the bot's own lines so the model can see what it already said
        and who was answering whom.
        """
        minutes = self._clamp_int(minutes, 30, 1, 24 * 60)
        limit = self._clamp_int(limit, 40, 1, 200)
        since = f"-{minutes} minutes"

        member_sql = (
            "SELECT user_name, content, timestamp, 0 AS is_bot, "
            f"       {self._MEMBER_SORT} AS sort_key "
            "FROM group_messages "
            "WHERE group_id = ? AND timestamp >= datetime('now','localtime',?)"
        )
        params: list[Any] = [group_id, since]
        if exclude_user:
            member_sql += " AND user_id != ?"
            params.append(exclude_user)

        # Placeholders are positional, in the order they appear in the SQL.
        params.append(self.BOT_DISPLAY_NAME)
        params.extend([group_id, since])

        sql = (
            f"{member_sql} UNION ALL "
            "SELECT ?, text, created_at, 1 AS is_bot, "
            f"       {self._BOT_SORT} AS sort_key "
            "FROM bot_messages "
            "WHERE group_id = ? AND recalled = 0 "
            "AND created_at >= datetime('now','localtime',?) "
            "ORDER BY sort_key DESC LIMIT ?"
        )
        params.append(limit)

        try:
            rows = self.fetch_data(sql, tuple(params))
        except sqlite3.Error:
            logger.exception("recent context query failed")
            return []
        return [
            {"user_name": r[0], "content": r[1], "timestamp": r[2], "is_bot": bool(r[3])}
            for r in reversed(rows)
        ]

    def get_group_message_page(
        self, group_id: str, day: str | None = None, keyword: str = "",
        user_name: str = "", page: int = 1, size: int = 100,
    ) -> dict[str, Any]:
        """Paginated group transcript for the dashboard review page.

        Members' messages and the bot's own lines are unioned so the review
        shows the conversation as it actually happened.
        """
        page = self._clamp_int(page, 1, 1, 10_000)
        size = self._clamp_int(size, 100, 1, 500)

        member_where = ["group_id = ?"]
        bot_where = ["group_id = ?"]
        member_params: list[Any] = [group_id]
        bot_params: list[Any] = [group_id]

        if day:
            member_where.append("date(timestamp) = ?")
            member_params.append(day)
            bot_where.append("date(created_at) = ?")
            bot_params.append(day)
        if keyword:
            member_where.append("content LIKE ?")
            member_params.append(f"%{keyword}%")
            bot_where.append("text LIKE ?")
            bot_params.append(f"%{keyword}%")
        if user_name:
            member_where.append("user_name = ?")
            member_params.append(user_name)
            # Bot rows carry no real sender, so they only match the bot label.
            bot_where.append("? = ?")
            bot_params.extend([user_name, self.BOT_DISPLAY_NAME])

        # sort_key keeps messages that share a timestamp in insertion order —
        # a plain ORDER BY timestamp is unstable for back-to-back messages.
        union = (
            "SELECT user_name, content, timestamp, message_seq, reply_to_seq, 0 AS is_bot, "
            f"       {self._MEMBER_SORT} AS sort_key "
            f"FROM group_messages WHERE {' AND '.join(member_where)} "
            "UNION ALL "
            "SELECT ?, text, created_at, NULL, NULL, 1 AS is_bot, "
            f"       {self._BOT_SORT} AS sort_key "
            f"FROM bot_messages WHERE {' AND '.join(bot_where)}"
        )
        # member placeholders come first, then the bot SELECT's label, then bot's own
        params = member_params + [self.BOT_DISPLAY_NAME] + bot_params

        try:
            total = self.fetch_data(
                f"SELECT COUNT(*) FROM ({union})", tuple(params)
            )[0][0]
            rows = self.fetch_data(
                "SELECT user_name, content, timestamp, message_seq, reply_to_seq, is_bot "
                f"FROM ({union}) ORDER BY sort_key DESC LIMIT ? OFFSET ?",
                tuple(params) + (size, (page - 1) * size),
            )
        except (sqlite3.Error, IndexError, TypeError):
            logger.exception("group message page query failed")
            return {"items": [], "total": 0, "page": page, "pages": 0}

        items = [
            {"user_name": r[0], "content": r[1], "timestamp": r[2],
             "message_seq": r[3], "reply_to_seq": r[4], "is_bot": bool(r[5])}
            for r in reversed(rows)   # oldest-first within the page
        ]
        return {
            "items": items,
            "total": total,
            "page": page,
            "pages": max(1, (total + size - 1) // size),
        }

    def get_group_days(self, group_id: str, limit: int = 60) -> list[dict[str, Any]]:
        """Days that have messages, newest first (for the review page picker)."""
        try:
            rows = self.fetch_data(
                "SELECT date(timestamp) d, COUNT(*) FROM group_messages "
                "WHERE group_id = ? GROUP BY d ORDER BY d DESC LIMIT ?",
                (group_id, limit),
            )
        except sqlite3.Error:
            return []
        return [{"date": r[0], "count": r[1]} for r in rows if r[0]]

    # ── Group push subscriptions ─────────────────────────

    SUBSCRIPTION_TOPICS = ("morning_news", "gaming_news", "hitokoto", "daily_roll_call")

    def get_subscriptions(self, group_id: str | None = None) -> list[dict[str, Any]]:
        """Per-group push subscriptions, optionally for one group."""
        try:
            if group_id:
                rows = self.fetch_data(
                    "SELECT group_id, topic, push_time, enabled, last_fired_date "
                    "FROM group_subscriptions WHERE group_id = ? ORDER BY topic",
                    (group_id,),
                )
            else:
                rows = self.fetch_data(
                    "SELECT group_id, topic, push_time, enabled, last_fired_date "
                    "FROM group_subscriptions ORDER BY group_id, topic"
                )
        except sqlite3.Error:
            logger.exception("subscription query failed")
            return []
        return [
            {"group_id": r[0], "topic": r[1], "push_time": r[2],
             "enabled": bool(r[3]), "last_fired_date": r[4]}
            for r in rows
        ]

    def set_subscription(self, group_id: str, topic: str,
                         push_time: str | None = None,
                         enabled: bool | None = None) -> None:
        """Create or update one subscription (unspecified fields are kept)."""
        if topic not in self.SUBSCRIPTION_TOPICS:
            raise ValueError(f"unknown topic: {topic}")
        self.execute_action(
            "INSERT INTO group_subscriptions (group_id, topic, push_time, enabled) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(group_id, topic) DO UPDATE SET "
            "push_time = COALESCE(?, push_time), "
            "enabled   = COALESCE(?, enabled)",
            (group_id, topic, push_time or "07:00", 1 if (enabled is None or enabled) else 0,
             push_time, None if enabled is None else (1 if enabled else 0)),
        )

    def delete_subscription(self, group_id: str, topic: str) -> None:
        self.execute_action(
            "DELETE FROM group_subscriptions WHERE group_id = ? AND topic = ?",
            (group_id, topic),
        )

    def due_subscriptions(self, now_hm: str, today: str) -> list[dict[str, Any]]:
        """Enabled subscriptions whose time has passed and haven't fired today."""
        try:
            rows = self.fetch_data(
                "SELECT group_id, topic, push_time FROM group_subscriptions "
                "WHERE enabled = 1 AND push_time <= ? AND last_fired_date != ?",
                (now_hm, today),
            )
        except sqlite3.Error:
            logger.exception("due subscription query failed")
            return []
        return [{"group_id": r[0], "topic": r[1], "push_time": r[2]} for r in rows]

    def mark_subscription_fired(self, group_id: str, topic: str, today: str) -> None:
        self.execute_action(
            "UPDATE group_subscriptions SET last_fired_date = ? "
            "WHERE group_id = ? AND topic = ?",
            (today, group_id, topic),
        )

    def get_profile_history(self, user_id: str, group_id: str,
                            limit: int = 3) -> list[dict[str, Any]]:
        """Earlier profile snapshots, newest first."""
        limit = self._clamp_int(limit, 3, 1, 10)
        try:
            rows = self.fetch_data(
                "SELECT profile_json, message_count, recorded_at FROM profile_history "
                "WHERE user_id = ? AND group_id = ? ORDER BY id DESC LIMIT ?",
                (user_id, group_id, limit),
            )
        except sqlite3.Error:
            return []
        import json as _json
        out = []
        for pj, cnt, ts in rows:
            try:
                parsed = _json.loads(pj)
            except (ValueError, TypeError):
                continue
            out.append({"profile": parsed, "message_count": cnt, "recorded_at": ts})
        return out

    # ── Topic threading ──────────────────────────────────

    def get_group_threads(self, group_id: str, day: str | None = None,
                          max_gap_minutes: int = 10,
                          limit: int = 300) -> list[dict[str, Any]]:
        """Group a transcript into topic threads.

        A group chat runs several conversations at once, so a flat timeline is
        hard to read (and hard for the model to reason about). Messages are
        clustered by: does this reply to something already in the current
        thread, or did the conversation pause long enough to be a new topic.
        """
        max_gap = self._clamp_int(max_gap_minutes, 10, 1, 240)
        limit = self._clamp_int(limit, 300, 10, 1000)

        where = ["group_id = ?"]
        params: list[Any] = [group_id]
        if day:
            where.append("date(timestamp) = ?")
            params.append(day)
        clause = " AND ".join(where)

        try:
            rows = self.fetch_data(
                "SELECT user_name, content, timestamp, message_seq, reply_to_seq, "
                "       IFNULL(ts_exact, (julianday(timestamp) - 2440587.5) * 86400.0) AS sk, "
                "       0 AS is_bot FROM group_messages "
                f"WHERE {clause} ORDER BY sk ASC LIMIT ?",
                tuple(params) + (limit,),
            )
        except sqlite3.Error:
            logger.exception("thread query failed")
            return []

        threads: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        prev_sk: float | None = None

        for user_name, content, ts, seq, reply_seq, sk, is_bot in rows:
            replies_into_current = (
                current is not None and reply_seq is not None
                and str(reply_seq) in current["seqs"]
            )
            gap_too_big = prev_sk is not None and (sk - prev_sk) > max_gap * 60

            if current is None or (gap_too_big and not replies_into_current):
                current = {"index": len(threads) + 1, "start": ts, "end": ts,
                           "messages": [], "seqs": set(), "participants": set()}
                threads.append(current)

            current["messages"].append({
                "user_name": user_name, "content": content, "timestamp": ts,
                "message_seq": seq, "reply_to_seq": reply_seq, "is_bot": bool(is_bot),
            })
            if seq is not None:
                current["seqs"].add(str(seq))
            current["participants"].add(user_name)
            current["end"] = ts
            prev_sk = sk

        for t in threads:
            t["participants"] = sorted(p for p in t["participants"] if p)
            t["seqs"] = len(t["seqs"])
            t["size"] = len(t["messages"])
            t["title"] = _thread_title(t["messages"])
        return threads

    # ── AI usage metrics ─────────────────────────────────

    # Peak hours are 01:00-04:00 and 06:00-10:00 UTC, Mon-Fri; off-peak is half.
    _PEAK_HOURS = (1, 2, 3, 6, 7, 8, 9)

    @classmethod
    def _is_peak(cls, utc_hour: Any, utc_weekday: Any) -> bool:
        try:
            hour, weekday = int(utc_hour), int(utc_weekday)
        except (TypeError, ValueError):
            return False
        return weekday < 5 and hour in cls._PEAK_HOURS

    @classmethod
    def estimate_cost_usd(cls, *, cache_hit: int, cache_miss: int, output: int,
                          utc_hour: Any = None, utc_weekday: Any = None) -> float:
        """Cost estimate in USD from Config's per-1M rates (off-peak halved)."""
        from config import Config

        factor = 1.0 if cls._is_peak(utc_hour, utc_weekday) else 0.5
        return (
            cache_hit / 1_000_000 * Config.AI_PRICE_CACHE_HIT
            + cache_miss / 1_000_000 * Config.AI_PRICE_CACHE_MISS
            + output / 1_000_000 * Config.AI_PRICE_OUTPUT
        ) * factor

    def record_ai_call(self, entry: dict[str, Any]) -> None:
        """Append one instrumented API call (see ai_metrics.record)."""
        cols = ("source", "kind", "model", "group_id", "prompt_tokens",
                "completion_tokens", "reasoning_tokens", "cache_hit_tokens",
                "cache_miss_tokens", "latency_ms", "success", "error",
                "utc_hour", "utc_weekday")
        self.deposit(
            "ai_calls",
            "(" + ", ".join(cols) + ")",
            "(" + ", ".join("?" * len(cols)) + ")",
            tuple(entry.get(c) for c in cols),
        )

    def get_ai_metrics(self, hours: int = 24) -> dict[str, Any]:
        """Aggregated AI usage for the dashboard: volume, latency, cost, errors."""
        hours = self._clamp_int(hours, 24, 1, 24 * 30)
        try:
            rows = self.fetch_data(
                "SELECT timestamp, source, kind, model, prompt_tokens, "
                "       completion_tokens, reasoning_tokens, cache_hit_tokens, "
                "       cache_miss_tokens, latency_ms, success, error, "
                "       utc_hour, utc_weekday "
                "FROM ai_calls WHERE timestamp >= datetime('now','localtime',?) "
                "ORDER BY id DESC LIMIT 20000",
                (f"-{hours} hours",),
            )
        except sqlite3.Error:
            logger.exception("ai metrics query failed")
            rows = []

        empty = {
            "hours": hours,
            "totals": {"calls": 0, "failed": 0, "success_rate": 100.0,
                       "prompt_tokens": 0, "completion_tokens": 0,
                       "reasoning_tokens": 0, "cache_hit_tokens": 0,
                       "cache_miss_tokens": 0, "tokens": 0, "cost_usd": 0.0},
            "latency": {"avg_ms": 0, "p50_ms": 0, "p95_ms": 0, "max_ms": 0},
            "by_source": [],
            # Always 24 buckets so consumers never special-case the empty shape.
            "hourly": [{"hour": h, "calls": 0, "tokens": 0} for h in range(24)],
            "recent_errors": [],
        }
        if not rows:
            return empty

        totals = {"calls": len(rows), "failed": 0, "prompt_tokens": 0,
                  "completion_tokens": 0, "reasoning_tokens": 0,
                  "cache_hit_tokens": 0, "cache_miss_tokens": 0, "cost_usd": 0.0}
        latencies: list[int] = []
        by_source: dict[str, dict[str, Any]] = {}
        hourly = {h: {"hour": h, "calls": 0, "tokens": 0} for h in range(24)}
        errors: list[dict[str, Any]] = []

        for (ts, source, kind, model, pt, ct, rt, hit, miss, latency,
             success, error, uhour, uwday) in rows:
            pt, ct, rt = int(pt or 0), int(ct or 0), int(rt or 0)
            hit, miss = int(hit or 0), int(miss or 0)
            latency = int(latency or 0)
            tokens = pt + ct
            cost = self.estimate_cost_usd(cache_hit=hit, cache_miss=miss,
                                          output=ct, utc_hour=uhour, utc_weekday=uwday)

            totals["prompt_tokens"] += pt
            totals["completion_tokens"] += ct
            totals["reasoning_tokens"] += rt
            totals["cache_hit_tokens"] += hit
            totals["cache_miss_tokens"] += miss
            totals["cost_usd"] += cost
            if not success:
                totals["failed"] += 1
                if len(errors) < 10:
                    errors.append({"timestamp": ts, "source": source or "?",
                                   "error": (error or "")[:160]})
            if success:
                latencies.append(latency)

            bucket = by_source.setdefault(source or "unknown", {
                "source": source or "unknown", "calls": 0, "failed": 0,
                "tokens": 0, "cost_usd": 0.0, "latency_ms": 0,
            })
            bucket["calls"] += 1
            bucket["failed"] += 0 if success else 1
            bucket["tokens"] += tokens
            bucket["cost_usd"] += cost
            bucket["latency_ms"] += latency

            hour = int(str(ts)[11:13]) if ts else 0
            if 0 <= hour < 24:
                hourly[hour]["calls"] += 1
                hourly[hour]["tokens"] += tokens

        latencies.sort()
        def pct(p: float) -> int:
            if not latencies:
                return 0
            idx = min(len(latencies) - 1, int(round((len(latencies) - 1) * p)))
            return latencies[idx]

        for bucket in by_source.values():
            bucket["avg_ms"] = round(bucket["latency_ms"] / bucket["calls"]) if bucket["calls"] else 0
            bucket["cost_usd"] = round(bucket["cost_usd"], 4)
            bucket.pop("latency_ms", None)

        totals["tokens"] = totals["prompt_tokens"] + totals["completion_tokens"]
        totals["cost_usd"] = round(totals["cost_usd"], 4)
        totals["success_rate"] = round(
            (totals["calls"] - totals["failed"]) / totals["calls"] * 100, 1
        ) if totals["calls"] else 100.0

        return {
            "hours": hours,
            "totals": totals,
            "latency": {
                "avg_ms": round(sum(latencies) / len(latencies)) if latencies else 0,
                "p50_ms": pct(0.50),
                "p95_ms": pct(0.95),
                "max_ms": latencies[-1] if latencies else 0,
            },
            "by_source": sorted(by_source.values(), key=lambda b: b["calls"], reverse=True),
            "hourly": [hourly[h] for h in range(24)],
            "recent_errors": errors,
        }

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
        # Long-term memory: keep what we believed before, so the bot can say
        # "you mentioned X before" instead of only knowing the latest snapshot.
        try:
            previous = self.fetch_data(
                "SELECT profile_json FROM user_profiles WHERE user_id = ? AND group_id = ?",
                (user_id, group_id),
            )
            if previous and previous[0][0] and previous[0][0] != profile_json:
                self.execute_action(
                    "INSERT INTO profile_history (user_id, group_id, profile_json, message_count) "
                    "VALUES (?, ?, ?, ?)",
                    (user_id, group_id, previous[0][0], message_count),
                )
        except sqlite3.Error:
            logger.debug("profile history snapshot failed", exc_info=True)

        self.execute_action(
            "INSERT INTO user_profiles (user_id, group_id, user_name, profile_json, message_count, last_updated) "
            "VALUES (?, ?, ?, ?, ?, datetime('now', 'localtime')) "
            "ON CONFLICT(user_id, group_id) DO UPDATE SET "
            "user_name=excluded.user_name, profile_json=excluded.profile_json, "
            "message_count=excluded.message_count, last_updated=datetime('now', 'localtime')",
            (user_id, group_id, effective_name, profile_json, message_count),
        )

    def get_user_profile(self, user_id: str, group_id: str | None = None) -> dict[str, Any] | None:
        """Profile for a user — scoped to a group when one is given.

        Profiles are per (user, group); omitting group_id falls back to the
        most recently updated one for backwards compatibility.
        """
        cols = "SELECT profile_json, user_name, message_count, last_updated, group_id FROM user_profiles WHERE user_id = ?"
        if group_id:
            rows = self.fetch_data(cols + " AND group_id = ?", (user_id, group_id))
        else:
            rows = self.fetch_data(cols + " ORDER BY last_updated DESC LIMIT 1", (user_id,))
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

    def record_tool_usage(self, tool_name: str, user_id: str, group_id: str | None,
                          arguments: str = "", result: str = "",
                          reasoning: str = "") -> None:
        self.deposit(
            "tool_usage",
            "(tool_name, user_id, group_id, arguments, result, reasoning)",
            "(?, ?, ?, ?, ?, ?)",
            (tool_name, user_id, group_id, arguments[:500], result[:500],
             reasoning[:2000]),
        )

    def get_recent_tool_chain(self, group_id: str | None, user_id: str,
                              limit: int = 5) -> list[dict[str, Any]]:
        """The most recent tool invocations for a user, oldest-first.

        Backs the "what did you just do / what were you thinking" tool.
        """
        limit = self._clamp_int(limit, 5, 1, 20)
        try:
            rows = self.fetch_data(
                "SELECT tool_name, arguments, result, reasoning, timestamp "
                "FROM tool_usage WHERE user_id = ? AND IFNULL(group_id,'') = ? "
                "ORDER BY id DESC LIMIT ?",
                (user_id, group_id or "", limit),
            )
        except sqlite3.Error:
            logger.exception("tool chain query failed")
            return []
        return [
            {"tool_name": r[0], "arguments": r[1], "result": r[2],
             "reasoning": r[3], "timestamp": r[4]}
            for r in reversed(rows)
        ]

    def get_feature_requests(self, status: str = "", limit: int = 20) -> list[dict[str, Any]]:
        """Feature requests, newest first (optionally filtered by status)."""
        limit = self._clamp_int(limit, 20, 1, 100)
        sql = ("SELECT id, summary_or_request, category, priority, status, timestamp "
               "FROM (SELECT id, COALESCE(NULLIF(ai_summary,''), request_text) AS summary_or_request, "
               "             category, priority, status, timestamp FROM feature_requests)")
        params: tuple[Any, ...] = ()
        if status:
            sql += " WHERE status = ?"
            params = (status,)
        sql += " ORDER BY id DESC LIMIT ?"
        params += (limit,)
        try:
            rows = self.fetch_data(sql, params)
        except sqlite3.Error:
            logger.exception("feature request query failed")
            return []
        return [
            {"id": r[0], "summary": r[1], "category": r[2],
             "priority": r[3], "status": r[4], "timestamp": r[5]}
            for r in rows
        ]

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

    # ── Group purge ─────────────────────────────────────
    # Tables with a real group_id column: everything here is scoped to one
    # group and can be deleted outright.
    _GROUP_SCOPED: tuple[tuple[str, str], ...] = (
        ("group_messages", "群消息"),
        ("history", "对话记录"),
        ("reminders", "提醒"),
        ("tool_usage", "工具调用"),
        ("user_profiles", "用户画像"),
        ("user_affection", "好感度"),
        ("user_affection_log", "好感度流水"),
        ("feature_requests", "功能需求"),
    )

    def _group_user_ids(self, group_id: str) -> list[str]:
        """Distinct users that ever spoke in this group."""
        try:
            return [r[0] for r in self.fetch_data(
                "SELECT DISTINCT user_id FROM group_messages WHERE group_id = ?",
                (str(group_id),),
            )]
        except sqlite3.Error:
            return []

    def group_purge_preview(self, group_id: str) -> dict[str, int]:
        """Row counts purge_group() would delete, for the confirm dialog.

        `users` / `learning_log` / `user_settings` are per-user artifacts that
        are NOT group-scoped in the schema (a user can be in several groups),
        so they are reported separately and clearly.
        """
        gid = str(group_id)
        counts: dict[str, int] = {}
        for table, label in self._GROUP_SCOPED:
            try:
                counts[table] = self.fetch_data(
                    f"SELECT COUNT(*) FROM {table} WHERE group_id = ?", (gid,)
                )[0][0]
            except (sqlite3.Error, IndexError):
                counts[table] = 0
        try:
            counts["group_settings"] = self.fetch_data(
                "SELECT COUNT(*) FROM feature_settings WHERE scope_type='group' AND scope_id = ?",
                (gid,),
            )[0][0]
        except (sqlite3.Error, IndexError):
            counts["group_settings"] = 0

        users = self._group_user_ids(gid)
        counts["users"] = len(users)
        counts["learning_log"] = 0
        counts["user_settings"] = 0
        if users:
            marks = ",".join("?" * len(users))
            try:
                counts["learning_log"] = self.fetch_data(
                    f"SELECT COUNT(*) FROM learning_log WHERE user_id IN ({marks})",
                    tuple(users),
                )[0][0]
            except (sqlite3.Error, IndexError):
                pass
            try:
                counts["user_settings"] = self.fetch_data(
                    f"SELECT COUNT(*) FROM feature_settings "
                    f"WHERE scope_type='user' AND scope_id IN ({marks})",
                    tuple(users),
                )[0][0]
            except (sqlite3.Error, IndexError):
                pass
        return counts

    def purge_group(self, group_id: str) -> dict[str, int]:
        """Delete everything belonging to a group.

        Covers all group-scoped tables plus the per-user artifacts of members
        seen in this group (profiles/affection are group-scoped already;
        learning notes and per-user toggles are not). Sticker files are a
        global gallery and are intentionally left alone.
        """
        gid = str(group_id)
        deleted: dict[str, int] = {}
        users = self._group_user_ids(gid)

        for table, label in self._GROUP_SCOPED:
            try:
                n = self.fetch_data(
                    f"SELECT COUNT(*) FROM {table} WHERE group_id = ?", (gid,)
                )[0][0]
                self.execute_action(f"DELETE FROM {table} WHERE group_id = ?", (gid,))
                deleted[table] = n
            except (sqlite3.Error, IndexError):
                logger.exception("purge_group: failed to clear %s", table)

        try:
            self.execute_action(
                "DELETE FROM feature_settings WHERE scope_type='group' AND scope_id = ?",
                (gid,),
            )
        except sqlite3.Error:
            logger.exception("purge_group: failed to clear group feature settings")

        if users:
            marks = ",".join("?" * len(users))
            for sql, params, key in (
                (f"DELETE FROM learning_log WHERE user_id IN ({marks})", tuple(users), "learning_log"),
                (f"DELETE FROM feature_settings WHERE scope_type='user' AND scope_id IN ({marks})",
                 tuple(users), "user_settings"),
            ):
                try:
                    self.execute_action(sql, params)
                    deleted[key] = len(users)
                except sqlite3.Error:
                    logger.exception("purge_group: failed to clear %s", key)

        self._member_cache.pop(gid, None)
        logger.info("Purged group %s: %s", gid, deleted)
        return deleted
