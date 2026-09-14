"""Regression tests for "the bot answers my previous message as well".

Root cause: a turn whose reply came from a self-contained tool (web_search,
tarot, hitokoto…) left `ai_text` empty, and `save_turn` stored only the user
half. That lone row sat in the context as a question nobody had answered, so
the next request carried two consecutive user turns and the model answered
both — the stale one first.

On a live database 154 of 1435 user rows (about one in ten) were stored that
way, so this was not a rare edge case.
"""
from __future__ import annotations

import pytest

from chat_history import MAX_HISTORY, load_history, save_turn

TOOL_CHAIN = '[{"name": "web_search", "arguments": "{\\"query\\": \\"x\\"}"}]'


class TestSaveTurn:
    def test_a_normal_turn_stores_both_halves(self, db):
        save_turn(db, "u1", "g1", "问题", "回答")
        assert load_history(db, "u1", "g1") == [
            {"role": "user", "content": "问题"},
            {"role": "assistant", "content": "回答"},
        ]

    def test_a_turn_with_no_reply_at_all_is_not_stored(self, db):
        """Half a turn is worse than none: it poisons every later request."""
        save_turn(db, "u1", "g1", "问题", "")
        assert load_history(db, "u1", "g1") == []

    def test_whitespace_only_reply_counts_as_no_reply(self, db):
        save_turn(db, "u1", "g1", "问题", "   \n ")
        assert load_history(db, "u1", "g1") == []

    def test_a_tool_reply_closes_the_turn_even_without_text(self, db):
        """web_search sends its own message, so the turn IS answered."""
        save_turn(db, "u1", "g1", "Sgt Pepper 封面有谁", "",
                  reasoning="查一下", tool_chain=TOOL_CHAIN, handled=True)
        history = load_history(db, "u1", "g1")
        assert history == [
            {"role": "user", "content": "Sgt Pepper 封面有谁"},
            {"role": "assistant", "content": "[已调用工具处理]"},
        ]

    def test_the_tool_chain_is_kept_for_execution_replay(self, db):
        save_turn(db, "u1", "g1", "问题", "", tool_chain=TOOL_CHAIN, handled=True)
        row = db.fetch_data(
            "SELECT tool_calls, reasoning FROM history WHERE role='assistant'"
        )[0]
        assert "web_search" in row[0]
        assert row[1] == ""

    def test_reasoning_is_stored_with_the_reply(self, db):
        save_turn(db, "u1", "g1", "问题", "回答", reasoning="我在想")
        row = db.fetch_data("SELECT reasoning FROM history WHERE role='assistant'")[0]
        assert row[0] == "我在想"

    def test_scopes_are_separated(self, db):
        save_turn(db, "u1", "g1", "群里的话", "群里回答")
        save_turn(db, "u1", None, "私聊的话", "私聊回答")
        assert load_history(db, "u1", "g1")[0]["content"] == "群里的话"
        assert load_history(db, "u1", None)[0]["content"] == "私聊的话"


