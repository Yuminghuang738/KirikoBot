"""Group activity stats, chat review, and the three new AI tools."""
from __future__ import annotations

import sqlite3

import pytest


class TestBotMessageLog:
    """The bot's own messages: needed for recall and for complete transcripts."""

    def test_records_and_finds_the_last_message(self, db):
        db.record_bot_message("g1", 555, "我是机器人")
        last = db.get_last_bot_message("g1")
        assert last["message_id"] == 555
        assert "机器人" in last["text"]

    def test_returns_none_when_nothing_sent(self, db):
        assert db.get_last_bot_message("g1") is None

    def test_recalled_messages_are_not_returned_again(self, db):
        db.record_bot_message("g1", 555, "撤回我")
        db.mark_bot_message_recalled(555)
        assert db.get_last_bot_message("g1") is None

    def test_only_the_most_recent_is_returned(self, db):
        db.record_bot_message("g1", 1, "第一条")
        db.record_bot_message("g1", 2, "第二条")
        assert db.get_last_bot_message("g1")["message_id"] == 2

    def test_messages_are_scoped_per_group(self, db):
        db.record_bot_message("g1", 1, "给 g1")
        db.record_bot_message("g2", 2, "给 g2")
        assert db.get_last_bot_message("g1")["message_id"] == 1

    def test_old_messages_fall_outside_the_recall_window(self, db):
        """QQ loses recall ability after ~2 minutes, so stale ids must not be offered."""
        db.execute_action(
            "INSERT INTO bot_messages (group_id, message_id, text, created_at) "
            "VALUES ('g1', 999, '很久以前', datetime('now','localtime','-10 minutes'))"
        )
        assert db.get_last_bot_message("g1", max_age_seconds=110) is None
        assert db.get_last_bot_message("g1", max_age_seconds=3600)["message_id"] == 999

    def test_none_message_id_is_ignored(self, db):
        db.record_bot_message("g1", None, "发送失败没有 id")
        assert db.get_last_bot_message("g1") is None


class TestDailyStats:
    def _seed(self, db, day_offset: int = 0):
        rows = [
            ("u1", "小明", "你好"),
            ("u1", "小明", "在吗"),
            ("u2", "小红", "[图片消息]"),
        ]
        for uid, name, content in rows:
            db.execute_action(
                "INSERT INTO group_messages (group_id,user_id,user_name,content,timestamp) "
                "VALUES ('g1',?,?,?, datetime('now','localtime',?))",
                (uid, name, content, f"{day_offset} days"),
            )

    def test_counts_and_ranking(self, db):
        self._seed(db)
        stats = db.get_daily_group_stats("g1")
        assert stats["total"] == 3
        assert stats["active_users"] == 2
        assert stats["images"] == 1
        assert stats["top"][0] == {"user_name": "小明", "count": 2, "user_id": "u1"}
        assert len(stats["hourly"]) == 24
        assert sum(stats["hourly"]) == 3

    def test_other_days_are_excluded(self, db):
        self._seed(db)
        self._seed(db, day_offset=-1)
        assert db.get_daily_group_stats("g1")["total"] == 3

    def test_empty_day_is_all_zero(self, db):
        stats = db.get_daily_group_stats("g1", "2000-01-01")
        assert stats["total"] == 0
        assert stats["top"] == []
        assert stats["active_users"] == 0

    def test_groups_are_isolated(self, db):
        self._seed(db)
        assert db.get_daily_group_stats("g2")["total"] == 0

    def test_days_listing(self, db):
        self._seed(db)
        days = db.get_group_days("g1")
        assert days and days[0]["count"] == 3


