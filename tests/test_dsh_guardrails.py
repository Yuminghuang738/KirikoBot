"""DSH 思考等级调优、安全守则与死锁自愈测试。"""
from __future__ import annotations

import json
import os
import subprocess
from typing import Any
import pytest

from dsh_client import (
    DshClient,
    ensure_dsh_patch_file,
    _guard_task_prompt,
    _clean_stale_session_lock,
    DSH_GUARDRAILS,
)
import config


class TestDshPatchReasoningEffort:
    def test_patch_contains_low_reasoning_effort(self, tmp_path):
        patch_file = ensure_dsh_patch_file(str(tmp_path))
        content = open(patch_file, encoding="utf-8").read()
        assert "reasoningEffort: low" in content
        assert "deepseek-official" in content

    def test_custom_reasoning_effort_honored(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config.Config, "DSH_REASONING_EFFORT", "medium")
        patch_file = ensure_dsh_patch_file(str(tmp_path))
        content = open(patch_file, encoding="utf-8").read()
        assert "reasoningEffort: medium" in content


class TestDshGuardrailsPrompt:
    def test_guardrails_contain_speedup_and_safety_rules(self):
        assert "国内可用镜像源" in DSH_GUARDRAILS
        assert "严禁任何破坏性系统命令" in DSH_GUARDRAILS
        assert "严禁读取、打印或向外泄露任何敏感凭据" in DSH_GUARDRAILS
        assert "优先复用本会话" in DSH_GUARDRAILS
        assert "[FILE:" in DSH_GUARDRAILS

    def test_guard_task_prompt_wraps_user_task(self):
        prompt = _guard_task_prompt("下载一个文件", has_session=False)
        assert "【用户交办的当前任务】" in prompt
        assert "下载一个文件" in prompt
        assert "继承和复用" not in prompt

    def test_guard_task_prompt_includes_reuse_hint_when_has_session(self):
        prompt = _guard_task_prompt("接着上一句做", has_session=True)
        assert "继承和复用本会话" in prompt
        assert "接着上一句做" in prompt


class TestSessionLockSelfHealing:
    def test_clean_stale_lock_removes_file(self, tmp_path, monkeypatch):
        fake_home = tmp_path / "home"
        session_dir = fake_home / ".dsh" / "sessions" / "test-workspace" / "session-xyz"
        session_dir.mkdir(parents=True)
        lock_file = session_dir / "session.lock"
        lock_file.write_text("locked", encoding="utf-8")

        monkeypatch.setenv("HOME", str(fake_home))
        assert lock_file.exists()

        _clean_stale_session_lock("session-xyz")
        assert not lock_file.exists()


class FakeDB:
    def __init__(self):
        self.state: dict[str, str] = {}

    def get_state(self, key: str, default: str = "") -> str:
        return self.state.get(key, default)

    def set_state(self, key: str, value: str) -> None:
        self.state[key] = value


class TestSessionResumeFallback:
    def test_resume_failure_falls_back_to_new_session_immediately(self, monkeypatch):
        db = FakeDB()
        db.set_state("dsh_session:group:100", "session-old-dead")
        client = DshClient(db)
        monkeypatch.setattr(client, "is_available", lambda: True)
        client._dsh_bin = "/usr/bin/dsh"

        calls = []

        # 模拟第一次 run (resume) 失败返回 1，随后 Popen 自动启动新 Session 成功
        def fake_run(cmd, **kwargs):
            calls.append("resume")
            return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="lock error")

        monkeypatch.setattr(subprocess, "run", fake_run)

        class FakePopen:
            def __init__(self, cmd, **kwargs):
                calls.append("init_new")
                self.returncode = 0

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def communicate(self, timeout=None):
                output = "\n".join([
                    json.dumps({"type": "session", "sessionId": "session-new-alive"}),
                    json.dumps({"type": "text", "text": "来自新会话的回答"}),
                ])
                return output, ""

        monkeypatch.setattr(subprocess, "Popen", FakePopen)

        ans = client.run_task("跑个复杂任务", session_key="group:100")
        assert ans == "来自新会话的回答"
        # 应该先尝试 resume，失败后无感调用 init_new
        assert calls == ["resume", "init_new"]
        # 数据库中已自动替换为新 Session ID
        assert db.get_state("dsh_session:group:100") == "session-new-alive"
