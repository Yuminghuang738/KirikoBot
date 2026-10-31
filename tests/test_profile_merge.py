"""画像合并：重新分析不能把已有的好特征覆盖掉。

线上实测：整体覆盖让一个用户的已填字段从 4 个掉到 3 个。
"""
from __future__ import annotations

import pytest

from profile_service import merge_profiles


def _p(traits=None, likes=None, nickname="", notes="", mood="", doing="", avoid=None, cat=None, style=None):
    return {"version": 2,
            "stable": {"traits": traits or [], "nickname": nickname, "avoid": avoid or [],
                       "catchphrase": cat or [], "style": style or {"length": "", "emoji": "", "night": False},
                       "likes": likes or [], "dislikes": [], "notes": notes},
            "recent": {"mood": mood, "doing": doing, "mentioned": []}}


class TestListFields:
    def test_a_new_analysis_missing_a_trait_does_not_drop_the_old_one(self):
        old = _p(likes=["吉他", "乐器"])
        new = _p(likes=["音乐"])
        out = merge_profiles(old, new)
        assert "吉他" in out["stable"]["likes"], "旧的好特征不能因为一次漏掉而丢"
        assert "音乐" in out["stable"]["likes"]

    def test_new_items_come_first(self):
        out = merge_profiles(_p(likes=["旧"]), _p(likes=["新"]))
        assert out["stable"]["likes"][0] == "新"

    def test_duplicates_are_removed(self):
        out = merge_profiles(_p(likes=["吉他"]), _p(likes=["吉他", "鼓"]))
        assert out["stable"]["likes"].count("吉他") == 1

    def test_caps_are_respected(self):
        out = merge_profiles(_p(likes=[f"旧{i}" for i in range(10)]), _p(likes=[f"新{i}" for i in range(10)]))
        assert len(out["stable"]["likes"]) == 6

    def test_a_missed_avoid_item_is_kept(self):
        out = merge_profiles(_p(avoid=["别拿他的事开玩笑"]), _p(avoid=[]))
        assert out["stable"]["avoid"] == ["别拿他的事开玩笑"]


class TestStringFields:
    def test_a_new_nonempty_value_wins(self):
        out = merge_profiles(_p(notes="旧备注"), _p(notes="新备注"))
        assert out["stable"]["notes"] == "新备注"

    def test_an_empty_new_value_keeps_the_old(self):
        out = merge_profiles(_p(notes="旧备注", nickname="小猫"), _p(notes="", nickname=""))
        assert out["stable"]["notes"] == "旧备注"
        assert out["stable"]["nickname"] == "小猫"

    def test_style_fields_fall_back_to_old(self):
        old = _p(style={"length": "短句", "emoji": "偶尔", "night": True})
        new = _p(style={"length": "", "emoji": "", "night": False})
        out = merge_profiles(old, new)
        assert out["stable"]["style"]["length"] == "短句"
        assert out["stable"]["style"]["night"] is True


class TestRecent:
    def test_a_new_mood_replaces_the_old(self):
        out = merge_profiles(_p(mood="开心"), _p(mood="烦躁"))
        assert out["recent"]["mood"] == "烦躁"

    def test_an_empty_recent_keeps_the_old(self):
        out = merge_profiles(_p(doing="修吉他"), _p(doing=""))
        assert out["recent"]["doing"] == "修吉他"


class TestRobustness:
    def test_v1_input_is_accepted(self):
        v1 = {"personality": "直率", "interests": ["吉他"], "note": "老数据"}
        out = merge_profiles(v1, _p(likes=["鼓"]))
        assert out["version"] == 2
        assert "鼓" in out["stable"]["likes"] and "吉他" in out["stable"]["likes"]

    @pytest.mark.parametrize("bad", [None, {}, "x"])
    def test_garbage_does_not_raise(self, bad):
        out = merge_profiles(bad, _p(likes=["鼓"]))
        assert "鼓" in out["stable"]["likes"]


class TestWiredIntoAnalysis:
    def test_the_analysis_path_merges_with_the_stored_profile(self):
        import inspect
        import profile_service
        src = inspect.getsource(profile_service.ProfileService._do_analyze)
        assert "merge_profiles" in src
