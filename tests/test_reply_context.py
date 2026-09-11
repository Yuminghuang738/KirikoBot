"""Quote-aware context: parsing the reply segment and describing it.

LLBot embeds the quoted message in the event, so the whole feature hinges on
parsing it correctly and never letting the quoted text leak into the message
body (which would make the bot answer its own words as if the user said them).
"""
from __future__ import annotations

import sqlite3

import pytest

from llbot_client import IncomingMessage, LLBotClient
from prompt_builder import build_user_message, describe_reply

QUOTED_TEXT = "今天天气不错哦"
REPLY_SEGMENT = {
    "type": "reply",
    "data": {
        "message_seq": 4242,
        "sender_id": 10000,
        "sender_name": "Kiriko",
        "time": 1789000000,
        "segments": [{"type": "text", "data": {"text": QUOTED_TEXT}}],
    },
}


def make_event(message=None, **overrides):
    event = {
        "message_type": "group",
        "user_id": "20000",
        "group_id": "g1",
        "message_id": 5000,
        "sender": {"nickname": "小明"},
        "message": message if message is not None else [
            REPLY_SEGMENT,
            {"type": "at", "data": {"qq": "10000"}},
            {"type": "text", "data": {"text": "那这个呢"}},
        ],
    }
    event.update(overrides)
    return event


class TestReplyParsing:
    def test_extracts_the_quoted_message(self):
        msg = IncomingMessage.from_onebot(make_event(), "10000")
        assert msg.reply is not None
        assert msg.reply.message_seq == 4242
        assert msg.reply.sender_name == "Kiriko"
        assert msg.reply.text == QUOTED_TEXT

    def test_quoted_text_never_leaks_into_the_message_body(self):
        msg = IncomingMessage.from_onebot(make_event(), "10000")
        assert msg.text == "那这个呢"
        assert QUOTED_TEXT not in msg.text

    def test_no_reply_segment_means_no_reply(self):
        msg = IncomingMessage.from_onebot(
            make_event([{"type": "text", "data": {"text": "在吗"}}]), "10000"
        )
        assert msg.reply is None

    def test_image_only_quote_is_flagged(self):
        seg = {
            "type": "reply",
            "data": {"message_seq": 1, "sender_name": "小王",
                     "segments": [{"type": "image", "data": {"url": "http://x"}}]},
        }
        msg = IncomingMessage.from_onebot(make_event([seg]), "10000")
        assert msg.reply.has_images is True
        assert msg.reply.text == ""

    @pytest.mark.parametrize("bad", [None, "abc", ""])
    def test_bad_message_seq_is_tolerated(self, bad):
        seg = {"type": "reply", "data": {"message_seq": bad, "sender_name": "x",
                                        "segments": [{"type": "text", "data": {"text": "hi"}}]}}
        msg = IncomingMessage.from_onebot(make_event([seg]), "10000")
        assert msg.reply is not None
        assert msg.reply.message_seq is None

    def test_tolerates_alternative_field_names(self):
        """Some LLBot code paths use id/qq instead of message_seq/sender_id."""
        seg = {"type": "reply", "data": {"id": "77", "qq": "10000", "text": "旧格式"}}
        msg = IncomingMessage.from_onebot(make_event([seg]), "10000")
        assert msg.reply.message_seq == 77
        assert msg.reply.sender_id == "10000"
        assert msg.reply.text == "旧格式"


class TestOwnMessageDetection:
    def _client(self):
        return LLBotClient("http://localhost:3000", "t")

    def test_matches_by_message_id(self):
        c = self._client()
        c._recent_sent.append({"message_id": 4242, "text": "别的", "group_id": "g1",
                               "user_id": "", "ts": 0})
        assert c.is_own_message(4242) is True

    def test_matches_by_text_when_ids_differ(self):
        """LLBot reports the reply sender as a UID in some paths, so fall back to text."""
        c = self._client()
        c._recent_sent.append({"message_id": 999, "text": QUOTED_TEXT, "group_id": "g1",
                               "user_id": "", "ts": 0})
        assert c.is_own_message(4242, QUOTED_TEXT) is True

    def test_does_not_match_foreign_messages(self):
        c = self._client()
        c._recent_sent.append({"message_id": 1, "text": "我说过的话", "group_id": "g1",
                               "user_id": "", "ts": 0})
        assert c.is_own_message(4242, "别人说的话") is False

    def test_substring_is_not_a_match(self):
        """Loose substring matching used to claim other people's quotes as ours."""
        c = self._client()
        c._recent_sent.append({"message_id": 1, "text": "今天天气不错哦", "group_id": "g1",
                               "user_id": "", "ts": 0})
        assert c.is_own_message(4242, "今天天气不错") is False
        assert c.is_own_message(4242, "天气") is False

    def test_short_replies_are_not_matched_by_text(self):
        """'好的' is not distinctive; only the id may match it."""
        c = self._client()
        c._recent_sent.append({"message_id": 1, "text": "好的", "group_id": "g1",
                               "user_id": "", "ts": 0})
        assert c.is_own_message(999, "好的") is False
        assert c.is_own_message(1, "好的") is True

    def test_whitespace_differences_still_match(self):
        c = self._client()
        c._recent_sent.append({"message_id": 1, "text": " 今天  天气不错哦 ", "group_id": "g1",
                               "user_id": "", "ts": 0})
        assert c.is_own_message(None, "今天 天气不错哦") is True

    def test_empty_history_is_safe(self):
        assert self._client().is_own_message(None, None) is False


