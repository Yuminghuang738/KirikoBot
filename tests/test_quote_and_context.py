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

    def test_the_scoped_lookup_is_tried_first(self, db):
        db.record_bot_message("g1", 123, "g1 的那条")
        assert db.find_quoted("g1", 123)["text"] == "g1 的那条"

    def test_a_group_id_mismatch_still_resolves(self, db):
        """Scoping alone failed silently: a wrong group id dropped the note and
        the bot answered as if nothing had been quoted. QQ ids are unique
        account-wide, so falling back to a global lookup is safe and turns an
        invisible miss into a hit."""
        db.record_bot_message("g1", 123, "给 g1 的")
        found = db.find_quoted("some-other-group", 123)
        assert found is not None and found["text"] == "给 g1 的"

    def test_a_private_chat_lookup_still_works_without_a_group(self, db):
        db.record_group_message("g1", "u1", "小明", "群里说的", message_id=321)
        assert db.find_quoted(None, 321)["text"] == "群里说的"

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
        assert "群「测试群」中" in text
        assert "群里最近" not in text

    def test_private_chat_never_gets_group_context(self):
        from prompt_builder import build_user_message

        class Private(self._Robot):
            msg_type = "private"

        text = build_user_message(Private(), "", "【群里最近 15 分钟还发生了这些】")
        assert "群里最近" not in text


class TestContextDefaults:
    def test_ambient_context_is_off_by_default(self):
        """Reading the room is the model's decision, not an unconditional dump.

        Attaching a transcript to every message was tried and reverted: what
        was wanted was a looser trigger for read_context, not blanket
        awareness. Quote resolution is what is always on.
        """
        from config import Config

        assert Config.GROUP_CONTEXT_ENABLED is False

    def test_the_knobs_still_exist_for_opting_in(self):
        from config import Config

        assert Config.GROUP_CONTEXT_MINUTES == 15
        assert Config.GROUP_CONTEXT_LIMIT == 20

    class _Robot:
        msg_type, msg = "group", "那这个呢"
        group_id, user_id = "g1", "u1"
        user_name, group_name = "小明", "测试群"

        class incoming:
            has_images = False

    def test_the_trigger_is_narrow(self, monkeypatch):
        """It was loosened to "when in doubt, look" and then fired on greetings.

        Over-firing does not just cost tokens: the transcript lands in front of
        the model and the reply answers *it* instead of the actual message.
        """
        from config import Config
        from prompt_builder import build_system_prompt

        monkeypatch.setattr(Config, "GROUP_CONTEXT_ENABLED", False)
        prompt = build_system_prompt(self._Robot())
        assert "群聊语境" in prompt
        assert "read_context" in prompt
        assert "只有**一种**情况需要调用" in prompt

    def test_it_names_what_not_to_look_up(self, monkeypatch):
        from config import Config
        from prompt_builder import build_system_prompt

        monkeypatch.setattr(Config, "GROUP_CONTEXT_ENABLED", False)
        prompt = build_system_prompt(self._Robot())
        for dont in ("打招呼", "骂你", "夸你", "收到", "图片"):
            assert dont in prompt, f"missing do-not-call case: {dont}"

    def test_it_reverses_the_old_doubt_rule(self, monkeypatch):
        from config import Config
        from prompt_builder import build_system_prompt

        monkeypatch.setattr(Config, "GROUP_CONTEXT_ENABLED", False)
        prompt = build_system_prompt(self._Robot())
        assert "拿不准的时候不要查" in prompt
        assert "宁可先问一句" in prompt

    def test_it_explains_why_over_looking_hurts(self, monkeypatch):
        from config import Config
        from prompt_builder import build_system_prompt

        monkeypatch.setattr(Config, "GROUP_CONTEXT_ENABLED", False)
        prompt = build_system_prompt(self._Robot())
        assert "淹掉" in prompt
        assert "答非所问" in prompt

    def test_opting_in_describes_the_attached_background(self, monkeypatch):
        from config import Config
        from prompt_builder import build_system_prompt

        monkeypatch.setattr(Config, "GROUP_CONTEXT_ENABLED", True)
        prompt = build_system_prompt(self._Robot())
        assert "已经附了一段最近的群聊背景" in prompt

    def test_private_chat_gets_no_group_context_rule(self, monkeypatch):
        from config import Config
        from prompt_builder import build_system_prompt

        monkeypatch.setattr(Config, "GROUP_CONTEXT_ENABLED", False)

        class Private(self._Robot):
            msg_type = "private"
            group_id = None

        assert "群聊语境" not in build_system_prompt(Private())


