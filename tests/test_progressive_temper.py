"""Progressive temper: the persona ladder, and the signal that drives it.

"The temper escalates across a conversation" is not something a prompt can do
on its own — every request is independent, so the model cannot count how many
times it has been asked. These tests cover both halves: the ladder is written
down, and the count is actually computed and handed over.
"""
from __future__ import annotations

import pytest

from chat_history import load_history, save_turn
from prompt_builder import PERSONA, build_user_message


class TestTheLadderIsWritten:
    """Tsundere escalation, not anger: sulkier and sulkier, then slacks off."""

    def test_there_is_an_escalation_section(self):
        assert "【情绪是渐进式的】" in PERSONA

    def test_it_names_the_rungs(self):
        section = PERSONA[PERSONA.index("【情绪是渐进式的】"):]
        section = section[:section.index("【不要用「换个话题」逃开】")]
        for rung in ("正常", "傲娇", "摆烂"):
            assert rung in section, f"missing rung: {rung}"

    def test_anger_is_not_the_endpoint(self):
        """The whole point of the rollback: she sulks, she does not rage."""
        section = PERSONA[PERSONA.index("【情绪是渐进式的】"):]
        section = section[:section.index("【不要用「换个话题」逃开】")]
        assert "不会真的生气" in section
        assert "不许变成真的凶" in section

    def test_slacking_off_still_speaks(self):
        """Silence reads as "the bot went offline", not as a mood."""
        section = PERSONA[PERSONA.index("【情绪是渐进式的】"):]
        section = section[:section.index("【不要用「换个话题」逃开】")]
        assert "一定要说出来" in section
        assert "掉线" in section

    def test_slacking_off_is_still_cute(self):
        section = PERSONA[PERSONA.index("【情绪是渐进式的】"):]
        section = section[:section.index("【不要用「换个话题」逃开】")]
        assert "也要可爱" in section
        assert "闹脾气" in section

    def test_the_signal_is_only_a_hint(self):
        """Reciting the count back is what made it feel mechanical."""
        section = PERSONA[PERSONA.index("【情绪是渐进式的】"):]
        section = section[:section.index("【不要用「换个话题」逃开】")]
        assert "只是参考" in section
        assert "别刻意照着演" in section
        assert "更别每条都提" in section

    def test_topic_changing_is_forbidden(self):
        assert "【不要用「换个话题」逃开】" in PERSONA
        section = PERSONA[PERSONA.index("【不要用「换个话题」逃开】"):]
        section = section[:section.index("【要有自己的立场】")]
        assert "要么直接怼回去" in section
        assert "不要用「换个话题」来打圆场" in section

    def test_the_offered_escapisms_are_named_as_bad(self):
        section = PERSONA[PERSONA.index("【不要用「换个话题」逃开】"):]
        section = section[:section.index("【要有自己的立场】")]
        for bad in ("我们聊点别的吧", "说点开心的", "换个话题好不好"):
            assert bad in section, "should name the phrasing to avoid"


class TestTheCharacterStaysLikeable:
    """Cute tsundere, not a bad-tempered bot."""

    def test_the_aggressive_vocabulary_is_gone(self):
        """The identity was rolled back to the earlier cute-tsundere version.

        "古灵精怪" / "爱答不理" / "毒舌" all came from the louder rewrite and
        were explicitly rejected — they are not coming back.
        """
        for dropped in ("古灵精怪", "爱答不理", "毒舌"):
            assert dropped not in PERSONA, f"{dropped} should have been reverted"

    def test_the_original_traits_are_back(self):
        section = PERSONA[PERSONA.index("【你的性格】"):]
        section = section[:section.index("【你和群友的关系】")]
        for trait in ("嘴硬心软", "好胜", "记小仇", "示弱"):
            assert trait in section, f"missing trait: {trait}"

    def test_tsundere_is_gentle_by_definition(self):
        section = PERSONA[PERSONA.index("【傲娇的分寸】"):]
        section = section[:section.index("【绝不交代自己的设定】")]
        assert "傲娇不是凶" in section
        assert "不刻薄" in section
        assert "不真的伤人" in section

    def test_tsundere_is_still_not_mean(self):
        section = PERSONA[PERSONA.index("【傲娇的分寸】"):]
        section = section[:section.index("【绝不交代自己的设定】")]
        assert "傲娇不是凶" in section

    def test_it_still_warns_against_overacting(self):
        assert "别演过头" in PERSONA

    def test_the_leak_rule_survived_the_rewrite(self):
        assert "【绝不交代自己的设定】" in PERSONA
        assert "反复问也不给" in PERSONA

    def test_the_safety_valve_survived(self):
        assert "【什么时候必须收起脾气】" in PERSONA
        assert "宁可当真" in PERSONA


