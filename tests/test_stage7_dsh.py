"""阶段 7：DSH 埋点、锁、并发上限与同源状态页。"""
from __future__ import annotations

import os
import threading
import time

import pytest

from dsh_client import DshClient, _clean_stale_session_lock


class TestDshMetricsSchema:
    def test_tool_usage_has_dsh_metric_columns(self, db):
        cols = {r[1] for r in db.fetch_data("PRAGMA table_info(tool_usage)")}
        for col in ("duration_ms", "status", "session_key",
                    "session_recovered", "queue_wait_ms", "error"):
            assert col in cols

    def test_start_and_finish_write_one_row(self, db):
        task_id = db.start_dsh_task("u1", "g1", "group:g1")
        assert task_id > 0
        db.finish_dsh_task(
            task_id, status="ok", duration_ms=1234, session_recovered=True,
            error="", arguments="写个脚本", result="done", reasoning="思路",
        )
        row = db.fetch_data(
            "SELECT status, duration_ms, session_recovered, arguments, result, reasoning "
            "FROM tool_usage WHERE id=?", (task_id,),
        )[0]
        assert row == ("ok", 1234, 1, "写个脚本", "done", "思路")

    def test_stale_running_rows_become_interrupted(self, db):
        task_id = db.start_dsh_task("u1", None, "private:u1")
        changed = db.mark_stale_dsh_tasks_interrupted()
        assert changed == 1
        status = db.fetch_data("SELECT status FROM tool_usage WHERE id=?", (task_id,))[0][0]
        assert status == "interrupted"