class TestGroupContext:
    def _seed(self, db):
        db.record_group_message("g1", "u1", "小明", "你好", message_seq=101)
        db.record_bot_message("g1", 900, "你好呀～")
        db.record_group_message("g1", "u2", "小红", "在吗", message_seq=102)

    def test_merges_bot_and_member_lines_in_order(self, db):
        self._seed(db)
        ctx = db.get_recent_group_context("g1", minutes=60)
        assert [c["content"] for c in ctx] == ["你好", "你好呀～", "在吗"]
        assert [c["is_bot"] for c in ctx] == [False, True, False]

    def test_excludes_the_current_speaker(self, db):
        """The message being answered is already stored — don't echo it back.

        The bot's own lines must survive: excluding a *speaker* is about hiding
        the current message, not about hiding the bot.
        """
        self._seed(db)
        ctx = db.get_recent_group_context("g1", minutes=60, exclude_user="u1")
        assert [c["content"] for c in ctx] == ["你好呀～", "在吗"]

    def test_recalled_bot_lines_are_hidden(self, db):
        self._seed(db)
        db.mark_bot_message_recalled(900)
        ctx = db.get_recent_group_context("g1", minutes=60)
        assert "你好呀～" not in [c["content"] for c in ctx]

    def test_zero_and_garbage_inputs_are_clamped(self, db):
        self._seed(db)
        assert len(db.get_recent_group_context("g1", minutes=0, limit=0)) == 1
        assert len(db.get_recent_group_context("g1", minutes="abc", limit=None)) == 3
        assert len(db.get_recent_group_context("g1", minutes=99_999, limit=99_999)) == 3


class TestTranscriptPaging:
    def _seed(self, db, n=5):
        for i in range(n):
            db.record_group_message("g1", "u1", "小明", f"消息{i}", message_seq=100 + i)

    def test_pages_are_chronological_within_the_page(self, db):
        self._seed(db)
        page = db.get_group_message_page("g1", size=3)
        assert [m["content"] for m in page["items"]] == ["消息2", "消息3", "消息4"]
        assert page["total"] == 5 and page["pages"] == 2

    def test_second_page_holds_the_older_messages(self, db):
        self._seed(db)
        page = db.get_group_message_page("g1", page=2, size=3)
        assert [m["content"] for m in page["items"]] == ["消息0", "消息1"]

    def test_keyword_filter(self, db):
        self._seed(db)
        page = db.get_group_message_page("g1", keyword="消息3")
        assert [m["content"] for m in page["items"]] == ["消息3"]
        assert page["total"] == 1

    def test_bot_lines_appear_in_the_transcript(self, db):
        self._seed(db, n=1)
        db.record_bot_message("g1", 900, "机器人说的话")
        page = db.get_group_message_page("g1")
        assert page["total"] == 2
        assert any(m["is_bot"] and m["content"] == "机器人说的话" for m in page["items"])

    def test_filter_by_bot_name(self, db):
        self._seed(db, n=2)
        db.record_bot_message("g1", 900, "机器人说的话")
        page = db.get_group_message_page("g1", user_name=db.BOT_DISPLAY_NAME)
        assert [m["content"] for m in page["items"]] == ["机器人说的话"]

    def test_size_is_clamped(self, db):
        self._seed(db)
        assert db.get_group_message_page("g1", size=100000)["pages"] == 1


class TestToolRegistration:
    TOOLS = {"recall_message", "group_stats", "read_context"}

    def test_schemas_are_exposed_to_the_model(self):
        from ai_tools_list import AiTools

        names = {t["function"]["name"] for t in AiTools().ai_tools()}
        assert self.TOOLS <= names

    def test_feature_gate_knows_them(self):
        from feature_gate import FEATURE_KEYS, TOOL_FEATURE

        assert {"recall", "group_stats", "context_read"} <= set(FEATURE_KEYS)
        assert TOOL_FEATURE["recall_message"] == "recall"
        assert TOOL_FEATURE["group_stats"] == "group_stats"
        assert TOOL_FEATURE["read_context"] == "context_read"

    def test_disabling_a_feature_hides_its_tool(self):
        from feature_gate import disabled_tool_names

        blocked = disabled_tool_names({"recall", "context_read"})
        assert {"recall_message", "read_context"} <= blocked
        assert "group_stats" not in blocked


