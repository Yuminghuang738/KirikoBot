"""好感度：事件制，不是积分制。

## 为什么有这些测试

旧版是积分制：每条消息 +0.1（日限 5）、每次工具调用 +0.05（**无上限**）、
判官按条 ±2.0（**也无总量上限**）。生产库实测的结果很能说明问题：

    挚友(≥80)   6 人   均值 98.3   ← 3 个顶在 100
    亲密(≥60)   6 人   均值 66.7
    友好(≥40)  58 人   均值 51.7   ← 全是初始值
    普通/冷淡    0 人              ← 数学上到不了（掉分封底卡在 50）

所以这组测试钉的是**两件核心事**：刷屏不给分、关系可以变差。
"""
from __future__ import annotations

from datetime import datetime

import pytest

from affection_service import (
    DAILY_EVENT_CAP,
    DECAY_FLOOR,
    SCORE_DEFAULT,
    SENTIMENT_DAILY_NET,
    SENTIMENT_MAX,
    AffectionService,
)


def _expected_first_day() -> float:
    """第一天第一次说话能拿到多少分（深夜会多 0.1，得算进去）。"""
    total = 1.0 + 0.3                      # first_ever + first_today
    if 0 <= datetime.now().hour < 5:
        total += 0.1                       # deep_night
    return total


class TestRelationshipLevels:
    @pytest.mark.parametrize("score,label", [
        (100, "挚友"), (85, "挚友"), (84.9, "亲密"),
        (65, "亲密"), (64.9, "友好"), (40, "友好"),
        (39.9, "普通"), (20, "普通"), (19.9, "冷淡"), (0, "冷淡"),
    ])
    def test_thresholds(self, score, label):
        """从 80/60 调成 85/65：顶端收紧，让"挚友"真的稀有。"""
        assert AffectionService.get_relationship(score)[0] == label

    def test_is_close_covers_亲密_and_above(self):
        assert AffectionService.is_close(65) is True
        assert AffectionService.is_close(64.9) is False


class TestEventsNotVolume:
    """核心：奖励「发生过什么」，不奖励「说了多少句」。"""

    def test_the_first_message_earns_events(self, db):
        score = AffectionService.record_interaction(db, "u1", "g1", "小明")
        assert score == pytest.approx(SCORE_DEFAULT + _expected_first_day(), abs=0.01)

    def test_sheer_volume_earns_nothing_more(self, db):
        """连发 19 条，除了第一天那点事件分，一分都不多给 —— 这是刷分的根。"""
        first = AffectionService.record_interaction(db, "u1", "g1", "小明")
        for _ in range(19):
            later = AffectionService.record_interaction(db, "u1", "g1", "小明")
        assert later == pytest.approx(first, abs=0.001)

    def test_tool_usage_no_longer_scores(self, db):
        """原来是 +0.05/次且无上限 —— 刷分主要入口，现在是空操作。"""
        before = AffectionService.get_or_create(db, "u1", "g1", "小明")["affection_score"]
        for _ in range(50):
            AffectionService.record_tool_usage(db, "u1", "g1", "小明")
        after = AffectionService.get_or_create(db, "u1", "g1", "小明")["affection_score"]
        assert after == pytest.approx(before, abs=0.001)

    def test_a_day_never_exceeds_the_event_cap(self, db):
        AffectionService.touch_streak(db, "u1", "g1", "小明")
        AffectionService.record_interaction(db, "u1", "g1", "小明", has_images=True)
        gained = AffectionService.get_or_create(db, "u1", "g1", "小明")["affection_score"] - SCORE_DEFAULT
        assert gained <= DAILY_EVENT_CAP + 0.001

    def test_private_chats_are_not_scored(self, db):
        """好感度是群内关系，私聊不计。"""
        assert AffectionService.record_interaction(db, "u1", None, "小明") is None


class TestStreak:
    def test_first_day_starts_at_one(self, db):
        assert AffectionService.touch_streak(db, "u1", "g1", "小明") == 1

    def test_same_day_twice_does_not_double(self, db):
        assert AffectionService.touch_streak(db, "u1", "g1", "小明") == 1
        assert AffectionService.touch_streak(db, "u1", "g1", "小明") == 1

    def test_consecutive_days_grow_then_a_gap_resets(self, db):
        AffectionService.touch_streak(db, "u1", "g1", "小明")
        # 手动把 last_seen_date 改成昨天，模拟连续两天
        yesterday = (datetime.now().replace(hour=0, minute=0, second=0)).timestamp() - 86400
        db.execute_action(
            "UPDATE user_affection SET last_seen_date=?, streak_days=3 "
            "WHERE user_id=? AND group_id=?",
            (datetime.fromtimestamp(yesterday).strftime("%Y-%m-%d"), "u1", "g1"),
        )
        assert AffectionService.touch_streak(db, "u1", "g1", "小明") == 4

        # 断签：上次是 10 天前 → 归 1
        ten_days_ago = (datetime.now().replace(hour=0, minute=0, second=0)).timestamp() - 86400 * 10
        db.execute_action(
            "UPDATE user_affection SET last_seen_date=? WHERE user_id=? AND group_id=?",
            (datetime.fromtimestamp(ten_days_ago).strftime("%Y-%m-%d"), "u1", "g1"),
        )
        assert AffectionService.touch_streak(db, "u1", "g1", "小明") == 1


