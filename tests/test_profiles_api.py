"""面板的「用户画像」页读的是 `/api/profiles`。

## 为什么单独测这个接口

2026-10 画像从「7 个平铺字段」改成了两层结构（`stable` / `recent`），但我忘了
同步前端 —— 结果**接口照常 200、页面照常渲染，只是每张卡片上除了名字什么都没有**。
这种坏法不报错、不打日志，只能靠人眼发现。

所以这里钉住三件事：

1. 接口吐的是**新结构**（`stable` / `recent`）
2. 库里如果还剩**老格式**，接口也会 normalize 成新结构（否则前端又是空卡片）
3. 真实存在的画像**至少有一样东西可显示**
"""
from __future__ import annotations

import json

import pytest

from profile_service import normalize_profile


def _v2(traits=None, likes=None, notes="", mood="", doing=""):
    return {
        "version": 2,
        "stable": {
            "traits": traits or [],
            "nickname": "",
            "avoid": [],
            "catchphrase": [],
            "style": {"length": "", "emoji": "", "night": False},
            "likes": likes or [],
            "dislikes": [],
            "notes": notes,
        },
        "recent": {"mood": mood, "doing": doing, "mentioned": []},
    }


class TestNormalizeProfile:
    """老格式 → 新格式的映射（接口和运行时都靠它）。"""

    def test_a_v1_profile_is_mapped_not_lost(self):
        v1 = {
            "personality": "直率急躁，夜猫子",
            "interests": ["音乐", "乐器"],
            "speaking_style": "口语化，简短",
            "topics": ["吉他"],
            "mood": "有点累",
            "note": "大学生",
        }
        out = normalize_profile(v1)
        assert out["stable"]["traits"] == ["直率急躁", "夜猫子"]
        assert out["stable"]["likes"] == ["音乐", "乐器"]
        assert out["stable"]["style"]["length"] == "口语化，简短"
        assert out["stable"]["notes"] == "大学生"
        assert out["recent"]["mood"] == "有点累"

    def test_a_v2_profile_passes_through(self):
        out = normalize_profile(_v2(traits=["直率"], likes=["吉他"]))
        assert out["stable"]["traits"] == ["直率"]
        assert out["stable"]["likes"] == ["吉他"]

    def test_garbage_does_not_raise(self):
        for bad in (None, "字符串", 42, [], {"stable": None, "recent": None}):
            out = normalize_profile(bad)
            assert isinstance(out, dict)
            assert "stable" in out and "recent" in out


class TestProfilesEndpoint:
    def _client(self):
        """面板有 Basic auth，测试里也得带上凭证，否则拿回来的是 401。"""
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

    def test_it_returns_the_two_layer_shape(self, db, monkeypatch):
        import main

        db.save_user_profile("u1", "小明", json.dumps(_v2(traits=["直率"], likes=["吉他"])), 30, "g1")
        monkeypatch.setattr(main, "db", db)
        body = self._client().get("/api/profiles").get_json()

        assert body["profiles"], "一条都没有"
        prof = body["profiles"][0]["profile"]
        assert set(prof) >= {"stable", "recent"}
        assert prof["stable"]["traits"] == ["直率"]

    def test_legacy_rows_are_normalised(self, db, monkeypatch):
        """库里还有老格式时，接口也必须吐新结构。"""
        import main

        legacy = {"personality": "热心直率", "interests": ["吉他"], "note": "老数据"}
        db.save_user_profile("u2", "老王", json.dumps(legacy), 25, "g1")
        monkeypatch.setattr(main, "db", db)
        body = self._client().get("/api/profiles").get_json()

        prof = body["profiles"][0]["profile"]
        assert prof["stable"]["traits"] == ["热心直率"], "老格式没被映射，前端会渲染空卡片"

    def test_it_carries_the_shared_memories(self, db, monkeypatch):
        """共同记忆也一起给面板，方便卡片上显示「你们之间」。"""
        import main

        db.save_user_profile("u3", "小红", json.dumps(_v2(likes=["吉他"])), 30, "g1")
        db.add_profile_memory("u3", "上次答应帮他听一段录音")
        monkeypatch.setattr(main, "db", db)
        body = self._client().get("/api/profiles").get_json()

        assert body["profiles"][0]["memories"] == ["上次答应帮他听一段录音"]

    def test_no_profile_renders_as_an_empty_card(self, db, monkeypatch):
        """这是那次事故的直接表现：卡片有壳没内容。"""
        import main

        db.save_user_profile("u4", "有内容", json.dumps(_v2(traits=["直率"])), 30, "g1")
        db.save_user_profile("u5", "也有内容", json.dumps(_v2(likes=["吉他"])), 30, "g1")
        monkeypatch.setattr(main, "db", db)
        body = self._client().get("/api/profiles").get_json()

        for row in body["profiles"]:
            prof = row["profile"]
            stable = prof["stable"]
            has_something = any([
                stable.get("traits"), stable.get("likes"), stable.get("notes"),
                prof["recent"].get("mood"), prof["recent"].get("doing"),
                row.get("memories"),
            ])
            assert has_something, f"{row['user_name']} 会渲染成空卡片"