class TestContextPromptGuidance:
    def test_prompt_tells_the_model_when_to_read_context(self):
        from prompt_builder import build_system_prompt

        class Incoming:
            has_images = False

        class Robot:
            msg_type, msg = "group", "那这个呢"
            group_id, user_id = "g1", "u1"
            user_name, group_name = "小明", "测试群"
            incoming = Incoming()

        prompt = build_system_prompt(Robot())
        assert "read_context" in prompt
        assert "群聊语境" in prompt


class TestTurnChainRecording:
    """The chain belongs to a turn, and users ask about the PREVIOUS reply."""

    def test_saves_and_reads_back_the_thinking_chain(self, db):
        db.deposit_chat_history("assistant", "u1", "g1", "今天多云", "", "",
                                reasoning="先查天气再回答")
        last = db.get_last_bot_turn("u1", "g1")
        assert last["content"] == "今天多云"
        assert last["reasoning"] == "先查天气再回答"

    def test_stores_the_tool_chain_json(self, db):
        chain = '[{"name": "weather", "arguments": "{\\"city\\": \\"杭州\\"}"}]'
        db.deposit_chat_history("assistant", "u1", "g1", "查到了", chain, "", "")
        last = db.get_last_bot_turn("u1", "g1")
        assert '"weather"' in last["tool_calls"]

    def test_returns_the_most_recent_reply_only(self, db):
        db.deposit_chat_history("assistant", "u1", "g1", "第一句", "", "", "")
        db.deposit_chat_history("user", "u1", "g1", "第二问", "", "", "")
        db.deposit_chat_history("assistant", "u1", "g1", "第二句", "", "", "")
        assert db.get_last_bot_turn("u1", "g1")["content"] == "第二句"

    def test_user_rows_are_not_returned(self, db):
        db.deposit_chat_history("user", "u1", "g1", "用户的问话", "", "", "")
        assert db.get_last_bot_turn("u1", "g1") is None

    def test_scoped_per_user_and_group(self, db):
        db.deposit_chat_history("assistant", "u1", "g1", "给 u1 的", "", "", "")
        db.deposit_chat_history("assistant", "u2", "g1", "给 u2 的", "", "", "")
        assert db.get_last_bot_turn("u2", "g1")["content"] == "给 u2 的"
        assert db.get_last_bot_turn("u1", "g2") is None

    def test_history_page_exposes_chain_and_reasoning(self, db):
        db.deposit_chat_history("assistant", "u1", "g1", "回复",
                                '[{"name":"dice"}]', "", reasoning="想了想")
        rec = db.get_all_history(10)[0]
        assert rec["tool_calls"] == '[{"name":"dice"}]'
        assert rec["reasoning"] == "想了想"

    def test_reasoning_column_is_migrated_onto_old_dbs(self, tmp_path):
        import sqlite3

        from database_manager import DatabaseManager

        path = str(tmp_path / "legacy.db")
        conn = sqlite3.connect(path)
        conn.execute(
            """CREATE TABLE history(
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
                group_id TEXT, role TEXT NOT NULL, content TEXT NOT NULL,
                tool_calls TEXT, tool_call_id TEXT,
                timestamp DATETIME DEFAULT (datetime('now','localtime'))
            )"""
        )
        conn.execute("INSERT INTO history (user_id,role,content) VALUES ('u1','assistant','旧回复')")
        conn.commit()
        conn.close()

        DatabaseManager(path)
        conn = sqlite3.connect(path)
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(history)")}
            assert "reasoning" in cols
            assert conn.execute("SELECT COUNT(*) FROM history").fetchone()[0] == 1
        finally:
            conn.close()