class TestDescribeReply:
    class _Reply:
        sender_name = "小王"
        has_images = False

        def __init__(self, text):
            self.text = text

    def test_own_message_is_called_out_explicitly(self):
        note = describe_reply(self._Reply(QUOTED_TEXT), is_own=True)
        assert "你自己" in note
        assert QUOTED_TEXT in note

    def test_other_members_are_named(self):
        note = describe_reply(self._Reply("晚上吃啥"), is_own=False)
        assert "小王" in note and "晚上吃啥" in note

    def test_long_quotes_are_truncated(self):
        note = describe_reply(self._Reply("字" * 500), is_own=False)
        assert len(note) < 400

    def test_image_only_quote_has_a_placeholder(self):
        r = self._Reply("")
        r.has_images = True
        assert "图片" in describe_reply(r, is_own=False)

    def test_none_reply_produces_nothing(self):
        assert describe_reply(None, is_own=False) == ""


class TestUserMessageWithQuote:
    class _Incoming:
        has_images = False
        reply = None

    class _Robot:
        msg_type = "group"
        msg = "那这个呢"
        user_name = "小明"
        group_name = "测试群"
        incoming = None

    def _robot(self):
        r = self._Robot()
        r.incoming = self._Incoming()
        return r

    def test_note_is_placed_next_to_the_message(self):
        text = build_user_message(self._robot(), "【引用回复】什么什么")
        assert text.index("【引用回复】") < text.index("那这个呢")

    def test_without_note_output_is_unchanged(self):
        text = build_user_message(self._robot())
        assert text.startswith("群「测试群」中")
        assert "引用回复" not in text


class TestMessageIdStorage:
    def test_records_message_id_and_quote_link(self, db):
        db.record_group_message("g1", "u1", "小明", "那这个呢",
                                message_id=5000, message_seq=4242, reply_to_seq=4242)
        row = db.fetch_data(
            "SELECT message_id, message_seq, reply_to_seq "
            "FROM group_messages WHERE group_id='g1'"
        )[0]
        assert row == (5000, 4242, 4242)

    def test_message_seq_and_short_id_are_separate_columns(self, db):
        """A reply segment references message_seq, not LLBot's short message_id."""
        db.record_group_message("g1", "u1", "小明", "那这个呢",
                                message_id=987654, message_seq=111)
        row = db.fetch_data(
            "SELECT message_id, message_seq FROM group_messages WHERE group_id='g1'"
        )[0]
        assert row == (987654, 111)

    def test_columns_are_optional(self, db):
        db.record_group_message("g1", "u1", "小明", "普通消息")
        row = db.fetch_data(
            "SELECT message_id, message_seq, reply_to_seq "
            "FROM group_messages WHERE group_id='g1'"
        )[0]
        assert row == (None, None, None)

    def test_migration_adds_columns_to_an_existing_db(self, tmp_path):
        """An older robot.db has none of the linkage columns."""
        from database_manager import DatabaseManager

        path = str(tmp_path / "legacy.db")
        conn = sqlite3.connect(path)
        conn.execute(
            """CREATE TABLE group_messages(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT NOT NULL, user_id TEXT NOT NULL, user_name TEXT NOT NULL,
                user_role TEXT DEFAULT '', content TEXT NOT NULL,
                msg_type TEXT DEFAULT 'text',
                timestamp DATETIME DEFAULT (datetime('now','localtime'))
            )"""
        )
        conn.execute(
            "INSERT INTO group_messages (group_id,user_id,user_name,content) VALUES ('g1','u1','小明','旧消息')"
        )
        conn.commit()
        conn.close()

        DatabaseManager(path)  # runs the migration

        conn = sqlite3.connect(path)
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(group_messages)")}
            assert {"message_id", "message_seq", "reply_to_seq"} <= cols
            assert conn.execute("SELECT COUNT(*) FROM group_messages").fetchone()[0] == 1
        finally:
            conn.close()