class TestLoadHistory:
    def test_never_returns_two_user_turns_in_a_row(self, db):
        """The invariant the whole module exists to protect."""
        db.deposit_chat_history("user", "u1", "g1", "第一条没人回答", "", "")
        db.deposit_chat_history("user", "u1", "g1", "第二条", "", "")
        db.deposit_chat_history("assistant", "u1", "g1", "回答第二条", "", "")

        history = load_history(db, "u1", "g1")
        roles = [h["role"] for h in history]
        assert roles == ["user", "assistant"]
        assert history[0]["content"] == "第二条", "the newest question wins"
        assert all(
            not (roles[i] == "user" and roles[i + 1] == "user")
            for i in range(len(roles) - 1)
        )

    def test_a_lone_trailing_orphan_yields_no_history(self, db):
        """The caller appends the current message after this, so a history
        ending on a user turn would put two questions back to back — the
        exact shape of the reported bug."""
        db.deposit_chat_history("user", "u1", "g1", "旧问题没人回答", "", "")
        assert load_history(db, "u1", "g1") == []

    def test_a_trailing_orphan_is_dropped_after_answered_turns(self, db):
        db.deposit_chat_history("user", "u1", "g1", "甲", "", "")
        db.deposit_chat_history("assistant", "u1", "g1", "答甲", "", "")
        db.deposit_chat_history("user", "u1", "g1", "没人回答的问题", "", "")
        assert load_history(db, "u1", "g1") == [
            {"role": "user", "content": "甲"},
            {"role": "assistant", "content": "答甲"},
        ]

    def test_several_orphans_collapse_and_the_tail_is_dropped(self, db):
        db.deposit_chat_history("user", "u1", "g1", "甲", "", "")
        db.deposit_chat_history("assistant", "u1", "g1", "答甲", "", "")
        for text in ("第一", "第二", "第三", "第四"):
            db.deposit_chat_history("user", "u1", "g1", text, "", "")
        assert load_history(db, "u1", "g1") == [
            {"role": "user", "content": "甲"},
            {"role": "assistant", "content": "答甲"},
        ]

    def test_history_never_ends_on_a_user_turn(self, db):
        """Whatever the stored rows look like, the invariant holds."""
        db.deposit_chat_history("user", "u1", "g1", "甲", "", "")
        db.deposit_chat_history("assistant", "u1", "g1", "答甲", "", "")
        db.deposit_chat_history("user", "u1", "g1", "乙", "", "")
        db.deposit_chat_history("user", "u1", "g1", "丙", "", "")
        history = load_history(db, "u1", "g1")
        assert history and history[-1]["role"] == "assistant"

    def test_an_orphan_between_answered_turns_leaves_one_user_each(self, db):
        db.deposit_chat_history("user", "u1", "g1", "甲", "", "")
        db.deposit_chat_history("assistant", "u1", "g1", "答甲", "", "")
        db.deposit_chat_history("user", "u1", "g1", "乙没人答", "", "")
        db.deposit_chat_history("user", "u1", "g1", "丙", "", "")
        db.deposit_chat_history("assistant", "u1", "g1", "答丙", "", "")
        assert load_history(db, "u1", "g1") == [
            {"role": "user", "content": "甲"},
            {"role": "assistant", "content": "答甲"},
            {"role": "user", "content": "丙"},
            {"role": "assistant", "content": "答丙"},
        ]

    def test_a_tool_assistant_row_becomes_a_marker(self, db):
        db.deposit_chat_history("user", "u1", "g1", "用工具", "", "")
        db.deposit_chat_history("assistant", "u1", "g1", "", TOOL_CHAIN, "")
        assert load_history(db, "u1", "g1")[1]["content"] == "[已调用工具处理]"

    def test_an_empty_assistant_row_is_skipped(self, db):
        """No text and no tools means there is nothing to remember."""
        db.deposit_chat_history("user", "u1", "g1", "甲", "", "")
        db.deposit_chat_history("assistant", "u1", "g1", "", "", "")
        db.deposit_chat_history("user", "u1", "g1", "乙", "", "")
        db.deposit_chat_history("assistant", "u1", "g1", "答乙", "", "")
        assert load_history(db, "u1", "g1") == [
            {"role": "user", "content": "乙"},
            {"role": "assistant", "content": "答乙"},
        ]

    def test_only_the_most_recent_turns_are_returned(self, db):
        for i in range(MAX_HISTORY + 6):
            save_turn(db, "u1", "g1", f"问题{i}", f"回答{i}")
        history = load_history(db, "u1", "g1")
        assert len(history) == MAX_HISTORY
        assert history[-1]["content"] == f"回答{MAX_HISTORY + 5}"

    def test_a_broken_database_yields_no_history(self, db, monkeypatch):
        monkeypatch.setattr(db, "takeout_chat_history",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db gone")))
        assert load_history(db, "u1", "g1") == []


class TestTheReportedScenario:
    """End to end: the exact sequence that produced the complaint."""

    def test_asking_after_a_web_search_turn_does_not_re_ask_it(self, db):
        # 1. The user asks something that the bot answers with web_search.
        #    Nothing was typed by the model, so before the fix this stored a
        #    lone user row.
        save_turn(db, "u1", "g1",
                  "Sgt. Pepper 这张专辑的专辑图里包含了哪些人", "",
                  tool_chain='[{"name": "web_search", "arguments": "{}"}]',
                  handled=True)

        # 2. Ten hours later they send a new, unrelated message.
        history = load_history(db, "u1", "g1")
        current = "不限方法，帮我赚够1亿美元"

        # The model must not see the old question as unanswered.
        assert [h["role"] for h in history] == ["user", "assistant"]
        assert history[0]["content"] != current
        assert history[1]["content"] == "[已调用工具处理]"

        # And the request carries exactly one question.
        pending = [h for h in history if h["role"] == "user"]
        assert len(pending) == 1

    def test_a_failed_turn_leaves_no_trace(self, db):
        """A model that returned nothing must not become a stale question."""
        save_turn(db, "u1", "g1", "第一个问题", "")          # e.g. empty completion
        save_turn(db, "u1", "g1", "第二个问题", "第二个回答")
        history = load_history(db, "u1", "g1")
        assert [h["content"] for h in history] == ["第二个问题", "第二个回答"]