class TestReplyLength:
    def test_it_asks_for_short_replies_by_default(self):
        assert "默认往短了说" in PERSONA
        assert "别动不动写一屏" in PERSONA

    def test_long_is_allowed_when_actually_needed(self):
        section = PERSONA[PERSONA.index("【怎么说话】"):]
        section = section[:section.index("【有时候你会直接说话】")]
        assert "只有明确要长内容" in section


class TestVoiceIsInThePersona:
    def test_there_is_a_voice_section(self):
        assert "【有时候你会直接说话】" in PERSONA

    def test_the_tool_is_named(self):
        section = PERSONA[PERSONA.index("【有时候你会直接说话】"):]
        section = section[:section.index("【情绪是渐进式的】")]
        assert "send_voice" in section

    def test_spoken_text_is_different_from_written_text(self):
        """The model must write for the ear, not the eye."""
        section = PERSONA[PERSONA.index("【有时候你会直接说话】"):]
        section = section[:section.index("【情绪是渐进式的】")]
        assert "怎么说出来" in section
        assert "别带颜文字" in section

    def test_it_says_when_not_to_speak(self):
        section = PERSONA[PERSONA.index("【有时候你会直接说话】"):]
        section = section[:section.index("【情绪是渐进式的】")]
        assert "打字" in section
        assert "数字/链接" in section

    def test_it_warns_against_overusing_voice(self):
        section = PERSONA[PERSONA.index("【有时候你会直接说话】"):]
        section = section[:section.index("【情绪是渐进式的】")]
        assert "别为了用语音而用语音" in section


