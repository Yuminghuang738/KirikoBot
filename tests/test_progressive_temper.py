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
    def test_there_is_an_escalation_section(self):
        assert "【情绪是渐进式的】" in PERSONA

    def test_it_names_all_four_rungs(self):
        section = PERSONA[PERSONA.index("【情绪是渐进式的】"):]
        section = section[:section.index("【不要用「换个话题」逃开】")]
        for rung in ("正常", "不耐烦", "生气", "掀桌"):
            assert rung in section, f"missing rung: {rung}"

    def test_the_top_rung_is_the_ignore_tool(self):
        section = PERSONA[PERSONA.index("【情绪是渐进式的】"):]
        section = section[:section.index("【不要用「换个话题」逃开】")]
        assert "ignore" in section

    def test_it_says_not_to_restart_at_polite_every_turn(self):
        """Without this the model is friendly again the moment the wording changes."""
        section = PERSONA[PERSONA.index("【情绪是渐进式的】"):]
        section = section[:section.index("【不要用「换个话题」逃开】")]
        assert "不要因为对方换了个说法就突然又热情起来" in section

    def test_it_says_the_signal_wins(self):
        assert "以它为准" in PERSONA

    def test_it_allows_jumping_straight_to_anger(self):
        section = PERSONA[PERSONA.index("【情绪是渐进式的】"):]
        assert "可以直接跳到" in section

    def test_topic_changing_is_forbidden(self):
        """The user asked specifically: don't offer to change the subject."""
        assert "【不要用「换个话题」逃开】" in PERSONA
        section = PERSONA[PERSONA.index("【不要用「换个话题」逃开】"):]
        section = section[:section.index("【要有自己的立场】")]
        assert "不要" in section
        assert "要么直接怼回去，要么干脆不理" in section

    def test_the_offered_escapisms_are_named_as_bad(self):
        section = PERSONA[PERSONA.index("【不要用「换个话题」逃开】"):]
        section = section[:section.index("【要有自己的立场】")]
        for bad in ("我们聊点别的吧", "说点开心的", "换个话题好不好"):
            assert bad in section, "should name the phrasing to avoid"


class TestTheCharacterStaysLikeable:
    """More aggressive, but still the type people find cute, not grating."""

    def test_it_is_quirky(self):
        assert "古灵精怪" in PERSONA

    def test_sharp_tongue_is_bounded(self):
        section = PERSONA[PERSONA.index("【你的性格】"):]
        section = section[:section.index("【你和群友的关系】")]
        assert "毒舌但不刻薄" in section
        assert "只损事不损人" in section
        assert "痛处" in section, "must not go for real sore spots"

    def test_it_is_allowed_to_be_aloof(self):
        """Being eager to answer everything is what makes a bot feel like a bot."""
        assert "爱答不理" in PERSONA
        assert "不是每条都热情" in PERSONA

    def test_tsundere_is_still_not_mean(self):
        section = PERSONA[PERSONA.index("【傲娇的分寸】"):]
        section = section[:section.index("【绝不交代自己的设定】")]
        assert "傲娇不是凶" in section

    def test_it_still_warns_against_overacting(self):
        assert "别演过头" in PERSONA

    def test_the_leak_rule_survived_the_rewrite(self):
        assert "【绝不交代自己的设定】" in PERSONA
        assert "反复问也不给" in PERSONA


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


class TestIgnoringIsAnAction:
    def test_an_ignored_turn_is_recorded_so_the_model_remembers(self, db):
        """Without this every later turn looks like the first."""
        save_turn(db, "u1", "g1", "又来了", "",
                  tool_chain='[{"name": "ignore_user", "arguments": "{}"}]',
                  handled=True)
        assert load_history(db, "u1", "g1") == [
            {"role": "user", "content": "又来了"},
            {"role": "assistant", "content": "[没理他]"},
        ]

    def test_other_tools_keep_the_generic_marker(self, db):
        save_turn(db, "u1", "g1", "搜一下", "",
                  tool_chain='[{"name": "web_search", "arguments": "{}"}]',
                  handled=True)
        assert load_history(db, "u1", "g1")[1]["content"] == "[已调用工具处理]"

    def test_the_tool_sends_nothing(self, db):
        """Being silent has to be an action, not the absence of one."""
        from ai_tools import IgnoreTool

        class AI:
            def __init__(self):
                self.ai_message = {"tool_calls": [{"id": "c1",
                                                   "function": {"name": "ignore_user"}}]}
                self.tool_result_text = ""
                self.user_text = ""

        class Robot:
            user_name, group_id = "小明", "g1"

        ai = AI()
        IgnoreTool(db, None).ignore_user_call(Robot(), ai)
        assert "一个字都不要回" in ai.tool_result_text

    def test_it_is_registered_as_self_contained(self):
        """Otherwise main_logic would ask the model for a follow-up reply."""
        import ast
        import os

        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "KirikoBot", "main.py",
        )
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                names = [t.id for t in node.targets if isinstance(t, ast.Name)]
                if "SELF_CONTAINED_TOOLS" in names:
                    assert "ignore_user" in {e.value for e in node.value.elts}
                    return
        raise AssertionError("SELF_CONTAINED_TOOLS not found")

    def test_the_feature_gate_knows_it(self):
        from feature_gate import FEATURE_KEYS, TOOL_FEATURE

        assert "ignore" in FEATURE_KEYS
        assert TOOL_FEATURE["ignore_user"] == "ignore"

    def test_the_schema_is_offered_to_the_model(self):
        from ai_tools_list import AiTools

        names = {t["function"]["name"] for t in AiTools().ai_tools()}
        assert "ignore_user" in names


class TestPatienceWindowIsConfigurable:
    def test_the_window_has_a_default(self):
        from config import Config

        assert Config.PATIENCE_WINDOW_MINUTES == 10
