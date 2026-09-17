"""Quote awareness and ambient group context.

Two related failures are covered here.

**Quote awareness never fired.** LLBot's reply segment is only
`{"type": "reply", "data": {"id": "75563830"}}` — no text, no sender — but the
code assumed the quoted content arrived inline, so `_reply_note` returned ""
for every single quote. Quoting the bot's own reply (the case the feature
exists for) did nothing at all. The id now resolves against bot_messages /
group_messages.

**Ambient context was left to the model.** Reading the room was offered only
as the `read_context` tool; it was called 12 times against 1000+ for other
tools, so replies kept answering the wrong thing. A short transcript is now
attached to every group message by default.
"""
from __future__ import annotations

from dataclasses import dataclass

from prompt_builder import (format_group_context, resolve_quote)


@dataclass
class _Reply:
    """A reply segment as LLBot actually sends it."""

    message_seq: int | None = None
    sender_id: str = ""
    sender_name: str = ""
    text: str = ""
    has_images: bool = False


class TestLLBotSendsOnlyAnId:
    """The regression: an id-only segment carries nothing to describe."""

    def test_an_id_only_segment_yields_nothing_without_a_lookup(self):
        assert resolve_quote(_Reply(message_seq=75563830), is_own=False) == ""

    def test_the_lookup_supplies_the_text(self):
        note = resolve_quote(
            _Reply(message_seq=75563830), is_own=False,
            lookup=lambda mid: {"text": "数字音响啊…啥症状？", "user_name": "", "is_own": True},
        )
        assert "数字音响啊" in note

    def test_quoting_the_bot_is_recognised_as_own(self):
        """The whole point: another user quotes a reply the bot gave someone."""
        note = resolve_quote(
            _Reply(message_seq=352360897), is_own=False,
            lookup=lambda mid: {"text": "那你去跟豆包聊啊", "user_name": "", "is_own": True},
        )
        assert "你自己" in note
        assert "不要当成新话题" in note

    def test_quoting_another_member_names_them(self):
        note = resolve_quote(
            _Reply(message_seq=-77706900), is_own=False,
            lookup=lambda mid: {"text": "你没比豆包强哪里去啊", "user_name": "Anonymous",
                                "is_own": False},
        )
        assert "Anonymous" in note
        assert "你自己" not in note

    def test_an_unknown_id_still_yields_nothing(self):
        assert resolve_quote(_Reply(message_seq=1), is_own=False,
                             lookup=lambda mid: None) == ""

    def test_a_broken_lookup_does_not_raise(self):
        def boom(mid):
            raise RuntimeError("db gone")

        assert resolve_quote(_Reply(message_seq=1), is_own=False, lookup=boom) == ""

    def test_inline_content_still_works(self):
        """Some LLBot builds do populate the segment; don't regress that."""
        note = resolve_quote(_Reply(text="晚上吃啥", sender_name="小明"), is_own=False)
        assert "晚上吃啥" in note and "小明" in note

    def test_inline_content_skips_the_lookup(self):
        called = []
        resolve_quote(_Reply(text="晚上吃啥", sender_name="小明"), is_own=False,
                      lookup=lambda mid: called.append(mid))
        assert not called

    def test_an_image_quote_is_described(self):
        assert "图片" in resolve_quote(_Reply(has_images=True), is_own=False)

    def test_no_reply_segment_at_all(self):
        assert resolve_quote(None, is_own=False) == ""


class TestFindQuoted:
    def test_resolves_a_message_the_bot_sent(self, db):
        db.record_bot_message("g1", 352360897, "那你去跟豆包聊啊")
        found = db.find_quoted("g1", 352360897)
        assert found["is_own"] is True
        assert found["text"] == "那你去跟豆包聊啊"

    def test_resolves_another_members_message(self, db):
        db.record_group_message("g1", "u2", "Anonymous", "你没比豆包强哪里去啊",
                                message_id=-77706900)
        found = db.find_quoted("g1", -77706900)
        assert found["is_own"] is False
        assert found["user_name"] == "Anonymous"

    def test_an_unknown_id_returns_none(self, db):
        assert db.find_quoted("g1", 999999) is None

    def test_a_nonsense_id_returns_none(self, db):
        assert db.find_quoted("g1", None) is None
        assert db.find_quoted("g1", "not-a-number") is None

    def test_lookups_are_scoped_to_the_group(self, db):
        db.record_bot_message("g1", 123, "给 g1 的")
        assert db.find_quoted("g2", 123) is None
        assert db.find_quoted("g1", 123) is not None

    def test_the_bots_own_message_wins_over_a_same_id_member_message(self, db):
        """Ids collide across stores; the bot's line is what matters here."""
        db.record_bot_message("g1", 555, "机器人说的")
        db.record_group_message("g1", "u1", "某人", "别人说的", message_id=555)
        found = db.find_quoted("g1", 555)
        assert found["is_own"] is True

    def test_recalled_bot_messages_are_still_resolvable_for_context(self, db):
        """A quote of a recalled line still explains what was being answered."""
        db.record_bot_message("g1", 777, "被撤回的话")
        db.mark_bot_message_recalled(777)
        assert db.find_quoted("g1", 777)["text"] == "被撤回的话"


