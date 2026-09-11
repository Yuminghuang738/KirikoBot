"""Schema migration, per-group profiles, group purge and retention."""
from __future__ import annotations

import sqlite3

from conftest import make_legacy_profile_db


def _columns(path: str, table: str) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    finally:
        conn.close()


def _table_sql(path: str, table: str) -> str:
    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        return row[0] or "" if row else ""
    finally:
        conn.close()


class TestUserProfileMigration:
    """user_profiles used to be UNIQUE(user_id) with a single group_id."""

    def test_migrates_legacy_schema(self, tmp_path):
        from database_manager import DatabaseManager

        path = str(tmp_path / "legacy.db")
        make_legacy_profile_db(path)
        assert "UNIQUE" in _table_sql(path, "user_profiles")

        DatabaseManager(path)  # triggers the migration

        assert "(user_id, group_id)" in _table_sql(path, "user_profiles")
        conn = sqlite3.connect(path)
        try:
            rows = conn.execute(
                "SELECT user_id, group_id, user_name, profile_json, message_count "
                "FROM user_profiles"
            ).fetchall()
        finally:
            conn.close()
        assert rows == [("u1", "g1", "小明", '{"personality":"开朗"}', 30)]

    def test_migration_is_idempotent(self, db_file):
        from database_manager import DatabaseManager

        DatabaseManager(db_file)
        DatabaseManager(db_file)
        assert "(user_id, group_id)" in _table_sql(db_file, "user_profiles")

    def test_same_user_can_have_a_profile_per_group(self, db):
        db.save_user_profile("u1", "g1", "小明", '{"personality":"开朗"}', 30)
        db.save_user_profile("u1", "g2", "小明", '{"personality":"安静"}', 12)

        assert db.get_user_profile("u1", "g1")["profile"]["personality"] == "开朗"
        assert db.get_user_profile("u1", "g2")["profile"]["personality"] == "安静"

        assert [p["user_id"] for p in db.get_group_profiles("g1")] == ["u1"]
        assert [p["user_id"] for p in db.get_group_profiles("g2")] == ["u1"]
        assert db.get_group_profiles("g3") == []

    def test_reprofile_updates_in_place(self, db):
        db.save_user_profile("u1", "g1", "小明", '{"personality":"开朗"}', 30)
        db.save_user_profile("u1", "g1", "小明", '{"personality":"内向"}', 55)

        rows = db.fetch_data("SELECT COUNT(*) FROM user_profiles")
        assert rows[0][0] == 1
        assert db.get_user_profile("u1", "g1")["profile"]["personality"] == "内向"


class TestPurgeGroup:
    def _seed(self, db, gid="g1", uid="u1"):
        db.execute_action(
            "INSERT INTO group_messages (group_id,user_id,user_name,content) VALUES (?,?,?,?)",
            (gid, uid, "小明", "你好"),
        )
        db.execute_action(
            "INSERT INTO history (user_id,group_id,role,content) VALUES (?,?,?,?)",
            (uid, gid, "user", "你好"),
        )
        db.execute_action(
            "INSERT INTO user_profiles (user_id,group_id,user_name) VALUES (?,?,?)",
            (uid, gid, "小明"),
        )
        db.execute_action(
            "INSERT INTO user_affection (user_id,group_id,user_name) VALUES (?,?,?)",
            (uid, gid, "小明"),
        )
        db.execute_action(
            "INSERT INTO learning_log (user_id,note) VALUES (?,?)", (uid, "教训")
        )
        db.execute_action(
            "INSERT INTO feature_settings (scope_type,scope_id) VALUES ('group',?)", (gid,)
        )

    def test_purges_only_the_target_group(self, db):
        self._seed(db, "g1", "u1")
        self._seed(db, "g2", "u2")

        preview = db.group_purge_preview("g1")
        assert preview["group_messages"] == 1
        assert preview["user_profiles"] == 1

        db.purge_group("g1")

        assert db.fetch_data("SELECT COUNT(*) FROM group_messages WHERE group_id='g1'")[0][0] == 0
        assert db.fetch_data("SELECT COUNT(*) FROM group_messages WHERE group_id='g2'")[0][0] == 1
        assert db.fetch_data("SELECT COUNT(*) FROM user_profiles WHERE group_id='g2'")[0][0] == 1
        assert db.fetch_data(
            "SELECT COUNT(*) FROM feature_settings WHERE scope_type='group' AND scope_id='g1'"
        )[0][0] == 0

    def test_stickers_are_never_deleted(self, db):
        db.execute_action(
            "INSERT INTO stickers (filename,file_hash,file_size) VALUES ('a.png','h',1)"
        )
        self._seed(db)
        db.purge_group("g1")
        assert db.fetch_data("SELECT COUNT(*) FROM stickers")[0][0] == 1

    def test_preview_is_read_only(self, db):
        self._seed(db)
        db.group_purge_preview("g1")
        assert db.fetch_data("SELECT COUNT(*) FROM group_messages")[0][0] == 1


class TestRetention:
    def test_prunes_old_rows_only(self, db):
        from maintenance_service import prune_old_data

        db.execute_action(
            "INSERT INTO group_messages (group_id,user_id,user_name,content,timestamp) "
            "VALUES ('g1','u1','小明','旧', datetime('now','-400 days'))"
        )
        db.execute_action(
            "INSERT INTO group_messages (group_id,user_id,user_name,content,timestamp) "
            "VALUES ('g1','u1','小明','新', datetime('now','-1 days'))"
        )
        deleted = prune_old_data(db, days=180)

        assert deleted.get("group_messages") == 1
        remaining = db.fetch_data("SELECT content FROM group_messages")
        assert remaining == [("新",)]

    def test_disabled_when_days_zero(self, db):
        from maintenance_service import prune_old_data

        db.execute_action(
            "INSERT INTO group_messages (group_id,user_id,user_name,content,timestamp) "
            "VALUES ('g1','u1','小明','旧', datetime('now','-4000 days'))"
        )
        assert prune_old_data(db, days=0) == {}
        assert db.fetch_data("SELECT COUNT(*) FROM group_messages")[0][0] == 1


class TestBackup:
    def test_backup_creates_a_readable_snapshot(self, db, db_file, tmp_path):
        from maintenance_service import backup_database

        db.execute_action(
            "INSERT INTO group_messages (group_id,user_id,user_name,content) VALUES ('g1','u1','小明','hi')"
        )
        dest_dir = str(tmp_path / "backups")
        out = backup_database(db_file, dest_dir, keep=3)
        assert out and out.endswith(".db")

        conn = sqlite3.connect(out)
        try:
            assert conn.execute("SELECT COUNT(*) FROM group_messages").fetchone()[0] == 1
        finally:
            conn.close()

    def test_rotation_keeps_newest(self, db_file, tmp_path):
        from maintenance_service import _rotate_backups, backup_database

        dest_dir = str(tmp_path / "backups")
        for i in range(5):
            backup_database(db_file, dest_dir, keep=2)
        _rotate_backups(dest_dir, keep=2)

        import os
        snaps = [f for f in os.listdir(dest_dir) if f.startswith("robot_")]
        assert len(snaps) == 2
