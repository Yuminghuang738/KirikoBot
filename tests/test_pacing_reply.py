"""拟人短句分次发送（Split & Pace Sending）与多轮追问唤醒测试。"""
from __future__ import annotations

import time
from typing import Any
import pytest

from robot_server import RobotServer, split_into_pacing_chunks


class TestSplitIntoPacingChunks:
    def test_empty_string_returns_empty(self):
        assert split_into_pacing_chunks("") == []
        assert split_into_pacing_chunks("   \n  ") == []

    def test_short_message_stays_single_chunk(self):
        text = "行啊，明天下午一起去看看。"
        assert split_into_pacing_chunks(text) == [text]

    def test_markdown_code_block_is_never_split(self):
        text = "你可以这样写：\n```python\nprint('hello')\nfor i in range(10):\n    print(i)\n```\n跑一下试试"
        assert split_into_pacing_chunks(text) == [text]

    def test_markdown_list_is_never_split(self):
        text = "清单如下：\n- 第一点建议\n- 第二点建议\n- 第三点建议"
        assert split_into_pacing_chunks(text) == [text]

    def test_markdown_table_is_never_split(self):
        text = "对比：\n| 方案 | 效果 |\n| A | 好 |\n| B | 差 |"
        assert split_into_pacing_chunks(text) == [text]

    def test_natural_newlines_split_into_chunks(self):
        text = (
            "module_param 是把外面的参数引进来，变量还是在模块内部\n"
            "EXPORT_SYMBOL 才是把符号放进内核符号表供别人引用\n"
            "两个方向正好相反，不能混在一块"
        )
        chunks = split_into_pacing_chunks(text)
        assert len(chunks) == 3
        assert "module_param" in chunks[0]
        assert "EXPORT_SYMBOL" in chunks[1]
        assert "两个方向" in chunks[2]

    def test_long_single_sentence_splits_by_punctuation(self):
        text = (
            "今天一共887条消息，39个人在聊，图占了两百多条！"
            "你一个人发了288条，排第二的才85，真是一骑绝尘啊。"
        )
        chunks = split_into_pacing_chunks(text)
        assert len(chunks) == 2
        assert chunks[0].endswith("！")
        assert "一骑绝尘" in chunks[1]

    def test_max_chunks_capped_at_three(self):
        text = "第一句讲原理。\n第二句讲具体用法。\n第三句给出避坑指南。\n第四句吐槽一句。\n第五句结尾。"
        chunks = split_into_pacing_chunks(text, max_chunks=3)
        assert len(chunks) <= 3


class FakeLLBot:
    def __init__(self):
        self.reply_calls: list[str] = []
        self.send_text_calls: list[str] = []

    def reply_to(self, msg: Any, text: str) -> bool:
        self.reply_calls.append(text)
        return True

    def send_text(self, msg: Any, text: str) -> bool:
        self.send_text_calls.append(text)
        return True


class TestReplyChunkedSending:
    @pytest.fixture
    def setup_robot(self, monkeypatch):
        fake_llbot = FakeLLBot()
        raw_msg = {
            "message_type": "group",
            "user_id": 123456,
            "group_id": 789012,
            "sender": {"nickname": "测试用户"},
            "message": [{"type": "text", "data": {"text": "hello"}}],
        }
        robot = RobotServer(raw_msg, fake_llbot, "10000")
        delays: list[float] = []
        monkeypatch.setattr(time, "sleep", lambda s: delays.append(s))
        return robot, fake_llbot, delays

    def test_single_chunk_sends_only_reply_to(self, setup_robot):
        robot, llbot, delays = setup_robot
        ok = robot.reply_chunked("随口应一句短话")
        assert ok is True
        assert len(llbot.reply_calls) == 1
        assert llbot.reply_calls[0] == "随口应一句短话"
        assert len(llbot.send_text_calls) == 0
        assert len(delays) == 0

    def test_multiple_chunks_send_reply_first_then_send_text(self, setup_robot):
        robot, llbot, delays = setup_robot
        text = "第一条讲原因\n第二条给出具体建议\n第三条补充注意事项"
        ok = robot.reply_chunked(text)
        assert ok is True
        assert len(llbot.reply_calls) == 1
        assert llbot.reply_calls[0] == "第一条讲原因"
        assert len(llbot.send_text_calls) == 2
        assert llbot.send_text_calls[0] == "第二条给出具体建议"
        assert llbot.send_text_calls[1] == "第三条补充注意事项"
        assert len(delays) == 2
        assert all(d >= 0.4 for d in delays)

    def test_rate_limiter_checked_only_once_for_entire_turn(self, setup_robot, monkeypatch):
        robot, llbot, delays = setup_robot
        allow_count = []

        class Limiter:
            def allow(self, gid, uid):
                allow_count.append(1)
                return True

        from robot_server import install_rate_limiter
        install_rate_limiter(Limiter())

        text = "第一句很长很长讲原因。\n第二句很长很长讲办法。"
        robot.reply_chunked(text)
        # 整个回合的分批发送只应消耗一次限流配额，不能每句都扣一次导致断头
        assert len(allow_count) == 1
        install_rate_limiter(None)


class TestMultiTurnReplyActivation:
    """验证群聊多轮追问场景：用户长按回复/引用机器人的消息时，即便没带 @，也顺畅激活。"""

    def test_quoting_bot_message_activates_response(self, monkeypatch):
        import main
        from llbot_client import IncomingMessage, ReplyInfo

        calls = []

        class DummyAi:
            ai_message = {}
            reasoning_content = ""
            handled_summary = ""

            def __init__(self, *a, **k):
                pass

            def ai_request(self):
                calls.append(1)

        class FakeRobot:
            msg_type = "group"
            group_id = "g1"
            group_name = "测试群"
            user_id = "u1"
            user_name = "小明"
            user_role = "member"
            msg = "为什么会这样呢"
            at_judgement = False  # 没有显式 @ 机器人
            incoming = IncomingMessage(
                raw={},
                msg_type="group",
                user_id="u1",
                user_name="小明",
                group_id="g1",
                text="为什么会这样呢",
                is_at_bot=False,
                reply=ReplyInfo(message_seq=10086, text="机器人的上一句话"),
            )

            def reply_chunked(self, text):
                pass
            def reply(self, text):
                pass

        monkeypatch.setattr(main, "_seed_group", lambda gid: None)
        monkeypatch.setattr(main, "_run_ai_judge", lambda robot: None)
        monkeypatch.setattr(main, "AiServer", DummyAi)
        # 模拟 llbot 确认该 message_seq 确实是本机器人发出的发言
        monkeypatch.setattr(main.llbot, "is_own_message", lambda seq, text: True)

        main.main_logic(FakeRobot())
        assert len(calls) == 1, "引用回复机器人自身发出的消息时，应当成功激活 AI 回复（多轮追问顺畅衔接）"

