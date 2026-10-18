"""回复频率限制：滑动窗口、两种模式、以及它们怎么组合。

两种模式各自独立开关：
  · 单用户 —— N 分钟内对同一个用户最多回 Y 条（对所有用户一视同仁）
  · 群聊   —— N 分钟内在这个群最多发 Y 条

这里的时钟是注入的，所以「窗口过期」可以精确测，不用 sleep。
"""
from __future__ import annotations

import pytest

from rate_limiter import RateLimiter


class FakeDB:
    """只提供 get_rate_limit 的替身。"""

    def __init__(self, cfg: dict | None = None) -> None:
        self.cfg = cfg or {}
        self.fail = False
        self.reads = 0

    def get_rate_limit(self, group_id: str) -> dict:
        self.reads += 1
        if self.fail:
            raise RuntimeError("db is down")
        return self.cfg.get(group_id, {})


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def make(cfg: dict | None = None, gid: str = "g1"):
    db = FakeDB({gid: cfg} if cfg else {})
    clock = Clock()
    return RateLimiter(db, clock=clock), clock, db


class TestDisabled:
    def test_no_config_means_no_limit(self):
        lim, _, _ = make()
        for _ in range(1000):
            assert lim.allow("g1", "u1") is True

    def test_both_modes_off_means_no_limit(self):
        lim, _, _ = make({"user_enabled": False, "group_enabled": False})
        for _ in range(100):
            assert lim.allow("g1", "u1") is True

    def test_private_chat_is_never_limited(self):
        """这个功能是给群聊设计的，group_id 为空直接放行。"""
        lim, _, _ = make({"user_enabled": True, "user_window_min": 1, "user_max": 1,
                          "group_enabled": True, "group_window_min": 1, "group_max": 1})
        for _ in range(50):
            assert lim.allow("", "u1") is True


class TestUserMode:
    CFG = {"user_enabled": True, "user_window_min": 10, "user_max": 3}

    def test_it_allows_up_to_the_limit_then_blocks(self):
        lim, _, _ = make(self.CFG)
        assert [lim.allow("g1", "u1") for _ in range(3)] == [True, True, True]
        assert lim.allow("g1", "u1") is False

    def test_the_window_slides(self):
        lim, clock, _ = make(self.CFG)
        for _ in range(3):
            assert lim.allow("g1", "u1")
        assert lim.allow("g1", "u1") is False
        clock.advance(10 * 60 + 1)          # 窗口过了
        assert lim.allow("g1", "u1") is True

    def test_it_is_uniform_across_users(self):
        """对所有用户一视同仁 —— 不是给某个人单独设。"""
        lim, _, _ = make(self.CFG)
        for _ in range(3):
            assert lim.allow("g1", "u1")
        assert lim.allow("g1", "u1") is False
        # 换个用户，额度是独立的
        assert lim.allow("g1", "u2") is True

    def test_users_are_separate_per_group(self):
        lim, _, _ = make(self.CFG)
        db_cfg = dict(self.CFG)
        lim.db.cfg["g2"] = db_cfg
        for _ in range(3):
            assert lim.allow("g1", "u1")
        assert lim.allow("g1", "u1") is False
        assert lim.allow("g2", "u1") is True

    def test_a_partially_expired_window_only_frees_the_slots_that_aged_out(self):
        lim, clock, _ = make(self.CFG)
        assert lim.allow("g1", "u1")          # t=1000
        clock.advance(300)
        assert lim.allow("g1", "u1")          # t=1300
        assert lim.allow("g1", "u1")          # t=1300
        assert lim.allow("g1", "u1") is False
        clock.advance(301)                    # t=1601，第一次的已经出窗
        assert lim.allow("g1", "u1") is True
        assert lim.allow("g1", "u1") is False  # 只剩一个空位


class TestGroupMode:
    CFG = {"group_enabled": True, "group_window_min": 10, "group_max": 4}

    def test_it_limits_the_whole_group(self):
        lim, _, _ = make(self.CFG)
        assert lim.allow("g1", "u1")
        assert lim.allow("g1", "u2")          # 换个人也不行 —— 这是群级额度
        assert lim.allow("g1", "u3")
        assert lim.allow("g1", "u4")
        assert lim.allow("g1", "u5") is False

    def test_the_window_slides(self):
        lim, clock, _ = make(self.CFG)
        for _ in range(4):
            assert lim.allow("g1", "u1")
        assert lim.allow("g1", "u1") is False
        clock.advance(601)
        assert lim.allow("g1", "u1") is True