class TestJudgeSentiment:
    """原来 ±2.0/条、无总量上限 —— 25 条消息就能从 50 刷到 100。"""

    def test_a_single_delta_is_clipped(self, db):
        AffectionService.apply_judge_delta(db, "u1", "g1", "小明", sentiment=9.9)
        gained = AffectionService.get_or_create(db, "u1", "g1", "小明")["affection_score"] - SCORE_DEFAULT
        assert gained == pytest.approx(SENTIMENT_MAX, abs=0.01)

    def test_the_daily_net_is_clipped(self, db):
        for _ in range(20):
            AffectionService.apply_judge_delta(db, "u1", "g1", "小明", sentiment=0.3)
        gained = AffectionService.get_or_create(db, "u1", "g1", "小明")["affection_score"] - SCORE_DEFAULT
        assert gained <= SENTIMENT_DAILY_NET + 0.01, "一天最多涨这么点，再多也没有"

    def test_negative_sentiment_also_clips(self, db):
        """负面有自己的日上限（非对称：比正面大，见 TestAsymmetricSentiment）。"""
        from affection_service import SENTIMENT_DAILY_NEG
        for _ in range(20):
            AffectionService.apply_judge_delta(db, "u1", "g1", "小明", sentiment=-0.3)
        lost = SCORE_DEFAULT - AffectionService.get_or_create(db, "u1", "g1", "小明")["affection_score"]
        assert lost <= SENTIMENT_DAILY_NEG + 0.01

    def test_learning_delta_still_counts(self, db):
        """学习分反映的是机器人质量，不该被情绪分的额度吃掉。"""
        AffectionService.apply_judge_delta(db, "u1", "g1", "小明", sentiment=0.0, learning=0.5)
        gained = AffectionService.get_or_create(db, "u1", "g1", "小明")["affection_score"] - SCORE_DEFAULT
        assert gained == pytest.approx(0.5, abs=0.01)


class TestDecay:
    """掉分从 0.5/周 调到 1.5/周，封底从 50 降到 20 —— 「普通/冷淡」终于可达。"""

    @staticmethod
    def _ago(days: int) -> str:
        from datetime import timedelta
        return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    def test_a_week_costs_1_5(self):
        assert AffectionService.apply_decay(90.0, self._ago(7)) == pytest.approx(88.5)

    def test_three_weeks_costs_4_5(self):
        assert AffectionService.apply_decay(90.0, self._ago(21)) == pytest.approx(85.5)

    def test_under_a_week_costs_nothing(self):
        assert AffectionService.apply_decay(90.0, self._ago(6)) == pytest.approx(90.0)

    def test_it_can_reach_普通_and_冷淡(self):
        """旧版封底 50，「普通/冷淡」永远到不了；现在能。"""
        assert DECAY_FLOOR <= 20
        far = AffectionService.apply_decay(50.0, self._ago(365))
        assert far == pytest.approx(DECAY_FLOOR)
        assert AffectionService.get_relationship(far)[0] in ("普通", "冷淡")

    def test_a_long_absence_adds_an_extra_penalty(self):
        """30 天以上额外再掉 3 分。"""
        three_weeks = AffectionService.apply_decay(90.0, self._ago(28))
        five_weeks = AffectionService.apply_decay(90.0, self._ago(35))
        # 28 天 → 4*1.5 = 6；35 天 → 5*1.5 + 3 = 10.5
        assert three_weeks == pytest.approx(84.0)
        assert five_weeks == pytest.approx(79.5)

    def test_it_never_goes_below_the_floor(self):
        assert AffectionService.apply_decay(30.0, self._ago(3650)) == pytest.approx(DECAY_FLOOR)
        assert AffectionService.apply_decay(10.0, self._ago(3650)) == pytest.approx(10.0)


