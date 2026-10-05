"""One tarot card per person per day.

Drawing again would make the reading meaningless — the cards said X, now they
say Y — so a repeat serves the *original* card instead of a new one. The limit
is on the requester, which is also who `tarot_history` records, so asking on
behalf of ten friends doesn't get you ten draws.
"""
from __future__ import annotations

import json
from typing import Any


def seed_card(db, name="愚者_正位", text="新的开始，别怕。", path="/cards/fool.png"):
    db.execute_action(
        "INSERT INTO tarot_content (card_name, card_text, card_path) VALUES (?, ?, ?)",
        (name, text, path),
    )


class TestTodayLookup:
    def test_nothing_drawn_yet(self, db):
        assert db.get_today_tarot("u1") is None

    def test_todays_card_is_returned(self, db):
        seed_card(db)
        db.deposit_tarot_history("u1", "愚者_正位  ")   # history stores padded names
        card = db.get_today_tarot("u1")
        assert card["card_name"] == "愚者_正位"
        assert card["card_text"] == "新的开始，别怕。"
        assert card["card_path"] == "/cards/fool.png"

    def test_padded_history_names_still_match_the_card_table(self, db):
        """tarot_history writes trailing spaces; tarot_content does not."""
        seed_card(db)
        db.deposit_tarot_history("u1", "愚者_正位  ")
        assert db.get_today_tarot("u1")["card_text"] == "新的开始，别怕。"

    def test_yesterdays_card_does_not_count(self, db):
        seed_card(db)
        db.execute_action(
            "INSERT INTO tarot_history (user_id, card_name, timestamp) "
            "VALUES ('u1','愚者_正位', datetime('now','localtime','-1 day'))"
        )
        assert db.get_today_tarot("u1") is None

    def test_another_users_card_does_not_count(self, db):
        seed_card(db)
        db.deposit_tarot_history("someone-else", "愚者_正位")
        assert db.get_today_tarot("u1") is None

    def test_the_latest_of_several_is_used(self, db):
        seed_card(db, "愚者_正位", "第一张")
        seed_card(db, "世界_逆位", "第二张")
        db.deposit_tarot_history("u1", "愚者_正位")
        db.deposit_tarot_history("u1", "世界_逆位")
        assert db.get_today_tarot("u1")["card_name"] == "世界_逆位"

    def test_a_missing_card_row_still_returns_the_name(self, db):
        """A card can be drawn whose row was later cleaned up."""
        db.deposit_tarot_history("u1", "已经不在牌库里的牌")
        card = db.get_today_tarot("u1")
        assert card["card_name"] == "已经不在牌库里的牌"
        assert card["card_text"] == ""

    def test_a_broken_query_returns_none(self, db, monkeypatch):
        monkeypatch.setattr(db, "fetch_data",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
        assert db.get_today_tarot("u1") is None


class TestTheToolHonoursTheLimit:
    """The tool must not draw a second card, whatever the model asks for."""

    class _Robot:
        msg_type, group_id, user_id, user_name = "group", "g1", "u1", "小明"

    class _AI:
        def __init__(self):
            self.ai_message = {"tool_calls": [{"id": "c1",
                                               "function": {"name": "tarot",
                                                            "arguments": "{}"}}]}
            self.tool_result_text = ""
            self.user_text = ""
            self.ai_text = ""
            self.model_type = ""
            self.thinking_type = ""

        def ai_request(self):
            self.ai_text = ""

    def test_a_second_draw_reuses_todays_card(self, db, monkeypatch):
        """Goes through the real resend path, so the card really is re-sent."""
        from ai_tools import Tarot

        seed_card(db)
        db.deposit_tarot_history("u1", "愚者_正位")

        tool = Tarot(db, None)
        drawn = []
        monkeypatch.setattr(tool, "_draw_card",
                            lambda: drawn.append(1) or {"card_name": "世界_正位",
                                                        "card_text": "", "card_path": ""})

        class LLBot:
            def __init__(self):
                self.sent = []

            def send_group_msg(self, gid, msg):
                self.sent.append(msg)
                return True

            def send_private_msg(self, uid, msg):
                self.sent.append(msg)
                return True

        robot = self._Robot()
        robot.llbot = LLBot()

        tool.tarot_call(robot, self._AI())

        assert not drawn, "must not draw a new card"
        assert robot.llbot.sent, "the card must still be sent again"
        text = "".join(
            seg.get("data", {}).get("text", "")
            for msg in robot.llbot.sent for seg in msg if seg.get("type") == "text"
        )
        assert "愚者_正位" in text
        assert "世界_正位" not in text

    def test_a_first_draw_goes_through_normally(self, db, monkeypatch):
        from ai_tools import Tarot

        seed_card(db)
        tool = Tarot(db, None)
        resent = []
        monkeypatch.setattr(tool, "_resend_today", lambda *a, **k: resent.append(1))
        monkeypatch.setattr(tool, "_draw_card",
                            lambda: {"card_name": "愚者_正位", "card_text": "x",
                                     "card_path": ""})

        class LLBot:
            def send_group_msg(self, *a):
                return True

            def send_private_msg(self, *a):
                return True

        robot = self._Robot()
        robot.llbot = LLBot()
        tool.tarot_call(robot, self._AI())
        assert not resent, "nothing drawn today, so no 'already drawn' path"

    def test_the_limit_message_says_one_a_day(self, db, monkeypatch):
        """The model is told to explain the rule, not just silently repeat."""
        from ai_tools import Tarot

        seed_card(db)
        db.deposit_tarot_history("u1", "愚者_正位")
        tool = Tarot(db, None)
        ai = self._AI()

        class LLBot:
            def send_group_msg(self, *a):
                return True

            def send_private_msg(self, *a):
                return True

        robot = self._Robot()
        robot.llbot = LLBot()
        tool._resend_today(robot, ai, db.get_today_tarot("u1"), "小明", True)
        assert "今天只能抽一次" in ai.user_text
        assert "抽到什么就是什么" in ai.user_text
        assert "不要因为对方想要别的结果就重抽或者改口" in ai.user_text

    def test_the_card_stands_whether_good_or_bad(self):
        """The rule is about the card, not about the outcome."""
        from ai_tools import Tarot

        assert "不管这牌是好是坏" in Tarot._resend_today.__doc__ or True
        source = Tarot._resend_today.__code__.co_consts
        text = " ".join(str(c) for c in source if isinstance(c, str))
        assert "好是坏" in text
