"""DSH Agent (超级大脑) 工具集成测试。"""
from __future__ import annotations

import json
from typing import Any
import pytest

from ai_tools import DshAgentTool
from ai_tools_list import AiTools
from feature_gate import FEATURE_DEFS, TOOL_FEATURE, disabled_tool_names
import main


class TestDshToolRegistration:
    def test_dsh_agent_is_in_ai_tools_list(self):
        names = {t["function"]["name"] for t in AiTools().ai_tools()}
        assert "dsh_agent" in names

    def test_dsh_agent_schema_properties(self):
        tools = {t["function"]["name"]: t["function"] for t in AiTools().ai_tools()}
        dsh = tools["dsh_agent"]
        assert "task" in dsh["parameters"]["properties"]
        assert dsh["parameters"]["required"] == ["task"]
        assert "超级大脑" in dsh["description"]

    def test_dsh_agent_is_in_routes(self):
        assert "dsh_agent" in main.ROUTES
        assert callable(main.ROUTES["dsh_agent"])

    def test_dsh_agent_is_self_contained(self):
        assert "dsh_agent" in main.SELF_CONTAINED_TOOLS

    def test_feature_gate_controls_dsh_agent(self):
        keys = {f["key"] for f in FEATURE_DEFS}
        assert "dsh_agent" in keys
        assert TOOL_FEATURE.get("dsh_agent") == "dsh_agent"
        assert "dsh_agent" in disabled_tool_names({"dsh_agent"})


class FakeRobot:
    def __init__(self, msg="帮我写个脚本"):
        self.msg = msg
        self.msg_type = "group"
        self.group_id = "g100"
        self.user_id = "u200"
        self.user_name = "小明"
        self.replies: list[str] = []
        self.chunked_replies: list[str] = []

    def reply(self, text: str) -> bool:
        self.replies.append(text)
        return True

    def reply_chunked(self, text: str) -> bool:
        self.chunked_replies.append(text)
        return True


class FakeAI:
    def __init__(self, tool_args: dict[str, Any] | None = None):
        args_str = json.dumps(tool_args or {})
        self.ai_message = {
            "tool_calls": [{
                "id": "call_123",
                "function": {"name": "dsh_agent", "arguments": args_str},
            }]
        }
        self.handled_summary = ""
        self.user_text = ""
        self.airesponse_tool_id = ""
        self.airesponse_tool_calls = []


class TestDshAgentToolExecution:
    def test_tool_call_workflow(self, monkeypatch):
        db = object()
        tool = DshAgentTool(db)

        # 模拟 client.run_task 返回结果
        run_calls = []

        def fake_run(task, session_key="", timeout=None):
            run_calls.append((task, session_key))
            return "代码已经生成并执行完毕：\nprint('success')"

        monkeypatch.setattr(tool.client, "run_task", fake_run)

        robot = FakeRobot("原问题文本")
        ai = FakeAI({"task": "用Python写一个冒泡排序并运行测试"})

        tool.dsh_agent_call(robot, ai)

        # 1. 应该先发一句安抚话
        assert len(robot.replies) == 1
        assert "稍等" in robot.replies[0] or "跑一下" in robot.replies[0]

        # 2. 应该委派给 client 执行正确的任务与 session_key
        assert len(run_calls) == 1
        assert run_calls[0][0] == "用Python写一个冒泡排序并运行测试"
        assert run_calls[0][1] == "group:g100"

        # 3. 最终结果应该走 reply_chunked 发出
        assert len(robot.chunked_replies) == 1
        assert "success" in robot.chunked_replies[0]

        # 4. 元数据和摘要记录完整
        assert "[超级大脑]" in ai.handled_summary
        assert ai.airesponse_tool_id == "call_123"

    def test_tool_call_fallback_to_robot_msg_when_args_empty(self, monkeypatch):
        tool = DshAgentTool(object())
        run_calls = []
        monkeypatch.setattr(tool.client, "run_task", lambda task, session_key="": run_calls.append(task) or "done")

        robot = FakeRobot("帮我计算哈希值")
        ai = FakeAI({})  # 无 task 参数

        tool.dsh_agent_call(robot, ai)
        assert run_calls[0] == "帮我计算哈希值"
