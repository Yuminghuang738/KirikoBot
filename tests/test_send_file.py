"""文件发送（Send File）与 DSH 文件回传自动化测试。"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest

import robot_server
from config import Config

from ai_tools import SendFileTool, DshAgentTool
from ai_tools_list import AiTools
from feature_gate import FEATURE_DEFS, TOOL_FEATURE, disabled_tool_names
from llbot_client import IncomingMessage, LLBotClient
from robot_server import RobotServer
import main


class FakeLLBot(LLBotClient):
    def __init__(self):
        self.uploaded_group_files: list[tuple[str, str, str]] = []
        self.uploaded_private_files: list[tuple[str, str, str]] = []
        self.sent_texts: list[str] = []

    def upload_group_file(self, group_id: str, file_path: str, name: str) -> bool:
        self.uploaded_group_files.append((str(group_id), file_path, name))
        return True

    def upload_private_file(self, user_id: str, file_path: str, name: str) -> bool:
        self.uploaded_private_files.append((str(user_id), file_path, name))
        return True

    def reply_to(self, msg: Any, text: str) -> bool:
        self.sent_texts.append(text)
        return True

    def send_text(self, msg: Any, text: str) -> bool:
        return True


class TestRobotServerSendFile:
    @pytest.fixture
    def env_setup(self, tmp_path, monkeypatch):
        # 所有落盘都进临时目录，绝不写仓库里的 shared_files。
        monkeypatch.setattr(robot_server, "SHARED_FILES_DIR", str(tmp_path / "shared_files"))
        fake_llbot = FakeLLBot()
        raw_group_msg = {
            "message_type": "group",
            "user_id": 111,
            "group_id": 222,
            "sender": {"nickname": "群友"},
            "message": [{"type": "text", "data": {"text": "给我发文件"}}],
        }
        group_robot = RobotServer(raw_group_msg, fake_llbot, "10000")

        raw_private_msg = {
            "message_type": "private",
            "user_id": 333,
            "sender": {"nickname": "私聊好友"},
            "message": [{"type": "text", "data": {"text": "发私聊文件"}}],
        }
        private_robot = RobotServer(raw_private_msg, fake_llbot, "10000")

        # 创建一个普通测试文件
        normal_file = tmp_path / "report.pdf"
        normal_file.write_text("dummy pdf content", encoding="utf-8")

        return group_robot, private_robot, fake_llbot, str(normal_file), tmp_path

    def test_send_file_nonexistent_returns_false(self, env_setup):
        group_robot, _, _, _, tmp_path = env_setup
        assert group_robot.send_file(str(tmp_path / "not_found.zip")) is False

    @pytest.mark.parametrize("bad_name", [
        ".env",
        "production.env",
        "id_rsa",
        "id_ed25519",
        "server.pem",
        "database_password.txt",
        "api_token.json",
        "client_secret.key",
    ])
    def test_send_file_blocks_sensitive_files(self, env_setup, bad_name):
        group_robot, _, _, _, tmp_path = env_setup
        sensitive_file = tmp_path / bad_name
        sensitive_file.write_text("secret_data", encoding="utf-8")
        assert group_robot.send_file(str(sensitive_file)) is False
        assert len(group_robot.llbot.uploaded_group_files) == 0

    def test_send_group_file_success(self, env_setup):
        group_robot, _, fake_llbot, normal_file, _ = env_setup
        ok = group_robot.send_file(normal_file, display_name="2026年度报告.pdf")
        assert ok is True
        assert len(fake_llbot.uploaded_group_files) == 1
        gid, path, name = fake_llbot.uploaded_group_files[0]
        assert gid == "222"
        assert name == "2026年度报告.pdf"
        assert "shared_files" in path

    def test_send_private_file_success(self, env_setup):
        _, private_robot, fake_llbot, normal_file, _ = env_setup
        ok = private_robot.send_file(normal_file)
        assert ok is True
        assert len(fake_llbot.uploaded_private_files) == 1
        uid, path, name = fake_llbot.uploaded_private_files[0]
        assert uid == "333"
        assert name == "report.pdf"

    def test_send_file_bypasses_ratelimit_when_turn_already_authorized(self, env_setup):
        group_robot, _, fake_llbot, normal_file, _ = env_setup
        # 模拟限流器已经用尽配额（下一次 allow 返回 False）
        class MockFullLimiter:
            def allow(self, gid, uid): return False

        from robot_server import install_rate_limiter
        install_rate_limiter(MockFullLimiter())

        # 如果没有回合授权，应被限流拦截
        assert group_robot.send_file(normal_file) is False

        # 模拟当前回合已经通过了文字回复的入口授权（_turn_authorized = True）
        group_robot._turn_authorized = True
        # 伴随的文件/图片产物必须放行送达，绝不能被限流器误杀
        assert group_robot.send_file(normal_file) is True
        assert len(fake_llbot.uploaded_group_files) == 1

        install_rate_limiter(None)


class TestSendFileToolWorkflow:
    def test_tool_registration(self):
        names = {t["function"]["name"] for t in AiTools().ai_tools()}
        assert "send_file" in names
        assert "send_file" in main.ROUTES
        assert "send_file" in main.SELF_CONTAINED_TOOLS
        assert "send_file" in {f["key"] for f in FEATURE_DEFS}
        assert TOOL_FEATURE.get("send_file") == "send_file"

    def test_send_file_call_workflow(self, tmp_path):
        dummy_file = tmp_path / "chart.png"
        dummy_file.write_text("png", encoding="utf-8")

        sent = []

        class RobotStub:
            def reply(self, t): pass
            def send_file(self, fpath, display_name=""):
                sent.append((fpath, display_name))
                return True

        class AIStub:
            ai_message = {
                "tool_calls": [{
                    "id": "c1",
                    "function": {"name": "send_file", "arguments": json.dumps({"file_path": str(dummy_file), "display_name": "图表.png"})},
                }]
            }
            handled_summary = ""
            user_text = ""
            airesponse_tool_id = ""
            airesponse_tool_calls = []

        tool = SendFileTool(object())
        tool.send_file_call(RobotStub(), AIStub())

        assert len(sent) == 1
        assert sent[0][0] == str(dummy_file)
        assert sent[0][1] == "图表.png"

    def test_send_file_resends_last_artifact_when_file_path_is_last(self, tmp_path):
        dummy_tab = tmp_path / "nirvana_tab.pdf"
        dummy_tab.write_text("pdf bytes", encoding="utf-8")

        sent = []

        class RobotStub:
            msg_type = "private"
            group_id = None
            user_id = "u123"
            def reply(self, t): pass
            def send_file(self, fpath, display_name=""):
                sent.append((fpath, display_name))
                return True

        class FakeDB:
            def get_state(self, key, default=""):
                if key == "last_artifact:private:u123":
                    return str(dummy_tab)
                return default

        class AIStub:
            ai_message = {
                "tool_calls": [{
                    "id": "c2",
                    "function": {"name": "send_file", "arguments": json.dumps({"file_path": "刚才的谱子"})},
                }]
            }
            handled_summary = ""
            user_text = ""
            airesponse_tool_id = ""
            airesponse_tool_calls = []

        tool = SendFileTool(FakeDB())
        tool.send_file_call(RobotStub(), AIStub())

        assert len(sent) == 1
        assert sent[0][0] == str(dummy_tab)
        assert "nirvana_tab.pdf" in sent[0][1]

    def test_send_file_falls_back_to_user_level_artifact(self, tmp_path):
        """测试用户在私聊产生文件，跑到群里喊重发，能够自动通过 user:uid 命中产物。"""
        dummy_file = tmp_path / "global_shared.pdf"
        dummy_file.write_text("global content", encoding="utf-8")

        sent = []

        class GroupRobotStub:
            msg_type = "group"
            group_id = "g_test_999"
            user_id = "u123"
            def reply(self, t): pass
            def send_file(self, fpath, display_name=""):
                sent.append((fpath, display_name))
                return True

        class FakeDB:
            def get_state(self, key, default=""):
                # 群里没有该产物记录，但该用户全局记录存在
                if key == "last_artifact:user:u123":
                    return str(dummy_file)
                return default

        class AIStub:
            ai_message = {
                "tool_calls": [{
                    "id": "c3",
                    "function": {"name": "send_file", "arguments": json.dumps({"file_path": "重发"})},
                }]
            }
            handled_summary = ""
            user_text = ""
            airesponse_tool_id = ""
            airesponse_tool_calls = []

        tool = SendFileTool(FakeDB())
        tool.send_file_call(GroupRobotStub(), AIStub())

        assert len(sent) == 1
        assert sent[0][0] == str(dummy_file)


class TestDshAgentFileAutoSend:
    """验证当 Harness 找到或生成文件并带有 [FILE:/path] 标记时，自动触发文件上传。"""

    def test_dsh_result_containing_file_tag_triggers_send_file(self, monkeypatch, tmp_path):
        tool = DshAgentTool(object())
        artifact = tmp_path / "extracted_log.txt"
        artifact.write_text("log content", encoding="utf-8")

        # 模拟 Harness 执行完毕，输出带文件标记的文本
        harness_output = (
            "已经帮你找到了对应的日志文件，内容排查正常。\n"
            f"[FILE:{str(artifact)}]"
        )
        monkeypatch.setattr(tool.client, "run_task", lambda *a, **k: harness_output)

        sent_files = []
        chunked = []

        class RobotStub:
            msg = "帮我找一下昨天的日志文件并发我"
            msg_type = "group"
            group_id = "g1"
            user_id = "u1"

            def reply(self, t): pass
            def reply_chunked(self, t): chunked.append(t)
            def send_file(self, p): sent_files.append(p)

        class AIStub:
            ai_message = {}
            handled_summary = ""
            user_text = ""
            airesponse_tool_id = ""
            airesponse_tool_calls = []

        tool.dsh_agent_call(RobotStub(), AIStub())

        # 1. 文本内容中的 [FILE:...] 标记应该被清洗，文本正常发送
        assert len(chunked) == 1
        assert "[FILE:" not in chunked[0]
        assert "已经帮你找到了对应的日志文件" in chunked[0]

        # 2. 应该自动调用 send_file 上传该文件
        assert len(sent_files) == 1
        assert sent_files[0] == str(artifact)


class TestSendFileSecurity:
    """R01/R05：display_name 穿越、数据库外泄、直链内网 IP。"""

    @pytest.fixture
    def setup(self, tmp_path, monkeypatch):
        monkeypatch.setattr(robot_server, "SHARED_FILES_DIR", str(tmp_path / "shared_files"))
        fake = FakeLLBot()
        raw = {
            "message_type": "group",
            "user_id": 111,
            "group_id": 222,
            "sender": {"nickname": "群友"},
            "message": [{"type": "text", "data": {"text": "发文件"}}],
        }
        robot = RobotServer(raw, fake, "10000")
        src = tmp_path / "report.pdf"
        src.write_text("dummy", encoding="utf-8")
        return robot, fake, src, tmp_path

    def test_traversal_display_name_is_reduced_to_basename(self, setup):
        robot, fake, src, tmp_path = setup
        assert robot.send_file(str(src), display_name="../../pwned.html") is True
        _, path, name = fake.uploaded_group_files[0]
        assert name == "pwned.html"
        shared = Path(robot_server.SHARED_FILES_DIR).resolve()
        assert Path(path).resolve().is_relative_to(shared)
        assert (shared / "pwned.html").is_file()
        # 不能写到 shared_files 外面
        assert not (tmp_path / "pwned.html").exists()
        assert not (tmp_path.parent / "pwned.html").exists()

    @pytest.mark.parametrize("bad", [
        "robot.db", "chat.db", "app.sqlite3", "robot.db-wal", "robot.db-shm",
    ])
    def test_database_files_are_never_sent(self, setup, bad):
        robot, fake, _, tmp_path = setup
        db_file = tmp_path / bad
        db_file.write_text("sqlite bytes", encoding="utf-8")
        assert robot.send_file(str(db_file)) is False
        assert fake.uploaded_group_files == []

    def test_source_outside_allowed_roots_is_rejected(self, setup, monkeypatch):
        robot, fake, src, _ = setup
        monkeypatch.setattr(
            robot_server, "_allowed_source_roots",
            lambda: [robot_server.SHARED_FILES_DIR],
        )
        assert robot.send_file(str(src)) is False
        assert fake.uploaded_group_files == []

    def test_download_url_uses_public_base_url_and_quotes_name(self, setup, monkeypatch):
        robot, fake, src, _ = setup
        monkeypatch.setattr(Config, "PUBLIC_BASE_URL", "https://kb.example.com/")
        assert robot.send_file(str(src), display_name="中文 报告.pdf") is True
        text = fake.sent_texts[-1]
        assert "https://kb.example.com/download/" + quote("中文 报告.pdf") in text
        assert "100.93" not in text

    def test_download_url_default_has_no_internal_ip(self, setup, monkeypatch):
        robot, fake, src, _ = setup
        monkeypatch.setattr(Config, "PUBLIC_BASE_URL", "")
        robot.public_base_url = ""
        assert robot.send_file(str(src)) is True
        text = fake.sent_texts[-1]
        assert "http://localhost:5000/download/report.pdf" in text
        assert "100.93.52.78" not in text


@pytest.mark.parametrize("path,expected", [
    ("robot.db", True),
    ("/app/robot.db-wal", True),
    ("/app/robot.db-shm", True),
    ("chat.sqlite3", True),
    ("/app/.env", True),
    ("/home/u/.ssh/id_ed25519", True),
    ("client_secret.key", True),
    ("api_token.json", True),
    ("report.pdf", False),
    ("chart.png", False),
    ("debug.pdf", False),
])
def test_is_sensitive_file(path, expected):
    assert robot_server.is_sensitive_file(path) is expected


class TestDownloadEndpoint:
    """免鉴权 /download 也不能把数据库/密钥直链拿走。"""

    @pytest.mark.parametrize("name", [
        "robot.db", "robot.db-wal", "chat.sqlite3", ".env", "id_ed25519", "api_token.json",
    ])
    def test_sensitive_downloads_are_403(self, name):
        client = main.app.test_client()
        assert client.get(f"/download/{name}").status_code == 403

    def test_normal_missing_file_is_404_not_403(self):
        client = main.app.test_client()
        assert client.get("/download/not_there.txt").status_code == 404
