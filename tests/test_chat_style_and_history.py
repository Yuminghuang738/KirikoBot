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
    def test_the_cap_is_twelve_turns(self):
        from chat_history import MAX_HISTORY
        assert MAX_HISTORY == 24, "24 条消息 = 12 轮；阶段 3 从 16 条（8 轮）拉上来"

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

    def test_loaded_history_fills_twenty_four_messages_when_available(self, db):
        from chat_history import load_history
        for i in range(20):
            db.deposit_chat_history("user", "u1", "g1", f"问{i}", "", "")
            db.deposit_chat_history("assistant", "u1", "g1", f"答{i}", "", "")
        history = load_history(db, "u1", "g1")
        assert len(history) == 24
        assert history[-1]["content"] == "答19", "最新的一轮必须在最后"

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

    # 人设长度只在 tests/test_persona_integrity.py 卡一处（4,800→7,500 时这里
    # 漏改过），避免两个上限各自漂移。


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


class TestContinuousFollowUpAnchor:
    """短追问要锚定上一轮讨论：`main.py` 必须把 history 传进 build_user_message。

    这个参数曾经漏传：`history, memory_summary, memory_on = _memory_context(robot)`
    已经算出来了，但调 `_context(...)` 时没传 history，于是 prompt_builder 里
    `is_continuous` 永远是 False、`context_focus` 永远是空串 —— 一段没人覆盖的
    死分支。这里同时钉住 builder 的行为和 main.py 的接线。
    """

    _Robot = TestGroupLengthHint._Robot

    @staticmethod
    def _robot(msg_type: str, msg: str = "那在C里呢"):
        robot = TestContinuousFollowUpAnchor._Robot(msg_type)
        robot.msg = msg
        return robot

    def test_group_short_follow_up_is_anchored(self):
        from prompt_builder import build_user_message
        text = build_user_message(
            self._robot("group"), history=[{"role": "user", "content": "指针是啥"}]
        )
        assert "这是对刚才同一话题的连续短追问" in text

    def test_private_short_follow_up_is_anchored(self):
        from prompt_builder import build_user_message
        text = build_user_message(
            self._robot("private"), history=[{"role": "user", "content": "指针是啥"}]
        )
        assert "这是对刚才同一话题的连续短追问" in text

    def test_short_message_without_history_is_not_anchored(self):
        from prompt_builder import build_user_message
        assert "连续短追问" not in build_user_message(self._robot("group"), history=[])
        assert "连续短追问" not in build_user_message(self._robot("private"), history=None)

    def test_long_message_with_history_is_not_anchored(self):
        from prompt_builder import build_user_message
        robot = self._robot("group", "帮我看看这个报错到底是怎么回事")
        text = build_user_message(robot, history=[{"role": "user", "content": "指针是啥"}])
        assert "连续短追问" not in text


def test_main_logic_passes_history_to_user_message():
    """接线回归：漏传 history 时上面的锚定就是死代码，而单测照样全绿。"""
    import pathlib

    source = pathlib.Path(__file__).resolve().parents[1] / "KirikoBot" / "main.py"
    text = source.read_text(encoding="utf-8")
    head = text.index("user_text = _context(")
    tail = text.index("system_prompt = _build_system_prompt", head)
    assert "history=history" in text[head:tail], (
        "main.py 调 _context 时必须传 history，否则连续短追问锚定是死分支"
    )
