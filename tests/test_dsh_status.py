"""ruff F821 抓到的真 bug：/api/dsh/status 用了未导入的 subprocess。

修好后这个端点应能返回真实版本号，而不是永远 "unknown"。
"""
from __future__ import annotations

import json

import main


class FakeDshClient:
    _dsh_bin = "/usr/bin/dsh"

    def is_available(self) -> bool:
        return True


class FakeResult:
    stdout = "dsh 9.9.9\n"


def test_dsh_status_reports_real_version(monkeypatch):
    monkeypatch.setattr(main.dsh_agent_tool, "client", FakeDshClient())
    monkeypatch.setattr(main.db, "fetch_data", lambda sql: [("dsh_session:group:1", "s1")])
    monkeypatch.setattr(main.subprocess, "run", lambda *a, **k: FakeResult())

    with main.app.test_request_context():
        payload = json.loads(main.api_dsh_status().get_data(as_text=True))

    assert payload["version"] == "dsh 9.9.9"
    assert payload["available"] is True
    assert payload["sessions"] == [{"target": "group:1", "session_id": "s1"}]


def test_dsh_status_handles_unavailable_and_db_errors(monkeypatch):
    class OfflineClient:
        _dsh_bin = ""

        def is_available(self) -> bool:
            return False

    monkeypatch.setattr(main.dsh_agent_tool, "client", OfflineClient())

    def broken(_sql):
        raise RuntimeError("db down")

    monkeypatch.setattr(main.db, "fetch_data", broken)

    with main.app.test_request_context():
        payload = json.loads(main.api_dsh_status().get_data(as_text=True))

    assert payload["version"] == ""
    assert payload["sessions"] == []
