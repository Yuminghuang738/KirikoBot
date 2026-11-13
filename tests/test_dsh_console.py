"""DSH 控制台与管理 API 接口测试。"""
from __future__ import annotations

import base64
import json
from typing import Any
import pytest

from config import Config
import dashboard_auth
from main import app


@pytest.fixture
def client():
    app.config["TESTING"] = True
    c = app.test_client()
    # 注入 Dashboard Basic Auth 凭据
    token = base64.b64encode(
        f"{Config.DASHBOARD_USER}:{dashboard_auth._load_password()}".encode()
    ).decode()
    c.environ_base["HTTP_AUTHORIZATION"] = f"Basic {token}"
    return c


class TestDshConsoleEndpoints:
    def test_dsh_status_endpoint(self, client):
        res = client.get("/api/dsh/status")
        assert res.status_code == 200
        data = res.get_json()
        assert "available" in data
        assert "workspace" in data
        assert "sessions" in data
        assert isinstance(data["sessions"], list)

    def test_dsh_plugins_endpoint(self, client):
        res = client.get("/api/dsh/plugins")
        assert res.status_code == 200
        data = res.get_json()
        assert "bundles" in data
        assert "profile_dir" in data

    def test_dsh_execute_empty_task_returns_400(self, client):
        res = client.post(
            "/api/dsh/execute",
            data=json.dumps({"task": ""}),
            content_type="application/json",
        )
        assert res.status_code == 400
        data = res.get_json()
        assert data["ok"] is False
        assert "不能为空" in data["error"]

    def test_dsh_execute_success_workflow(self, client, monkeypatch):
        import main

        def fake_run(task, session_key="", timeout=None, meta=None):
            if meta is not None:
                meta["status"] = "ok"
            return f"执行完成: {task}"

        monkeypatch.setattr(main.dsh_agent_tool.client, "run_task", fake_run)

        res = client.post(
            "/api/dsh/execute",
            data=json.dumps({"task": "计算 1+1", "session_key": "test_web"}),
            content_type="application/json",
        )
        assert res.status_code == 200
        data = res.get_json()
        assert data["ok"] is True
        assert data["result"] == "执行完成: 计算 1+1"
