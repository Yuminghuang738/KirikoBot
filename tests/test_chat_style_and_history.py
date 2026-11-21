"""对话体验两处修复的回归测试。

1. 注意力：历史原来只有 4 轮（LIMIT 12 + tool 行占名额 + MAX_HISTORY=8 条消息）。
   实测线上有效记忆 = 4 轮，而 README 写的是 16 轮。
2. 人味：群聊回复中位 36 字、49% 句末带句号，真人中位 8 字、1% 带句号。
   语感章节 + 群聊末尾长度提示，是 A/B 里收益最大的改动。
"""
from __future__ import annotations

import re
from datetime import datetime

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
    """短追问锚定必须看**真实时间**：history 非空不等于「刚才」。

    线上事故：用户隔 7 天发「我去你怎么这么高级」，上一轮是「硬水」的话题；
    `len(history) > 0` 让提示谎称「刚才同一话题」，模型顺着硬水答，思维链也
    跟着跑偏。这里钉住时间判断、隔了多久的事实说明，以及 main.py 的接线。
    """

    _Robot = TestGroupLengthHint._Robot
    NOW = datetime(2026, 11, 21, 16, 15)

    @staticmethod
    def _robot(msg_type: str, msg: str = "那在C里呢"):
        robot = TestContinuousFollowUpAnchor._Robot(msg_type)
        robot.msg = msg
        return robot

    @staticmethod
    def _history(label: str):
        # 时间戳由 chat_history.to_messages 加在 user 行上，assistant 行没有
        return [
            {"role": "user", "content": f"[{label}] 指针是啥"},
            {"role": "assistant", "content": "指针是地址"},
        ]

    def _text(self, msg_type="group", msg="那在C里呢", label="11-21 16:13", now=None):
        from prompt_builder import build_user_message
        return build_user_message(
            self._robot(msg_type, msg), history=self._history(label),
            now=now or self.NOW,
        )

    def test_recent_group_follow_up_is_anchored(self):
        text = self._text()
        assert "这是对刚才同一话题的连续短追问" in text
        assert "隔了" not in text

    def test_recent_private_follow_up_is_anchored(self):
        text = self._text("private")
        assert "这是对刚才同一话题的连续短追问" in text

    def test_seven_day_old_history_is_not_anchored_but_says_how_old(self):
        text = self._text(label="11-14 20:04")
        assert "连续短追问" not in text, "7 天前不可能是「刚才」"
        assert "上一轮对话是 11-14 20:04" in text
        assert "隔了 7 天" in text
        assert "别把那时候的事当成刚才发生的" in text

    def test_private_old_history_still_gets_the_gap_note(self):
        text = self._text("private", label="11-14 20:04")
        assert "连续短追问" not in text
        assert "上一轮对话是 11-14 20:04" in text

    def test_history_without_timestamps_is_not_anchored(self):
        """4 元组路径（eval 回放）没有时间戳：宁可少锚定，也不能瞎说刚才。"""
        from prompt_builder import build_user_message
        history = [{"role": "user", "content": "指针是啥"},
                   {"role": "assistant", "content": "指针是地址"}]
        text = build_user_message(self._robot("group"), history=history, now=self.NOW)
        assert "连续短追问" not in text
        assert "隔了" not in text

    def test_short_message_without_history_is_not_anchored(self):
        from prompt_builder import build_user_message
        assert "连续短追问" not in build_user_message(
            self._robot("group"), history=[], now=self.NOW)
        assert "连续短追问" not in build_user_message(
            self._robot("private"), history=None, now=self.NOW)

    def test_long_message_with_recent_history_is_not_anchored(self):
        text = self._text(msg="帮我看看这个报错到底是怎么回事")
        assert "连续短追问" not in text

    def test_the_hour_boundary(self):
        # 59 分钟前还算刚才；61 分钟前既不算刚才，也还不到 6 小时的隔天说明
        assert "连续短追问" in self._text(label="11-21 15:16")
        stale = self._text(label="11-21 15:14")
        assert "连续短追问" not in stale
        assert "隔了" not in stale

    def test_six_hours_gets_the_gap_note(self):
        text = self._text(label="11-21 10:15")
        assert "连续短追问" not in text
        assert "隔了 6 小时" in text

    def test_cross_year_history_is_not_treated_as_future(self):
        """12-31 的记录在 1 月要算上一年，不能被当成「未来」而锚定。"""
        from datetime import datetime as dt
        now = dt(2027, 1, 1, 10, 0)
        text = self._text(label="12-31 22:00", now=now)
        assert "连续短追问" not in text
        assert "上一轮对话是 12-31 22:00" in text
        assert "隔了 12 小时" in text


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
