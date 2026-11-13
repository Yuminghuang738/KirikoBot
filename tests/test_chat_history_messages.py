"""chat_history.to_messages：清洗规则纯函数 + load_history 的 raw 窗口参数。"""
from __future__ import annotations

from chat_history import MAX_HISTORY, load_history, to_messages


def test_empty_rows():
    assert to_messages([]) == []


def test_consecutive_user_keeps_latest():
    rows = [
        ("user", "旧问题", "", ""),
        ("user", "新问题", "", ""),
        ("assistant", "回答", "", ""),
    ]
    messages = to_messages(rows)
    assert messages == [
        {"role": "user", "content": "新问题"},
        {"role": "assistant", "content": "回答"},
    ]


def test_empty_assistant_with_tool_calls_becomes_marker():
    rows = [("user", "问", "", ""), ("assistant", "", "[]", "call_1")]
    assert to_messages(rows)[-1] == {"role": "assistant", "content": "[已调用工具处理]"}


def test_empty_assistant_without_tool_calls_is_dropped():
    rows = [("user", "问", "", ""), ("assistant", "   ", "", "")]
    # assistant 空且无 tool_calls → 丢掉；随后这条 user 成了尾部孤儿，也一并丢掉。
    assert to_messages(rows) == []


def test_trailing_orphan_user_is_dropped():
    rows = [("user", "已答", "", ""), ("assistant", "答", "", ""), ("user", "没人回", "", "")]
    assert to_messages(rows)[-1]["role"] == "assistant"


def test_load_history_default_and_custom_limit(monkeypatch):
    calls = []

    class FakeDB:
        def takeout_chat_history(self, uid, gid, limit=None):
            calls.append(limit)
            return [("user", f"m{i}", "", "") for i in range(limit or 0)]

    load_history(FakeDB(), "u1", "g1")
    assert calls[-1] == MAX_HISTORY  # 3a 默认仍是 16，行为不变

    load_history(FakeDB(), "u1", "g1", raw_messages=24)
    assert calls[-1] == 24


def test_load_history_swallows_db_errors():
    class BrokenDB:
        def takeout_chat_history(self, uid, gid, limit=None):
            raise RuntimeError("db down")

    assert load_history(BrokenDB(), "u1", "g1") == []
