"""对话体验两处修复的回归测试。

1. 注意力：历史原来只有 4 轮（LIMIT 12 + tool 行占名额 + MAX_HISTORY=8 条消息）。
   实测线上有效记忆 = 4 轮，而 README 写的是 16 轮。
2. 人味：群聊回复中位 36 字、49% 句末带句号，真人中位 8 字、1% 带句号。
   语感章节 + 群聊末尾长度提示，是 A/B 里收益最大的改动。
"""
from __future__ import annotations

import re

import pytest


class TestHistoryDepth:
    def test_the_cap_is_eight_turns_not_four(self):
        from chat_history import MAX_HISTORY
        assert MAX_HISTORY == 16, "16 条消息 = 8 轮；8 条只有 4 轮，是实测的注意力瓶颈"

    def test_takeout_reaches_past_the_old_twelve_row_limit(self, db):
        for i in range(30):
            db.deposit_chat_history("user", "u1", "g1", f"问{i}", "", "")
            db.deposit_chat_history("assistant", "u1", "g1", f"答{i}", "", "")
        rows = db.takeout_chat_history("u1", "g1")
        assert len(rows) == db.CHAT_HISTORY_RAW_LIMIT
        assert len(rows) > 12, "旧的通用 takeout 写死 LIMIT 12，这里不能再被它限住"

    def test_tool_rows_do_not_eat_the_budget(self, db):
        """tool 行不进模型上下文，就不该占取数名额。"""
        for i in range(10):
            db.deposit_chat_history("user", "u1", "g1", f"问{i}", "", "")
            db.deposit_chat_history("tool", "u1", "g1", "工具结果" * 5, "", "call_1")
            db.deposit_chat_history("assistant", "u1", "g1", f"答{i}", "", "")
        roles = {r[0] for r in db.takeout_chat_history("u1", "g1")}
        assert roles <= {"user", "assistant"}

    def test_loaded_history_fills_sixteen_messages_when_available(self, db):
        from chat_history import load_history
        for i in range(12):
            db.deposit_chat_history("user", "u1", "g1", f"问{i}", "", "")
            db.deposit_chat_history("assistant", "u1", "g1", f"答{i}", "", "")
        history = load_history(db, "u1", "g1")
        assert len(history) == 16
        assert history[-1]["content"] == "答11", "最新的一轮必须在最后"

    def test_group_and_private_scopes_stay_separate(self, db):
        db.deposit_chat_history("user", "u1", "g1", "群里的话", "", "")
        db.deposit_chat_history("user", "u1", None, "私聊的话", "", "")
        assert [r[1] for r in db.takeout_chat_history("u1", "g1")] == ["群里的话"]
        assert [r[1] for r in db.takeout_chat_history("u1", None)] == ["私聊的话"]


class TestStyleSection:
    @pytest.fixture
    def section(self):
        from prompt_builder import PERSONA
        start = PERSONA.index("【说话的语感】")
        return PERSONA[start:PERSONA.index("【表情包】")]

    def test_it_exists_and_is_the_most_important_rule(self, section):
        assert "这一条最重要" in section
        assert "句末不加句号" in section

    def test_it_ships_real_short_exemplars(self, section):
        examples = re.findall(r"^\s+· (.+)$", section, re.M)
        assert len(examples) == 8

    def test_exemplars_are_clean_and_short(self, section):
        examples = re.findall(r"^\s+· (.+)$", section, re.M)
        for ex in examples:
            assert len(ex) <= 20, f"示例太长，不像群里随口一句：{ex}"
            assert not re.search(r"[@\[\]]|http", ex), f"示例混入了平台标记：{ex}"
            assert not ex.endswith("。"), f"示例不该带句号：{ex}"

    def test_it_is_covered_by_the_leak_guard(self):
        from prompt_builder import LEAK_MARKERS
        assert "【说话的语感】" in LEAK_MARKERS

    def test_the_persona_stays_under_its_cost_cap(self):
        from prompt_builder import PERSONA
        assert len(PERSONA) < 4800


class TestGroupLengthHint:
    class _Robot:
        def __init__(self, msg_type: str):
            self.msg_type = msg_type
            self.group_id = "g1" if msg_type == "group" else None
            self.group_name = "测试群"
            self.user_id = "u1"
            self.user_name = "小明"
            self.msg = "在吗"
            self.incoming = type("I", (), {"has_images": False})()

    def test_group_messages_end_with_the_short_reply_reminder(self):
        from prompt_builder import build_user_message
        text = build_user_message(self._Robot("group"))
        assert text.rstrip().endswith("句末不加句号。）")
        assert "十几个字" in text

    def test_private_messages_get_no_extra_hint(self):
        from prompt_builder import build_user_message
        text = build_user_message(self._Robot("private"))
        assert "十几个字" not in text