class TestRelationshipBlock:
    """关系块给的是**可执行的动作**，不是"什么感觉"。"""

    def test_it_reads_the_current_level(self, db):
        db.set_rate_limit  # noqa: B018  (只是确保 db fixture 可用)
        AffectionService.manual_adjust(db, "u1", "g1", 40.0)   # 50 → 90
        block = AffectionService.relationship_block(db, "u1", "g1")
        assert "挚友" in block
        assert "可以接他的梗" in block, "挚友档要给出具体的亲近行为"

    def test_a_stranger_gets_told_not_to_force_it(self, db):
        AffectionService.manual_adjust(db, "u1", "g1", -35.0)  # 50 → 15
        block = AffectionService.relationship_block(db, "u1", "g1")
        assert "印象不好" in block
        assert "别多说" in block and "不接梗" in block

    def test_private_has_no_relationship_block(self, db):
        assert AffectionService.relationship_block(db, "u1", None) == ""


class TestRecalibration:
    """一次性重算用的纯函数（脚本和测试共用）。"""

    def test_it_never_exceeds_the_bounds(self):
        from affection_service import clamp_score
        assert clamp_score(999) == 100.0
        assert clamp_score(-5) == 0.0

    def test_a_farmed_account_comes_down(self):
        """按「来过多少天 + 连续多久」折算：靠刷消息刷到 100 的会掉回合理区间。"""
        from affection_service import recalibrated_score

        # 一天刷 200 条 → 实际只来过 1 天
        assert recalibrated_score(days_seen=1, streak=1) < 60
        # 真的连续来了两个月
        assert recalibrated_score(days_seen=60, streak=30) >= 85

    def test_it_does_not_look_at_the_old_score(self):
        """旧分数本身就是错的，拿它做基准等于把刷出来的结果请回来。"""
        from affection_service import recalibrated_score
        assert recalibrated_score(days_seen=1, streak=1) == recalibrated_score(days_seen=1, streak=1)


class TestAsymmetricSentiment:
    """信任被破坏的速度快于建立。之前正负对称，负面永远追不上日常的小正分。"""

    def test_a_hostile_message_hurts_more_than_a_thank_you_helps(self, db):
        from affection_service import SENTIMENT_MAX, SENTIMENT_NEG_MAX
        assert SENTIMENT_NEG_MAX > SENTIMENT_MAX

        AffectionService.apply_judge_delta(db, "u1", "g1", "A", sentiment=0.8)
        up = AffectionService.get_or_create(db, "u1", "g1", "A")["affection_score"] - SCORE_DEFAULT
        AffectionService.apply_judge_delta(db, "u2", "g1", "B", sentiment=-0.8)
        down = SCORE_DEFAULT - AffectionService.get_or_create(db, "u2", "g1", "B")["affection_score"]
        assert down > up, f"辱骂应比感谢影响更大：down={down} up={up}"

    def test_a_single_hostile_message_is_clipped_at_the_negative_max(self, db):
        from affection_service import SENTIMENT_NEG_MAX
        AffectionService.apply_judge_delta(db, "u1", "g1", "A", sentiment=-9.0)
        lost = SCORE_DEFAULT - AffectionService.get_or_create(db, "u1", "g1", "A")["affection_score"]
        assert lost == pytest.approx(SENTIMENT_NEG_MAX, abs=0.01)

    def test_the_daily_negative_cap_is_larger_than_the_positive_one(self, db):
        from affection_service import SENTIMENT_DAILY_NEG, SENTIMENT_DAILY_NET
        assert SENTIMENT_DAILY_NEG > SENTIMENT_DAILY_NET
        for _ in range(20):
            AffectionService.apply_judge_delta(db, "u1", "g1", "A", sentiment=-0.6)
        lost = SCORE_DEFAULT - AffectionService.get_or_create(db, "u1", "g1", "A")["affection_score"]
        assert lost == pytest.approx(SENTIMENT_DAILY_NEG, abs=0.01)

    def test_hostile_days_actually_move_a_friend_down_the_tiers(self, db):
        """业务结论：一个持续辱骂的人，应该能从「友好」掉到「普通」以下，而不是永远卡在友好。"""
        from affection_service import SENTIMENT_DAILY_NEG
        # 每天一次辱骂，单日封顶 -SENTIMENT_DAILY_NEG；连续 4 天 ≈ -8，应掉到 40 以下
        for _ in range(4):
            AffectionService.update_score(db, "u1", "g1", "A", -SENTIMENT_DAILY_NEG)
        score = AffectionService.get_or_create(db, "u1", "g1", "A")["affection_score"]
        assert score < 45
        # 再 4 天 ≈ 从 42 掉到 34，已经进了「普通」以下区间的边缘；再来两天就到冷淡
        for _ in range(4):
            AffectionService.update_score(db, "u1", "g1", "A", -SENTIMENT_DAILY_NEG)
        score = AffectionService.get_or_create(db, "u1", "g1", "A")["affection_score"]
        assert AffectionService.get_relationship(score)[0] in ("普通", "冷淡")