class TestBothModes:
    def test_either_one_can_block(self):
        cfg = {"user_enabled": True, "user_window_min": 10, "user_max": 10,
               "group_enabled": True, "group_window_min": 10, "group_max": 2}
        lim, _, _ = make(cfg)
        assert lim.allow("g1", "u1")
        assert lim.allow("g1", "u2")
        # 群额度用完了，即使 u1 自己的额度还有
        assert lim.allow("g1", "u1") is False

    def test_the_user_limit_can_block_first(self):
        cfg = {"user_enabled": True, "user_window_min": 10, "user_max": 1,
               "group_enabled": True, "group_window_min": 10, "group_max": 100}
        lim, _, _ = make(cfg)
        assert lim.allow("g1", "u1")
        assert lim.allow("g1", "u1") is False   # 用户额度先到
        assert lim.allow("g1", "u2") is True    # 群里还有额度


class TestConfigHandling:
    def test_it_caches_the_config(self):
        lim, _, db = make({"user_enabled": True, "user_window_min": 10, "user_max": 99})
        for _ in range(20):
            lim.allow("g1", "u1")
        assert db.reads == 1, "每条消息都查库就太浪费了"

    def test_invalidate_forces_a_reread(self):
        lim, _, db = make({"user_enabled": False})
        lim.allow("g1", "u1")
        before = db.reads
        lim.invalidate("g1")
        lim.allow("g1", "u1")
        assert db.reads == before + 1

    def test_a_changed_config_takes_effect_after_invalidate(self):
        lim, _, db = make({"user_enabled": False})
        assert lim.allow("g1", "u1")
        db.cfg["g1"] = {"user_enabled": True, "user_window_min": 10, "user_max": 1}
        lim.invalidate("g1")
        assert lim.allow("g1", "u1") is True
        assert lim.allow("g1", "u1") is False

    def test_a_broken_config_read_fails_open(self):
        """限流是保护措施，它坏了不该把聊天堵死。"""
        lim, _, db = make({"user_enabled": True, "user_window_min": 1, "user_max": 1})
        db.fail = True
        for _ in range(10):
            assert lim.allow("g1", "u1") is True

    def test_missing_fields_fall_back_to_defaults(self):
        lim, _, _ = make({"group_enabled": True, "group_max": 2})
        assert lim.allow("g1", "u1")
        assert lim.allow("g1", "u1")
        assert lim.allow("g1", "u1") is False


class TestPersistence:
    def test_roundtrip(self, db):
        saved = db.set_rate_limit("g1", user_enabled=True, user_window_min=5,
                                  user_max=7, group_enabled=True,
                                  group_window_min=15, group_max=40)
        assert saved["user_enabled"] is True
        assert saved["user_window_min"] == 5
        assert saved["user_max"] == 7
        assert saved["group_enabled"] is True
        assert saved["group_window_min"] == 15
        assert saved["group_max"] == 40

        again = db.get_rate_limit("g1")
        assert again["user_max"] == 7 and again["group_max"] == 40

    def test_unconfigured_group_is_empty(self, db):
        assert db.get_rate_limit("nope") == {}

    def test_partial_update_keeps_the_rest(self, db):
        db.set_rate_limit("g1", user_enabled=True, user_max=9)
        db.set_rate_limit("g1", group_enabled=True)
        got = db.get_rate_limit("g1")
        assert got["user_enabled"] is True and got["user_max"] == 9
        assert got["group_enabled"] is True

    def test_out_of_range_values_are_clamped(self, db):
        got = db.set_rate_limit("g1", user_window_min=0, user_max=99999)
        assert got["user_window_min"] >= 1
        assert got["user_max"] <= 1000

    def test_switching_a_mode_off_persists(self, db):
        db.set_rate_limit("g1", user_enabled=True)
        assert db.get_rate_limit("g1")["user_enabled"] is True
        db.set_rate_limit("g1", user_enabled=False)
        assert db.get_rate_limit("g1")["user_enabled"] is False

    def test_all_rate_limits_lists_configured_groups(self, db):
        db.set_rate_limit("g1", user_enabled=True)
        db.set_rate_limit("g2", group_enabled=True)
        rows = {r["group_id"]: r for r in db.all_rate_limits()}
        assert set(rows) == {"g1", "g2"}
        assert rows["g1"]["user_enabled"] is True
        assert rows["g2"]["group_enabled"] is True

    def test_purging_a_group_clears_its_limit(self, db):
        db.set_rate_limit("g1", user_enabled=True)
        db.purge_group("g1")
        assert db.get_rate_limit("g1") == {}
