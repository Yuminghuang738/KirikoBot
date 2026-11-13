"""DSH 自主交付工具 (dsh_deliver) 与自治触发测试。"""
from __future__ import annotations

import json
import time
from typing import Any
import pytest

from dsh_deliver import clean_markdown, deliver
from ai_tools import DshAgentTool


class FakeDB:
    def __init__(self):
        self.state: dict[str, str] = {}

    def get_state(self, key: str, default: str = "") -> str:
        return self.state.get(key, default)

    def set_state(self, key: str, value: str) -> None:
        self.state[key] = value


class FakeLLBot:
    def __init__(self):
        self.group_msgs: list[tuple[str, str]] = []
        self.private_msgs: list[tuple[str, str]] = []
        self.group_files: list[tuple[str, str, str]] = []
        self.private_files: list[tuple[str, str, str]] = []

    def send_group_msg(self, gid: str, msg: list[dict[str, Any]]) -> bool:
        text = "".join(m["data"].get("text", "") for m in msg if m.get("type") == "text")
        self.group_msgs.append((str(gid), text))
        return True

    def send_private_msg(self, uid: str, msg: list[dict[str, Any]]) -> bool:
        text = "".join(m["data"].get("text", "") for m in msg if m.get("type") == "text")
        self.private_msgs.append((str(uid), text))
        return True

    def reply_image(self, incoming: Any, path: str) -> bool:
        return True

    def reply_to(self, incoming: Any, text: str) -> bool:
        return True

    def upload_group_file(self, gid: str, file_path: str, name: str) -> bool:
        self.group_files.append((str(gid), file_path, name))
        return True

    def upload_private_file(self, uid: str, file_path: str, name: str) -> bool:
        self.private_files.append((str(uid), file_path, name))
        return True


class TestDshDeliverModule:
    def test_invalid_channel_fails(self):
        assert deliver("invalid_channel", "text") is False
        assert deliver("", "text") is False

    def test_deliver_text_to_group(self):
        db = FakeDB()
        llbot = FakeLLBot()
        ok = deliver("group:734954188", text="这是超级大脑自主计算出来的结论", db=db, llbot=llbot)
        assert ok is True
        assert len(llbot.group_msgs) == 1
        assert llbot.group_msgs[0][0] == "734954188"
        assert "超级大脑自主计算" in llbot.group_msgs[0][1]
        assert "dsh_delivered:group:734954188" in db.state

    def test_deliver_text_and_file_to_private(self, tmp_path):
        db = FakeDB()
        llbot = FakeLLBot()
        artifact = tmp_path / "result.pdf"
        artifact.write_text("pdf bytes", encoding="utf-8")

        ok = deliver(
            "private:3234979817",
            text="谱子已找到",
            file_path=str(artifact),
            display_name="吉他谱.pdf",
            db=db,
            llbot=llbot,
        )
        assert ok is True
        assert len(llbot.private_msgs) >= 1
        assert db.get_state("last_artifact:private:3234979817") == str(artifact)
        assert db.get_state("last_artifact:user:3234979817") == str(artifact)


class TestDshAgentToolAutonomousDeduplication:
    """验证当 DSH 内部自主调用了 deliver 时，外层 dsh_agent_call 绝不重复发送相同文本。"""

    def test_already_delivered_skips_outer_reply(self, monkeypatch):
        db = FakeDB()
        tool = DshAgentTool(db)

        # 模拟 DSH 进程执行时自主调用了 deliver 并更新了戳记
        def fake_run(task, session_key="", timeout=None, meta=None):
            if meta is not None:
                meta["status"] = "ok"
            db.set_state(f"dsh_delivered:{session_key}", str(time.time() + 1))
            return "这是被输出在终端的文本"

        monkeypatch.setattr(tool.client, "run_task", fake_run)

        replies = []

        class RobotStub:
            msg = "帮我跑个分析"
            msg_type = "group"
            group_id = "g100"
            user_id = "u200"
            def reply(self, t): replies.append(t)
            def reply_chunked(self, t): replies.append(t)
            def send_file(self, p): pass

        class AIStub:
            ai_message = {"tool_calls": [{"id": "c1", "function": {"arguments": "{}"}}]}
            handled_summary = ""
            user_text = ""
            airesponse_tool_id = ""
            airesponse_tool_calls = []

        tool.dsh_agent_call(RobotStub(), AIStub())

        # 只应该有前置的那句“稍等安抚语”，不应该在外层再次重复调用 reply_chunked
        assert len(replies) == 1
        assert "稍等" in replies[0]


class TestCleanMarkdown:
    """QQ 气泡不渲染 Markdown，交付前必须压成纯文本。"""

    def test_bold_and_list_markers_are_stripped(self):
        src = "已找到\n\n- **角色**：市谷有咲\n- **尺寸**：1100 × 1400 px"
        assert clean_markdown(src) == "已找到 角色：市谷有咲 尺寸：1100 × 1400 px"

    def test_link_keeps_its_text(self):
        src = "来源：[BanG Dream 官方](https://anime.bang-dream.com/x)"
        assert clean_markdown(src) == "来源：BanG Dream 官方"

    def test_headings_are_stripped(self):
        assert clean_markdown("## 分析结果\n\n**结论**：不错") == "分析结果 结论：不错"

    def test_image_is_dropped_entirely(self):
        assert clean_markdown("![图](http://a/b.jpg)完成") == "完成"

    def test_plain_text_is_unchanged(self):
        assert clean_markdown("普通文字，没有标记") == "普通文字，没有标记"

    def test_deliver_sends_cleaned_text(self):
        """端到端：交付到群里的文本必须已经被清洗。"""
        db = FakeDB()
        llbot = FakeLLBot()
        ok = deliver(
            "group:734954188",
            text="- **角色**：市谷有咲\n- 来源：[官网](https://x.y)",
            db=db,
            llbot=llbot,
        )
        assert ok is True
        assert len(llbot.group_msgs) == 1, "一次交付只应产生一条消息，不能有调试尾巴"
        _, sent = llbot.group_msgs[0]
        assert "**" not in sent
        assert "](" not in sent
        assert "市谷有咲" in sent