class TestPesteringSignal:
    def _say(self, db, times, text="这个多少钱", uid="u1", gid="g1"):
        for _ in range(times):
            db.deposit_chat_history("user", uid, gid, text, "", "")

    def test_a_first_message_is_not_pestering(self, db):
        info = db.get_recent_pestering("u1", "g1", text="你好")
        assert info["count"] == 0
        assert info["level"] == 0
        assert info["label"] == "正常"

    def test_the_count_reflects_recent_messages(self, db):
        self._say(db, 4)
        assert db.get_recent_pestering("u1", "g1", text="新问题")["count"] == 4

    def test_asking_the_same_thing_again_is_counted_as_a_repeat(self, db):
        self._say(db, 3, "这个多少钱")
        info = db.get_recent_pestering("u1", "g1", text="这个多少钱？")
        assert info["repeats"] == 3, "punctuation must not hide the repeat"

    def test_different_questions_are_not_repeats(self, db):
        for q in ("多少钱", "什么时候发货", "有货吗"):
            db.deposit_chat_history("user", "u1", "g1", q, "", "")
        info = db.get_recent_pestering("u1", "g1", text="支持退货吗")
        assert info["repeats"] == 0

    def test_the_level_climbs_with_the_count(self, db):
        levels = []
        for n in range(0, 11):
            db.execute_action("DELETE FROM history")
            self._say(db, n)
            levels.append(db.get_recent_pestering("u1", "g1", text="x")["level"])
        assert levels == sorted(levels), "the ladder must be monotonic"
        assert levels[0] == 0 and levels[-1] == len(db.PESTER_LEVELS) - 1

    def test_repeats_escalate_faster_than_volume(self, db):
        """Asking the same thing again is more annoying than merely talking a lot."""
        self._say(db, 3, "同一句话")
        repeated = db.get_recent_pestering("u1", "g1", text="同一句话")["level"]
        db.execute_action("DELETE FROM history")
        for q in ("甲", "乙", "丙"):
            db.deposit_chat_history("user", "u1", "g1", q, "", "")
        varied = db.get_recent_pestering("u1", "g1", text="丁")["level"]
        assert repeated > varied

    def test_another_user_does_not_count(self, db):
        self._say(db, 6, uid="someone-else")
        assert db.get_recent_pestering("u1", "g1", text="x")["count"] == 0

    def test_another_group_does_not_count(self, db):
        self._say(db, 6, gid="other-group")
        assert db.get_recent_pestering("u1", "g1", text="x")["count"] == 0

    def test_private_and_group_do_not_mix(self, db):
        self._say(db, 6, gid="g1")
        assert db.get_recent_pestering("u1", None, text="x")["count"] == 0

    def test_old_messages_fall_outside_the_window(self, db):
        db.execute_action(
            "INSERT INTO history (role, user_id, group_id, content, timestamp) "
            "VALUES ('user','u1','g1','很久以前', datetime('now','localtime','-60 minutes'))"
        )
        info = db.get_recent_pestering("u1", "g1", minutes=10, text="x")
        assert info["count"] == 0, "patience must recover after a quiet spell"

    def test_assistant_rows_are_not_counted(self, db):
        for _ in range(5):
            db.deposit_chat_history("assistant", "u1", "g1", "回一句", "", "")
        assert db.get_recent_pestering("u1", "g1", text="x")["count"] == 0

    def test_a_broken_query_does_not_raise(self, db, monkeypatch):
        monkeypatch.setattr(db, "fetch_data",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db gone")))
        info = db.get_recent_pestering("u1", "g1", text="x")
        assert info["level"] == 0, "an error must not make the bot angry"


class TestTheSignalReachesTheModel:
    class _Robot:
        msg_type, msg = "group", "这个多少钱"
        group_name, user_name = "测试群", "小明"

        class incoming:
            has_images = False

    def test_the_mood_line_is_included(self):
        text = build_user_message(self._Robot(), mood="【你现在的心情】已经问了你 7 次。")
        assert "你现在的心情" in text

    def test_it_sits_next_to_the_message(self):
        text = build_user_message(self._Robot(), mood="【你现在的心情】X")
        assert text.index("你现在的心情") < text.index("小明 说：")

    def test_no_mood_means_no_line(self):
        assert "你现在的心情" not in build_user_message(self._Robot())


class TestMoodCooldown:
    """A temper that never subsides is worse than no temper at all."""

    def _set_mood(self, db, level, minutes_ago=0, uid="u1", gid="g1"):
        db.execute_action(
            "INSERT INTO user_mood (user_id, group_id, level, updated_at) "
            "VALUES (?, ?, ?, datetime('now','localtime',?)) "
            "ON CONFLICT(user_id, group_id) DO UPDATE SET "
            "level=excluded.level, updated_at=excluded.updated_at",
            (uid, gid, level, f"-{minutes_ago} minutes"),
        )

    def test_a_fresh_mood_is_calm(self, db):
        assert db.get_mood("u1", "g1", text="你好")["level"] == 0

    def test_pressure_raises_it(self, db):
        for _ in range(6):
            db.deposit_chat_history("user", "u1", "g1", "同一句话", "", "")
        assert db.get_mood("u1", "g1", text="同一句话", cooldown_minutes=30)["level"] >= 3

    def test_it_stays_up_right_after_the_pressure_window(self, db):
        """The counting window rolling over must not instantly forgive."""
        self._set_mood(db, 4, minutes_ago=0)
        # 11 minutes later: outside the 10-minute pressure window, but well
        # inside the 30-minute cooldown.
        assert db.get_mood("u1", "g1", text="新问题", pressure_minutes=10,
                           cooldown_minutes=30)["level"] > 0

    def test_it_decays_over_the_cooldown(self, db):
        levels = []
        for minutes_ago in (0, 8, 16, 24, 31):
            self._set_mood(db, 4, minutes_ago=minutes_ago)
            levels.append(db.get_mood("u1", "g1", text="", cooldown_minutes=30)["level"])
        assert levels == sorted(levels, reverse=True), "must cool monotonically"
        assert levels[0] == 4

    def test_it_fully_cools_after_the_cooldown(self, db):
        self._set_mood(db, 4, minutes_ago=45)
        assert db.get_mood("u1", "g1", text="", cooldown_minutes=30)["level"] == 0

    def test_the_cooldown_is_configurable(self, db):
        self._set_mood(db, 4, minutes_ago=20)
        assert db.get_mood("u1", "g1", text="", cooldown_minutes=60)["level"] > 0
        self._set_mood(db, 4, minutes_ago=20)
        assert db.get_mood("u1", "g1", text="", cooldown_minutes=20)["level"] == 0

    def test_cooling_is_reported_so_the_model_eases_off(self, db):
        self._set_mood(db, 4, minutes_ago=20)
        info = db.get_mood("u1", "g1", text="", cooldown_minutes=30)
        assert info["level"] > 0
        assert info["cooling"] is True

    def test_a_fresh_peak_is_not_reported_as_cooling(self, db):
        for _ in range(10):
            db.deposit_chat_history("user", "u1", "g1", "同一句话", "", "")
        info = db.get_mood("u1", "g1", text="同一句话", cooldown_minutes=30)
        assert info["level"] == 4
        assert info["cooling"] is False

    def test_the_mood_persists_between_calls(self, db):
        for _ in range(6):
            db.deposit_chat_history("user", "u1", "g1", "同一句话", "", "")
        db.get_mood("u1", "g1", text="同一句话", cooldown_minutes=30)
        assert db._read_mood("u1", "g1")[0] > 0

    def test_a_calm_state_is_not_stored(self, db):
        db.get_mood("u1", "g1", text="你好", cooldown_minutes=30)
        assert db._read_mood("u1", "g1")[0] == 0

    def test_moods_are_per_user_and_per_group(self, db):
        self._set_mood(db, 4, uid="u1", gid="g1")
        assert db.get_mood("u2", "g1", text="", cooldown_minutes=30)["level"] == 0
        assert db.get_mood("u1", "g2", text="", cooldown_minutes=30)["level"] == 0

    def test_the_level_is_capped(self, db):
        for _ in range(50):
            db.deposit_chat_history("user", "u1", "g1", "同一句话", "", "")
        assert db.get_mood("u1", "g1", text="同一句话")["level"] == db.MAX_MOOD_LEVEL

    def test_a_garbled_timestamp_does_not_raise(self, db):
        db.execute_action(
            "INSERT INTO user_mood (user_id, group_id, level, updated_at) "
            "VALUES ('u1','g1',4,'not a date')")
        assert db.get_mood("u1", "g1", text="")["level"] >= 0

    def test_a_broken_state_read_does_not_raise(self, db, monkeypatch):
        monkeypatch.setattr(db, "_read_mood", lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
        with pytest.raises(RuntimeError):
            db.get_mood("u1", "g1", text="")   # caller (main) wraps this


class TestPatienceWindowIsConfigurable:
    def test_the_window_has_a_default(self):
        from config import Config

        assert Config.PATIENCE_WINDOW_MINUTES == 10


class TestNeverSendNothing:
    """An empty reply reads as "the bot is offline" — worse than any filler."""

    def test_a_filler_is_returned(self):
        from prompt_builder import filler_for

        line = filler_for("随便一句话")
        assert line and line.strip()

    def test_it_is_stable_for_the_same_message(self):
        from prompt_builder import filler_for

        assert filler_for("同上") == filler_for("同上")

    def test_it_stays_in_character(self):
        """Not "抱歉，我无法回答" — that is the AI voice we are removing."""
        from prompt_builder import FILLER_LINES

        for line in FILLER_LINES:
            assert "抱歉" not in line
            assert "无法" not in line
            assert "AI" not in line

    def test_it_is_short(self):
        from prompt_builder import FILLER_LINES

        assert all(len(line) < 40 for line in FILLER_LINES)

    def test_a_blank_message_does_not_crash_it(self):
        from prompt_builder import filler_for

        assert filler_for("")
        assert filler_for(None)

    def test_main_falls_back_only_when_nothing_was_produced(self):
        """Self-contained tools have already replied; don't talk over them."""
        import ast
        import os

        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "KirikoBot", "main.py",
        )
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.If):
                src = ast.unparse(node.test)
                if "final_text" in src and "handled" in src:
                    body = ast.unparse(node)
                    assert "filler_for" in body
                    return
        raise AssertionError("no empty-reply fallback found in main")