class TestExplainSelfRawDump:
    """explain_self is a DEBUG tool: it must show the chain, not re-tell it.

    An earlier version handed the record back to the model and asked it to
    narrate in first person, so the user got a paraphrase of the thinking
    chain instead of the chain itself. Since the whole point is inspecting
    what the model actually thought, the dump is now reproduced verbatim.
    """

    RAW = "第一步：先看用户问了什么\n第二步：翻一下群里的记录\n  这里缩进也要保留\n第三步：决定回答"

    class _Incoming:
        message_id = 42

    class _Robot:
        msg_type, group_id, user_id, user_name = "group", "g1", "u1", "小明"

        def __init__(self, llbot):
            self.llbot = llbot
            self.incoming = TestExplainSelfRawDump._Incoming()

    class _AI:
        def __init__(self):
            self.ai_message = {
                "tool_calls": [{"id": "call_1", "function": {"name": "explain_self"}}],
            }
            self.tool_result_text = ""
            self.user_text = ""

    class _LLBot:
        def __init__(self):
            self.sent = []

        def send_group_msg(self, group_id, message):
            self.sent.append((group_id, message))
            return True

        def send_private_msg(self, user_id, message):
            self.sent.append((user_id, message))
            return True

    def _run(self, db, reasoning=RAW, chain="", content="回复内容"):
        from ai_tools import ExplainSelfTool

        db.deposit_chat_history("assistant", "u1", "g1", content, chain, "",
                                reasoning=reasoning)
        llbot = self._LLBot()
        ai = self._AI()
        ExplainSelfTool(db, None).explain_self_call(self._Robot(llbot), ai)
        text = "\n".join(
            seg["data"]["text"]
            for _, segments in llbot.sent
            for seg in segments
            if seg["type"] == "text"
        )
        return llbot.sent, text, ai

    def test_reasoning_is_reproduced_verbatim(self, db):
        """Newlines and indentation survive — this is the raw text, not a summary."""
        _, text, _ = self._run(db)
        assert self.RAW in text

    def test_whitespace_is_not_collapsed(self, db):
        """" ".join(split()) would have flattened the chain into one line."""
        _, text, _ = self._run(db)
        assert "翻一下群里的记录\n  这里缩进也要保留" in text

    def test_dump_is_sent_directly_not_left_to_the_model(self, db):
        sent, _, ai = self._run(db)
        assert sent, "the dump must actually be sent to the chat"
        assert "思维链原文" in sent[0][1][-1]["data"]["text"]

    def test_model_is_told_to_stay_silent(self, db):
        """Otherwise the follow-up turn paraphrases what we just dumped."""
        _, _, ai = self._run(db)
        assert "不要再说" in ai.tool_result_text

    def test_tool_chain_and_reply_are_included(self, db):
        chain = '[{"name": "dice", "arguments": "{\\"n\\": 6}"}]'
        _, text, _ = self._run(db, chain=chain, content="我掷了个 6")
        assert "dice" in text
        assert "我掷了个 6" in text

    def test_missing_reasoning_says_so_instead_of_inventing(self, db):
        _, text, _ = self._run(db, reasoning="")
        assert "没有思维链" in text

    def test_no_previous_turn_is_reported_plainly(self, db):
        from ai_tools import ExplainSelfTool

        llbot = self._LLBot()
        ExplainSelfTool(db, None).explain_self_call(self._Robot(llbot), self._AI())
        assert "没有上一轮" in llbot.sent[0][1][-1]["data"]["text"]

    def test_long_dumps_are_chunked_not_truncated(self, db):
        """QQ text limits are per-message, so split rather than lose the tail.

        A thinking chain is often one unbroken paragraph, so this uses no
        newlines at all — line-based chunking alone would emit one huge
        segment and the tail would be dropped by the platform.
        """
        sent, text, _ = self._run(db, reasoning="x" * 5000)
        assert sent, "nothing was sent"
        texts = [
            seg["data"]["text"]
            for _, segments in sent
            for seg in segments
            if seg["type"] == "text"
        ]
        assert all(len(t) <= 1300 for t in texts), "chunk over QQ's limit"
        assert text.count("x") == 5000, "the tail must survive chunking"
        assert "(1/" in text and "5/" in text

    def test_runs_without_a_follow_up_turn(self):
        """Registered self-contained, so the model never re-narrates the dump."""
        import ast
        import os

        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "KirikoBot", "main.py",
        )
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
                if "SELF_CONTAINED_TOOLS" in targets:
                    names = {e.value for e in node.value.elts}
                    assert "explain_self" in names
                    return
        raise AssertionError("SELF_CONTAINED_TOOLS not found")
