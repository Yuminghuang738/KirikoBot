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


def _make_per_group_profile_db(path: str, rows: list[tuple]) -> None:
    """线上实际形态：user_profiles 是 (user_id, group_id) 复合键。

    同一个人在每个群各有一份互不相干的画像，私聊还单独一份 —— 正是要收敛掉的
    那个状态。
    """
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE user_profiles(
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      TEXT NOT NULL,
            group_id     TEXT NOT NULL,
            user_name    TEXT NOT NULL,
            profile_json TEXT NOT NULL DEFAULT '{}',
            message_count INTEGER DEFAULT 0,
            last_updated DATETIME DEFAULT (datetime('now', 'localtime')),
            UNIQUE(user_id, group_id))"""
    )
    conn.execute(
        """CREATE TABLE profile_history(
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id       TEXT,
            group_id      TEXT,
            profile_json  TEXT,
            message_count INTEGER,
            recorded_at   DATETIME DEFAULT (datetime('now', 'localtime')))"""
    )
    conn.executemany(
        "INSERT INTO user_profiles (user_id,group_id,user_name,profile_json,message_count) "
        "VALUES (?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()


class TestUserProfileMigration:
    """画像从「每个群一份」收敛成「每个用户一份」。

    中间有过一次反向改动：最早是 UNIQUE(user_id)，同一个人在多个群会互相覆盖，
    于是改成了 (user_id, group_id) 复合键。那修好了「画像丢失」，却让同一个人
    变成好几份互不相干的画像。画像的初衷是全方位了解一个人，所以现在是单份。
    """

    def test_migrates_legacy_single_group_schema(self, tmp_path):
        """最早的 schema：UNIQUE(user_id) + 一个 group_id 列。"""
        from database_manager import DatabaseManager

        path = str(tmp_path / "legacy.db")
        make_legacy_profile_db(path)
        assert "group_id" in _columns(path, "user_profiles")

        DatabaseManager(path)  # triggers the migration

        assert "group_id" not in _columns(path, "user_profiles")
        conn = sqlite3.connect(path)
        try:
            rows = conn.execute(
                "SELECT user_id, user_name, profile_json, message_count "
                "FROM user_profiles"
            ).fetchall()
        finally:
            conn.close()
        assert rows == [("u1", "小明", '{"personality":"开朗"}', 30)]

    def test_merges_the_same_person_across_groups_and_private(self, tmp_path):
        """同一个人在多群 + 私聊的画像合并成一份。

        合并规则：取**消息数最多**的那份（背后样本最多、描述最有依据），
        消息数按所有群求和。
        """
        from database_manager import DatabaseManager

        path = str(tmp_path / "pergroup.db")
        _make_per_group_profile_db(path, [
            ("u1", "g1", "小明", '{"p":"A"}', 10),
            ("u1", "g2", "小明", '{"p":"B"}', 80),   # 样本最多，应当胜出
            ("u1", "",   "小明", '{"p":"C"}', 5),    # 私聊
            ("u2", "g1", "小红", '{"p":"D"}', 20),
        ])

        DatabaseManager(path)

        conn = sqlite3.connect(path)
        try:
            rows = dict(
                (r[0], r[1:]) for r in conn.execute(
                    "SELECT user_id, profile_json, message_count FROM user_profiles")
            )
        finally:
            conn.close()

        assert len(rows) == 2, "每个用户只该剩一行"
        assert rows["u1"][0] == '{"p":"B"}', "应当采用消息数最多的那份"
        assert rows["u1"][1] == 95, "消息数应当按所有群求和 (10+80+5)"
        assert rows["u2"][0] == '{"p":"D"}'

    def test_losing_profiles_are_kept_as_history(self, tmp_path):
        """落选的那几份进 profile_history 留档，不是直接丢掉。"""
        from database_manager import DatabaseManager

        path = str(tmp_path / "pergroup.db")
        _make_per_group_profile_db(path, [
            ("u1", "g1", "小明", '{"p":"A"}', 10),
            ("u1", "g2", "小明", '{"p":"B"}', 80),
        ])
        DatabaseManager(path)

        conn = sqlite3.connect(path)
        try:
            kept = [r[0] for r in conn.execute(
                "SELECT profile_json FROM profile_history WHERE user_id='u1'")]
        finally:
            conn.close()
        assert kept == ['{"p":"A"}']

    def test_migration_is_idempotent(self, db_file):
        from database_manager import DatabaseManager

        DatabaseManager(db_file)
        DatabaseManager(db_file)
        assert "group_id" not in _columns(db_file, "user_profiles")

    def test_one_person_one_profile_regardless_of_group(self, db):
        """同一个人换个地方说话，画像仍然只有一份、且被更新。"""
        db.save_user_profile("u1", "小明", '{"personality":"开朗"}', 30, "g1")
        assert db.get_user_profile("u1")["profile"]["personality"] == "开朗"

        # 在另一个群说了更多话 → 还是同一条记录被更新
        db.save_user_profile("u1", "小明", '{"personality":"安静"}', 55, "g2")
        assert db.get_user_profile("u1")["profile"]["personality"] == "安静"
        assert db.fetch_data("SELECT COUNT(*) FROM user_profiles")[0][0] == 1

    def test_group_view_shows_members_with_their_global_profile(self, db):
        """群视图列出的是**在本群露面的人**，内容是他们的全局画像。

        换句话说：知道谁在这个屋子，也知道每个人的完整样子。
        """
        for gid in ("g1", "g2"):
            db.execute_action(
                "INSERT INTO group_messages (group_id,user_id,user_name,content) VALUES (?,?,?,?)",
                (gid, "u1", "小明", "你好"),
            )
        db.save_user_profile("u1", "小明", '{"personality":"开朗"}', 30)

        assert [p["user_id"] for p in db.get_group_profiles("g1")] == ["u1"]
        assert [p["user_id"] for p in db.get_group_profiles("g2")] == ["u1"]
        # 没露过面的群不该出现
        assert db.get_group_profiles("g3") == []
        # 内容来自全局那一份
        assert db.get_group_profiles("g1")[0]["profile"]["personality"] == "开朗"

    def test_analysis_samples_span_all_groups(self, db):
        """取样必须跨群 —— 只在当前群取样会让画像变成「他在这个群的样子」。"""
        for gid, text in (("g1", "在A群说的话"), ("g2", "在B群说的话"), ("", "私聊说的话")):
            db.execute_action(
                "INSERT INTO group_messages (group_id,user_id,user_name,content) VALUES (?,?,?,?)",
                (gid, "u1", "小明", text),
            )
        texts = [c for c, _ in db.get_user_messages("u1", 50)]
        assert set(texts) == {"在A群说的话", "在B群说的话", "私聊说的话"}
        assert db.count_user_messages("u1") == 3


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
            "INSERT INTO user_profiles (user_id,user_name) VALUES (?,?)",
            (uid, "小明"),
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
        # u1 只在 g1 说过话 → 删完画像就没依据了
        assert preview["user_profiles"] == 1

        db.purge_group("g1")

        assert db.fetch_data("SELECT COUNT(*) FROM group_messages WHERE group_id='g1'")[0][0] == 0
        assert db.fetch_data("SELECT COUNT(*) FROM group_messages WHERE group_id='g2'")[0][0] == 1
        # u2 还在 g2 说话，他的画像必须留着；u1 已无任何发言，画像跟着清掉
        assert db.fetch_data("SELECT COUNT(*) FROM user_profiles WHERE user_id='u2'")[0][0] == 1
        assert db.fetch_data("SELECT COUNT(*) FROM user_profiles WHERE user_id='u1'")[0][0] == 0
        assert db.fetch_data(
            "SELECT COUNT(*) FROM feature_settings WHERE scope_type='group' AND scope_id='g1'"
        )[0][0] == 0

    def test_a_member_of_several_groups_keeps_their_profile(self, db):
        """跨群用户是这次改动的核心：删掉一个群，不该抹掉他在别处的印象。

        画像现在是「人」级别的，删群只该清掉那些**只在这个群说过话**的人的画像。
        """
        self._seed(db, "g1", "u1")
        # 同一个人还在 g2 和私聊里说过话
        for gid in ("g2", ""):
            db.execute_action(
                "INSERT INTO group_messages (group_id,user_id,user_name,content) VALUES (?,?,?,?)",
                (gid, "u1", "小明", "还在别处说话"),
            )

        assert db.group_purge_preview("g1")["user_profiles"] == 0, "他在别处还有发言"

        db.purge_group("g1")

        assert db.fetch_data("SELECT COUNT(*) FROM group_messages WHERE group_id='g1'")[0][0] == 0
        assert db.fetch_data("SELECT COUNT(*) FROM user_profiles WHERE user_id='u1'")[0][0] == 1, \
            "跨群用户的画像不能被一个群的删除带走"

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