class TestReplySegmentParsing:
    """The reply segment's id and message_seq are different number spaces.

    Live LLBot sends only `{"id": ...}` and that value matches
    bot_messages.message_id. Taking `message_seq` first would silently resolve
    to nothing whenever a build supplies both.
    """

    def test_id_is_preferred_over_message_seq(self):
        from llbot_client import IncomingMessage

        reply = IncomingMessage._extract_reply([
            {"type": "reply", "data": {"id": "75563830", "message_seq": 36427}},
        ])
        assert reply.message_seq == 75563830

    def test_message_seq_is_used_when_id_is_absent(self):
        from llbot_client import IncomingMessage

        reply = IncomingMessage._extract_reply([
            {"type": "reply", "data": {"message_seq": "75563830"}},
        ])
        assert reply.message_seq == 75563830

    def test_the_live_payload_shape_parses(self):
        """Exactly what a real LLBot sends."""
        from llbot_client import IncomingMessage

        reply = IncomingMessage._extract_reply([
            {"type": "reply", "data": {"id": "75563830"}},
        ])
        assert reply is not None
        assert reply.message_seq == 75563830
        assert reply.text == "" and reply.sender_name == ""

    def test_a_non_numeric_id_does_not_raise(self):
        from llbot_client import IncomingMessage

        reply = IncomingMessage._extract_reply([
            {"type": "reply", "data": {"id": "abc"}},
        ])
        assert reply.message_seq is None

    def test_no_reply_segment(self):
        from llbot_client import IncomingMessage

        assert IncomingMessage._extract_reply([{"type": "text", "data": {"text": "hi"}}]) is None


class TestContextToolDoesNotHijack:
    """The transcript must be framed as background, not as the thing to answer.

    Live symptom: someone said "唱秋妈妈给我听" and the bot replied to a line
    from the transcript it had just fetched instead. The tool caused that, not
    the history.
    """

    class _Robot:
        msg_type, group_id, user_id, user_name = "group", "g1", "u1", "小明"

        class incoming:
            message_id = 1

    class _AI:
        def __init__(self):
            self.ai_message = {"tool_calls": [{"id": "c1", "function": {
                "name": "read_context", "arguments": "{}"}}]}
            self.tool_result_text = ""
            self.user_text = ""

    def _rows(self, n=12):
        return [{"user_name": f"u{i}", "content": f"消息{i}", "timestamp": "2026-09-18 18:0%d" % i,
                 "is_bot": False} for i in range(n)]

    def _run(self, db, monkeypatch, rows=None):
        import ai_tools

        tool = ai_tools.ReadContextTool(db, None)
        monkeypatch.setattr(db, "get_recent_group_context",
                            lambda *a, **k: self._rows() if rows is None else rows)
        ai = self._AI()
        tool.read_context_call(self._Robot(), ai)
        return ai.tool_result_text

    def test_the_framing_comes_before_the_transcript(self, db, monkeypatch):
        import ai_tools

        monkeypatch.setattr(ai_tools, "_context_seen", {})
        text = self._run(db, monkeypatch)
        assert text.index("不是要你回应的话") < text.index("消息0")

    def test_the_framing_is_repeated_at_the_end(self, db, monkeypatch):
        import ai_tools

        monkeypatch.setattr(ai_tools, "_context_seen", {})
        text = self._run(db, monkeypatch)
        assert "背景到此结束" in text
        assert text.index("背景到此结束") > text.index("消息0")

    def test_it_says_not_to_answer_the_background(self, db, monkeypatch):
        import ai_tools

        monkeypatch.setattr(ai_tools, "_context_seen", {})
        text = self._run(db, monkeypatch)
        assert "不要回应背景里的任何一条" in text

    def test_it_prefers_asking_over_guessing(self, db, monkeypatch):
        import ai_tools

        monkeypatch.setattr(ai_tools, "_context_seen", {})
        assert "就直接问，别猜" in self._run(db, monkeypatch)

    def test_long_lines_are_trimmed(self, db, monkeypatch):
        import ai_tools

        monkeypatch.setattr(ai_tools, "_context_seen", {})
        text = self._run(db, monkeypatch, rows=[{
            "user_name": "x", "content": "字" * 400, "timestamp": "2026-09-18 18:00",
            "is_bot": False}])
        assert "…" in text
        assert text.count("字") < 400

    def test_an_empty_group_says_to_answer_literally(self, db, monkeypatch):
        import ai_tools

        monkeypatch.setattr(ai_tools, "_context_seen", {})
        text = self._run(db, monkeypatch, rows=[])
        assert "按字面回答" in text
        assert "不要因为查了记录就硬找话说" in text