class TestFormatGroupContext:
    ROWS = [
        {"user_name": "小明", "content": "这把琴多少钱", "is_bot": False},
        {"user_name": "Kiriko", "content": "两千左右", "is_bot": True},
        {"user_name": "小红", "content": "那这个呢", "is_bot": False},
    ]

    def test_renders_every_line(self):
        text = format_group_context(self.ROWS, minutes=15)
        assert "小明: 这把琴多少钱" in text
        assert "两千左右" in text
        assert "那这个呢" in text

    def test_the_bots_own_lines_are_labelled(self):
        assert "你(Kiriko): 两千左右" in format_group_context(self.ROWS)

    def test_says_how_far_back_it_goes(self):
        assert "最近 15 分钟" in format_group_context(self.ROWS, minutes=15)

    def test_marks_the_background_as_not_addressed_to_the_bot(self):
        text = format_group_context(self.ROWS)
        assert "不是发给你的" in text
        assert "下面才是需要你回应的消息" in text

    def test_empty_rows_produce_nothing(self):
        assert format_group_context([]) == ""
        assert format_group_context(None) == ""

    def test_blank_lines_are_skipped(self):
        assert format_group_context([{"user_name": "x", "content": "   "}]) == ""

    def test_long_lines_are_trimmed(self):
        """Every line costs tokens on every message, so keep them short."""
        text = format_group_context([{"user_name": "x", "content": "字" * 500}])
        assert "…" in text
        assert text.count("字") < 500

    def test_newlines_inside_a_message_are_collapsed(self):
        """One message per line, or the transcript becomes unreadable."""
        text = format_group_context([{"user_name": "x", "content": "第一行\n第二行"}])
        assert "第一行 第二行" in text


class TestAmbientContextInUserMessage:
    class _Robot:
        msg_type, msg = "group", "那这个呢"
        group_name, user_name = "测试群", "小红"

        class incoming:
            has_images = False

    def test_context_is_prepended_to_the_message(self):
        from prompt_builder import build_user_message

        text = build_user_message(self._Robot(), "", "【群里最近 15 分钟还发生了这些】\n  a: b")
        assert text.index("群里最近") < text.index("小红 说：")

    def test_the_quote_note_comes_after_the_context(self):
        """The quote explains the current message, so it sits closest to it."""
        from prompt_builder import build_user_message

        text = build_user_message(self._Robot(), "【引用回复】…", "【群里最近 15 分钟…】")
        assert text.index("群里最近") < text.index("【引用回复】") < text.index("小红 说：")

    def test_no_context_leaves_the_message_unchanged(self):
        from prompt_builder import build_user_message

        text = build_user_message(self._Robot(), "", "")
        assert text.startswith("群「测试群」中")

    def test_private_chat_never_gets_group_context(self):
        from prompt_builder import build_user_message

        class Private(self._Robot):
            msg_type = "private"

        text = build_user_message(Private(), "", "【群里最近 15 分钟还发生了这些】")
        assert "群里最近" not in text


class TestContextDefaults:
    def test_ambient_context_is_on_by_default(self):
        from config import Config

        assert Config.GROUP_CONTEXT_ENABLED is True
        assert Config.GROUP_CONTEXT_MINUTES == 15
        assert Config.GROUP_CONTEXT_LIMIT == 20

    def test_the_prompt_explains_the_background_is_attached(self):
        from prompt_builder import build_system_prompt

        class Incoming:
            has_images = False

        class Robot:
            msg_type, msg = "group", "那这个呢"
            group_id, user_id = "g1", "u1"
            user_name, group_name = "小明", "测试群"
            incoming = Incoming()

        prompt = build_system_prompt(Robot())
        assert "群聊语境" in prompt
        assert "背景" in prompt
        assert "read_context" in prompt, "the deeper tool must still be offered"


class TestAmbientContextSelection:
    """Which recent lines the model gets to see."""

    def _seed(self, db):
        db.record_group_message("g1", "u1", "小明", "我先说一句", message_id=101)
        db.record_group_message("g1", "u2", "小红", "别人插一句", message_id=102)
        db.record_group_message("g1", "u1", "小明", "那这个呢", message_id=103)
        db.record_bot_message("g1", 104, "回了小红")
        return db

    def test_current_message_is_excluded(self, db):
        self._seed(db)
        rows = db.get_recent_group_context("g1", minutes=30,
                                           exclude_message_id=103)
        contents = [r["content"] for r in rows]
        assert "那这个呢" not in contents

    def test_the_authors_other_messages_are_kept(self, db):
        """The gap that exclude_user left: their own earlier lines matter.

        Messages that never mentioned the bot are stored nowhere else — not in
        `history` either — so dropping the whole author made them invisible.
        """
        self._seed(db)
        rows = db.get_recent_group_context("g1", minutes=30,
                                           exclude_message_id=103)
        assert "我先说一句" in [r["content"] for r in rows]

    def test_exclude_user_still_works_for_other_callers(self, db):
        self._seed(db)
        rows = db.get_recent_group_context("g1", minutes=30, exclude_user="u1")
        contents = [r["content"] for r in rows]
        assert "我先说一句" not in contents
        assert "那这个呢" not in contents
        assert "别人插一句" in contents

    def test_the_bots_lines_are_included(self, db):
        self._seed(db)
        rows = db.get_recent_group_context("g1", minutes=30)
        assert any(r["is_bot"] and "回了小红" in r["content"] for r in rows)

    def test_ordering_is_oldest_first(self, db):
        self._seed(db)
        rows = db.get_recent_group_context("g1", minutes=30)
        contents = [r["content"] for r in rows]
        assert contents.index("我先说一句") < contents.index("那这个呢")

    def test_no_exclusion_returns_everything(self, db):
        self._seed(db)
        assert len(db.get_recent_group_context("g1", minutes=30)) == 4

    def test_a_non_numeric_exclusion_id_is_ignored_not_fatal(self, db):
        self._seed(db)
        rows = db.get_recent_group_context("g1", minutes=30,
                                           exclude_message_id="not-a-number")
        assert rows, "a junk id must not blank the whole transcript"