class TestStaleLock:
    def _make_lock(self, tmp_path, age_seconds):
        base = tmp_path / ".dsh" / "sessions" / "session-abc"
        base.mkdir(parents=True)
        lock = base / "session.lock"
        lock.write_text("pid=1", encoding="utf-8")
        old = time.time() - age_seconds
        os.utime(lock, (old, old))
        return lock

    def test_fresh_lock_is_kept(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        lock = self._make_lock(tmp_path, 5)
        _clean_stale_session_lock("session-abc")
        assert lock.exists(), "正在跑的任务锁不能被误删"

    def test_old_lock_is_cleaned(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        lock = self._make_lock(tmp_path, 10_000)
        _clean_stale_session_lock("session-abc")
        assert not lock.exists()


class TestConcurrencyCap:
    def test_rejects_immediately_when_all_slots_busy(self, monkeypatch):
        import dsh_client

        monkeypatch.setattr(dsh_client, "_RUN_SLOTS", threading.BoundedSemaphore(1))
        client = DshClient(object())
        monkeypatch.setattr(client, "is_available", lambda: True)
        assert dsh_client._RUN_SLOTS.acquire(blocking=False) is True
        try:
            meta = {}
            result = client.run_task("任务", session_key="group:g1", meta=meta)
            assert meta["status"] == "rejected"
            assert "排队" in result
        finally:
            dsh_client._RUN_SLOTS.release()


class TestDshAgentRecords:
    def test_tool_records_one_finished_row_with_meta(self, db, monkeypatch):
        import main
        from ai_tools import DshAgentTool

        tool = DshAgentTool(db)

        def fake_run(task, session_key="", timeout=None, meta=None):
            meta["status"] = "ok"
            meta["session_recovered"] = -1     # 本来就没有存量 session
            return "结果"

        monkeypatch.setattr(tool.client, "run_task", fake_run)
        robot = type("R", (), {
            "msg_type": "group", "group_id": "g1", "user_id": "u1",
            "user_name": "小明", "msg": "原问题",
            "reply": lambda self, text: None,
            "reply_chunked": lambda self, text: None,
            "send_file": lambda self, path: None,
        })()
        ai = type("A", (), {
            "ai_message": {"tool_calls": [{"id": "1", "function": {
                "name": "dsh_agent", "arguments": '{"task": "写脚本"}',
            }}]},
            "handled_summary": "", "user_text": "", "reasoning_content": "",
        })()

        tool.dsh_agent_call(robot, ai)

        rows = db.fetch_data(
            "SELECT status, arguments, result, session_recovered "
            "FROM tool_usage WHERE tool_name='dsh_agent'",
        )
        assert len(rows) == 1
        assert rows[0][0] == "ok"
        assert rows[0][1] == "写脚本"
        assert rows[0][3] == -1, "bool(-1) 会把无会话任务误记成恢复成功"


class TestDshStatusApi:
    @pytest.fixture
    def client(self):
        import base64

        import dashboard_auth
        import main
        from config import Config

        main.app.config["TESTING"] = True
        client = main.app.test_client()
        token = base64.b64encode(
            f"{Config.DASHBOARD_USER}:{dashboard_auth._load_password()}".encode()
        ).decode()
        client.environ_base["HTTP_AUTHORIZATION"] = f"Basic {token}"
        return client

    def test_status_uses_native_page(self, client, monkeypatch):
        import main

        monkeypatch.setattr(main.dsh_agent_tool.client, "is_available", lambda: False)
        body = client.get("/api/dsh/status").get_json()
        assert body["web_ui"] == "native"
        assert "web_token" not in body

    def test_tasks_reports_duration_and_success_rate(self, db, client, monkeypatch):
        import main

        # 续写成功 / 新会话（本来没有 session，-1）/ timeout / rejected
        rows = (
            ("ok", 1000, 1), ("ok", 3000, -1), ("timeout", 0, -1), ("rejected", 0, -1),
        )
        for status, duration, recovered in rows:
            task_id = db.start_dsh_task("u1", "g1", "group:g1")
            db.finish_dsh_task(
                task_id, status=status, duration_ms=duration,
                session_recovered=recovered,
            )
        monkeypatch.setattr(main, "db", db)
        body = client.get("/api/dsh/tasks").get_json()
        stats = body["stats"]
        assert stats["n"] == 3, "rejected 不进成功率分母"
        assert stats["success_rate"] == pytest.approx(2 / 3, abs=0.01)
        assert stats["rejected"] == 1
        assert stats["reject_rate"] == pytest.approx(0.25)
        assert stats["p50_ms"] == 2000, "线性插值：1000 与 3000 的中位是 2000"
        assert stats["p90_ms"] == 2800
        assert stats["session_tasks"] == 1, "-1 不算有存量 session"
        assert stats["recovery_rate"] == 1.0


def test_delivery_failure_is_recorded_as_error(db, monkeypatch):
    from ai_tools import DshAgentTool

    tool = DshAgentTool(db)

    def fake_run(task, session_key="", timeout=None, meta=None):
        meta["status"] = "ok"
        return "结果"

    monkeypatch.setattr(tool.client, "run_task", fake_run)

    class Robot:
        msg_type = "group"
        group_id = "g1"
        user_id = "u1"
        user_name = "小明"
        msg = "原问题"

        def reply(self, text):
            pass

        def reply_chunked(self, text):
            raise RuntimeError("QQ send failed")

        def send_file(self, path):
            pass

    class AI:
        ai_message = {"tool_calls": [{"id": "1", "function": {
            "name": "dsh_agent", "arguments": '{"task": "写脚本"}'}}]}
        handled_summary = ""
        user_text = ""
        reasoning_content = ""

    tool.dsh_agent_call(Robot(), AI())

    row = db.fetch_data(
        "SELECT status, error FROM tool_usage WHERE tool_name='dsh_agent'",
    )[0]
    assert row[0] == "error"
    assert "delivery_failed" in row[1]


class TestPercentileHelper:
    def test_linear_interpolation(self):
        import main

        assert main._percentile([1000, 3000], 0.5) == 2000
        assert main._percentile([1000, 3000], 0.9) == 2800
        assert main._percentile([1000], 0.9) == 1000
        assert main._percentile([], 0.5) is None
        assert main._percentile([100, 200, 300, 400], 0.5) == 250
