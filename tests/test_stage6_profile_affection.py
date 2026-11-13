"""阶段 6：好感度并入画像（一人一个 global，群/私聊是分量）。"""
from __future__ import annotations

import json
import sqlite3

import pytest

from affection_service import SCORE_DEFAULT, AffectionService


class _PrivateRobot:
    msg_type = "private"
    group_id = None
    group_name = None
    user_id = "u1"
    user_name = "小明"
    msg = "谢谢你"
    incoming = type("I", (), {"has_images": False})()


class TestPrivateAffection:
    def test_private_judge_writes_a_private_component(self, db, monkeypatch):
        import judge_service as js

        monkeypatch.setattr(
            js, "quick_chat",
            lambda *a, **k: '{"sentiment":{"delta":0.3,"reason":"感谢"}}',
        )
        out = js.JudgeService().judge_turn(db, _PrivateRobot(), affection_on=True)

        assert out is not None and out["delta"] == pytest.approx(0.3)
        rows = db.fetch_data(
            "SELECT group_id, scope, affection_score FROM user_affection WHERE user_id='u1'",
        )
        assert rows and rows[0][0] == "" and rows[0][1] == "private"
        assert rows[0][2] > SCORE_DEFAULT

    def test_private_judge_prompt_has_no_group_name(self):
        import judge_service as js

        prompt = js.JudgeService()._build_prompt(_PrivateRobot(), None)
        assert "私聊中用户 小明" in prompt
        assert "群「" not in prompt

    def test_private_speaker_context_is_not_empty(self, db, monkeypatch):
        import main

        db.save_user_profile(
            "u1", "小明",
            json.dumps({"version": 2, "stable": {"traits": ["爱聊技术"]}, "recent": {}}),
            30, "",
        )
        monkeypatch.setattr(main, "db", db)

        block = main._speaker_context(_PrivateRobot(), set())
        assert "【你和他的关系】" in block
        assert "【正在跟你说话的人】" in block


class TestAffectionApiUsesGlobal:
    @pytest.fixture
    def client(self):
        import base64

        import dashboard_auth
        import main
        from config import Config

        main.app.config["TESTING"] = True
        client = main.app.test_client()
        token = base64.b64encode(
            f"{Config.DASHBOARD_USER}:{dashboard_auth._load_password()}".encode()
        ).decode()
        client.environ_base["HTTP_AUTHORIZATION"] = f"Basic {token}"
        return client

    def _two_groups(self, db):
        for group_id, score, count in (("g1", 80.0, 10), ("g2", 40.0, 30)):
            db.execute_action(
                "INSERT INTO user_affection (user_id, group_id, scope, user_name, "
                "affection_score, interaction_count) VALUES (?,?,?,?,?,?)",
                ("u1", group_id, "group", "小明", score, count),
            )

    def test_default_list_returns_one_row_per_user(self, db, client, monkeypatch):
        import main

        self._two_groups(db)
        monkeypatch.setattr(main, "db", db)
        body = client.get("/api/affection").get_json()

        assert body["total"] == 1
        record = body["affection_records"][0]
        assert record["user_id"] == "u1"
        assert record["affection_score"] == 50.0        # (80*10 + 40*30) / 40
        assert len(record["components"]) == 2

    def test_user_detail_without_group_returns_global(self, db, client, monkeypatch):
        import main

        self._two_groups(db)
        monkeypatch.setattr(main, "db", db)
        resp = client.get("/api/affection/u1")

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["global_score"] == 50.0
        assert len(body["components"]) == 2

    def test_user_detail_with_group_returns_component_and_global(self, db, client, monkeypatch):
        import main

        self._two_groups(db)
        monkeypatch.setattr(main, "db", db)
        body = client.get("/api/affection/u1?group_id=g1").get_json()

        assert body["affection_score"] == 80.0
        assert body["global_score"] == 50.0

    def test_leaderboard_defaults_to_global(self, db, client, monkeypatch):
        import main

        self._two_groups(db)
        monkeypatch.setattr(main, "db", db)
        body = client.get("/api/affection/leaderboard").get_json()

        assert body["leaderboard"][0]["user_id"] == "u1"
        assert body["leaderboard"][0]["affection_score"] == 50.0


class TestScopeMigration:
    def test_legacy_table_gains_scope_without_touching_scores(self, tmp_path):
        from database_manager import DatabaseManager

        path = str(tmp_path / "legacy.db")
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE user_affection("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, "
            "group_id TEXT NOT NULL, user_name TEXT NOT NULL, "
            "affection_score REAL DEFAULT 50.0, interaction_count INTEGER DEFAULT 0, "
            "positive_count INTEGER DEFAULT 0, negative_count INTEGER DEFAULT 0, "
            "last_interaction DATETIME, relationship TEXT DEFAULT 'neutral', "
            "notes TEXT DEFAULT '', created_at DATETIME, updated_at DATETIME, "
            "UNIQUE(user_id, group_id))"
        )
        conn.execute(
            "INSERT INTO user_affection (user_id, group_id, user_name, affection_score) "
            "VALUES ('u1', 'g1', '小明', 77.0)",
        )
        conn.commit()
        conn.close()

        DatabaseManager(path)
        db = DatabaseManager(path)          # 第二次启动：幂等

        rows = db.fetch_data(
            "SELECT affection_score, scope FROM user_affection WHERE user_id='u1'",
        )
        assert rows == [(77.0, "group")]
        log_cols = [r[1] for r in db.fetch_data("PRAGMA table_info(user_affection_log)")]
        assert "scope" in log_cols


class TestNotesSurviveGlobal:
    def test_relationship_block_keeps_the_operator_note(self, db):
        AffectionService.get_or_create(db, "u1", "g1", "小明")
        AffectionService.manual_adjust(db, "u1", "g1", 40.0, note="别聊他前女友")
        block = AffectionService.relationship_block(db, "u1", "g1")
        assert "备注：别聊他前女友" in block
