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

        def send(self, message):
            """镜像 RobotServer.send：按 msg_type 走群或私聊。"""
            if self.msg_type == "group":
                return self.llbot.send_group_msg(self.group_id, message)
            return self.llbot.send_private_msg(self.user_id, message)

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

    class _Capture:
        """记录每一次 send，供断言「只发了一条」「内容是什么」。"""

        def __init__(self):
            self.sent: list[str] = []

        def send_group_msg(self, gid, msg):
            self.sent.append(json.dumps(msg, ensure_ascii=False))
            return True

        def send_private_msg(self, uid, msg):
            self.sent.append(json.dumps(msg, ensure_ascii=False))
            return True

    def _robot(self, cap):
        robot = self._Robot()
        robot.llbot = cap
        return robot

    def test_a_repeat_draw_sends_exactly_one_message_and_no_ai_call(self, db):
        """重抽：只发同一张牌一次，不再请 AI 说第二段话（之前会发两条）。"""
        from ai_tools import Tarot

        seed_card(db)
        db.deposit_tarot_history("u1", "愚者_正位")
        cap = self._Capture()
        ai = self._AI()
        calls = []
        ai.ai_request = lambda: calls.append(1)
        Tarot(db, None)._resend_today(self._robot(cap), ai, db.get_today_tarot("u1"), "小明", True)

        assert len(cap.sent) == 1, "只能发一条消息"
        assert calls == [], "不应再调用 AI"
        assert "一天只能抽一张" in cap.sent[0]
        assert "抽到什么就是什么" in cap.sent[0]
        assert "愚者_正位" in cap.sent[0]

    def test_a_first_draw_sends_exactly_one_message_and_no_ai_call(self, db):
        """首抽：牌面 + 简短解释，只发这一条；不再请 AI 写解读。"""
        from ai_tools import Tarot

        seed_card(db, "愚者_正位", "新的开始、天真、自由", "/cards/fool.png")
        cap = self._Capture()
        ai = self._AI()
        calls = []
        ai.ai_request = lambda: calls.append(1)
        Tarot(db, None).tarot_call(self._robot(cap), ai)

        assert len(cap.sent) == 1, "只能发一条消息"
        assert calls == [], "不应再调用 AI"
        assert "愚者_正位" in cap.sent[0]
        assert "新的开始、天真、自由" in cap.sent[0], "简短解释要在第一条里"

    def test_the_handled_summary_carries_the_card_into_history(self, db):
        """历史里要留下「抽到了什么」，否则之后的对话不知道刚才发了哪张牌。"""
        from ai_tools import Tarot

        seed_card(db, "愚者_正位", "新的开始、天真、自由")
        ai = self._AI()
        Tarot(db, None).tarot_call(self._robot(self._Capture()), ai)
        assert "愚者_正位" in ai.handled_summary
        assert "新的开始" in ai.handled_summary

    def test_the_tool_no_longer_writes_chat_history_itself(self, db):
        """主流程已经保存这一轮；塔罗再写一遍会出现连续两条 user 行。"""
        from ai_tools import Tarot

        seed_card(db)
        Tarot(db, None).tarot_call(self._robot(self._Capture()), self._AI())
        assert db.takeout_chat_history("u1", "g1") == [], "工具不该自己写历史"

    def test_the_history_keeps_the_card_not_an_empty_marker(self, db):
        """端到端：自回工具的这一轮，历史里是摘要，不是「[已调用工具处理]」。"""
        from chat_history import load_history, save_turn
        from ai_tools import Tarot

        seed_card(db, "愚者_正位", "新的开始、天真、自由")
        ai = self._AI()
        Tarot(db, None).tarot_call(self._robot(self._Capture()), ai)
        save_turn(db, "u1", "g1", "帮我抽张塔罗", ai.handled_summary, "", "", True)

        history = load_history(db, "u1", "g1")
        assistant = [h["content"] for h in history if h["role"] == "assistant"]
        assert assistant and "愚者_正位" in assistant[-1]
        assert "[已调用工具处理]" not in assistant[-1]

    def test_the_card_stands_whether_good_or_bad(self):
        """The rule is about the card, not about the outcome."""
        from ai_tools import Tarot

        source = Tarot._resend_today.__code__.co_consts
        text = " ".join(str(c) for c in source if isinstance(c, str))
        assert "好是坏" in text