class TestRepeatGuard:
    """Prompts fail, so a second look within a couple of minutes is shrunk."""

    def test_the_defaults_are_small(self):
        from ai_tools_list import AiTools

        tools = {t["function"]["name"]: t["function"] for t in AiTools().ai_tools()}
        props = tools["read_context"]["parameters"]["properties"]
        assert "默认 15" in props["minutes"]["description"]
        assert "默认 20" in props["limit"]["description"]

    def test_a_first_look_is_not_throttled(self):
        import ai_tools

        ai_tools._context_seen.clear()
        assert ai_tools._just_looked("g1", "u1") is False

    def test_a_second_look_soon_after_is_throttled(self):
        import ai_tools

        ai_tools._context_seen.clear()
        assert ai_tools._just_looked("g1", "u1") is False
        assert ai_tools._just_looked("g1", "u1") is True

    def test_it_is_scoped_per_user_and_group(self):
        import ai_tools

        ai_tools._context_seen.clear()
        ai_tools._just_looked("g1", "u1")
        assert ai_tools._just_looked("g1", "u2") is False
        assert ai_tools._just_looked("g2", "u1") is False

    def test_the_window_is_a_few_minutes(self):
        import ai_tools

        assert 1 <= ai_tools.CONTEXT_REPEAT_MINUTES <= 10
        assert ai_tools.CONTEXT_REPEAT_LIMIT <= 10

    def test_the_repeat_is_disclosed_to_the_model(self, db, monkeypatch):
        import ai_tools

        ai_tools._context_seen.clear()
        tool = ai_tools.ReadContextTool(db, None)
        rows = [{"user_name": "x", "content": f"m{i}", "timestamp": "2026-09-18 18:00",
                 "is_bot": False} for i in range(30)]
        monkeypatch.setattr(db, "get_recent_group_context",
                            lambda *a, **k: rows[:k.get("limit", 20)])

        class Robot:
            msg_type, group_id, user_id, user_name = "group", "g9", "u9", "小明"

            class incoming:
                message_id = 1

        class AI:
            def __init__(self):
                self.ai_message = {"tool_calls": [{"id": "c", "function": {
                    "name": "read_context", "arguments": "{}"}}]}
                self.tool_result_text = ""

        first = AI()
        tool.read_context_call(Robot(), first)
        assert "别再查了" not in first.tool_result_text

        second = AI()
        tool.read_context_call(Robot(), second)
        assert "别再查了" in second.tool_result_text
        assert "只给最近几条" in second.tool_result_text

    def test_stale_entries_are_pruned(self):
        """The dict is bounded by who was active inside the window, not by a
        hard cap — but anything past the window must actually be dropped."""
        import time as _time
        import ai_tools

        ai_tools._context_seen.clear()
        stale = _time.time() - (ai_tools.CONTEXT_REPEAT_MINUTES * 60 + 60)
        for i in range(600):
            ai_tools._context_seen[(f"g{i}", "u")] = stale
        ai_tools._just_looked("fresh-group", "u")
        assert ("fresh-group", "u") in ai_tools._context_seen
        assert len(ai_tools._context_seen) == 1, "stale entries should be pruned"

    def test_recent_entries_are_kept(self):
        import ai_tools

        ai_tools._context_seen.clear()
        for i in range(600):
            ai_tools._just_looked(f"g{i}", "u")
        # All fresh, so all are legitimately still inside the window.
        assert len(ai_tools._context_seen) == 600
