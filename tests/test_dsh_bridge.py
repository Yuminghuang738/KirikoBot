"""DSH (DeepSeek Harness) 桥接客户端单元测试。"""
from __future__ import annotations

import json
import subprocess
from typing import Any
import pytest

from dsh_client import DshClient, _resolve_dsh_api_key


class FakeDB:
    def __init__(self):
        self.state: dict[str, str] = {}

    def get_state(self, key: str, default: str = "") -> str:
        return self.state.get(key, default)

    def set_state(self, key: str, value: str) -> None:
        self.state[key] = value


class TestDshClientBasics:
    def test_resolve_dsh_api_key(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-123")
        assert _resolve_dsh_api_key() == "test-key-123"

    def test_is_available_when_disabled(self, monkeypatch):
        import config
        monkeypatch.setattr(config.Config, "DSH_ENABLED", False)
        client = DshClient(FakeDB())
        assert client.is_available() is False

    def test_run_task_returns_fallback_when_unavailable(self, monkeypatch):
        import config
        monkeypatch.setattr(config.Config, "DSH_ENABLED", False)
        client = DshClient(FakeDB())
        res = client.run_task("写个脚本")
        assert "尚未配置就绪" in res

    def test_empty_task_returns_warning(self, monkeypatch):
        import config
        monkeypatch.setattr(config.Config, "DSH_ENABLED", True)
        client = DshClient(FakeDB())
        monkeypatch.setattr(client, "is_available", lambda: True)
        res = client.run_task("")
        assert "为空" in res


class TestDshClientExecution:
    @pytest.fixture
    def client(self, monkeypatch):
        db = FakeDB()
        client = DshClient(db)
        monkeypatch.setattr(client, "is_available", lambda: True)
        client._dsh_bin = "/usr/bin/dsh"
        return client, db

    def test_init_session_captures_id_and_returns_text(self, client, monkeypatch):
        c, db = client
        recorded_cmd = []

        class FakePopen:
            def __init__(self, cmd, **kwargs):
                recorded_cmd.extend(cmd)
                self.returncode = 0

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def communicate(self, timeout=None):
                output = "\n".join([
                    json.dumps({"type": "session", "sessionId": "session-test-999"}),
                    json.dumps({"type": "status", "phase": "step_start"}),
                    json.dumps({"type": "text", "text": "这是来自超级大脑的计算结果"}),
                ])
                return output, ""

        monkeypatch.setattr(subprocess, "Popen", FakePopen)

        ans = c.run_task("帮我算个复杂微积分", session_key="user:1001")
        assert ans == "这是来自超级大脑的计算结果"
        assert db.get_state("dsh_session:user:1001") == "session-test-999"
        assert "--json" in recorded_cmd
        assert any("帮我算个复杂微积分" in str(arg) for arg in recorded_cmd)

    def test_subsequent_turn_uses_stored_session_id(self, client, monkeypatch):
        c, db = client
        db.set_state("dsh_session:user:1001", "session-existing-123")
        recorded_cmd = []

        def fake_run(cmd, **kwargs):
            recorded_cmd.extend(cmd)
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="这是续写的分析结果\n", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)

        ans = c.run_task("接着刚才的分析继续说", session_key="user:1001")
        assert ans == "这是续写的分析结果"
        assert "--session-id" in recorded_cmd
        assert "session-existing-123" in recorded_cmd

    def test_timeout_handling(self, client, monkeypatch):
        c, db = client

        class TimeoutPopen:
            def __init__(self, cmd, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def communicate(self, timeout=None):
                raise subprocess.TimeoutExpired(cmd=["dsh"], timeout=1)

            def kill(self):
                pass

        monkeypatch.setattr(subprocess, "Popen", TimeoutPopen)

        ans = c.run_task("死循环任务")
        assert "时间过长" in ans
