"""DSH 任务详情：鉴权、arguments/result、筛选与统计口径。"""
from __future__ import annotations

import base64

import pytest


def _auth_client():
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


@pytest.fixture()
def client():
    return _auth_client()


def _row(db, status, *, session="group:g1", arguments="", result="", duration=0):
    task_id = db.start_dsh_task("u1", "g1", session)
    db.finish_dsh_task(
        task_id, status=status, duration_ms=duration,
        arguments=arguments, result=result,
    )
    return task_id


class TestDshTaskDetails:
    def test_unauthenticated_returns_401(self):
        import main

        main.app.config["TESTING"] = True
        assert main.app.test_client().get("/api/dsh/tasks").status_code == 401

    def test_tasks_carry_arguments_and_result(self, db, client, monkeypatch):
        import main

        _row(db, "ok", arguments="写个脚本", result="4", duration=123)
        monkeypatch.setattr(main, "db", db)

        task = client.get("/api/dsh/tasks").get_json()["tasks"][0]

        assert task["arguments"] == "写个脚本"
        assert task["result"] == "4"

    def test_status_filter_changes_list_but_not_stats(self, db, client, monkeypatch):
        import main

        _row(db, "ok", duration=100)
        _row(db, "rejected")
        monkeypatch.setattr(main, "db", db)

        body = client.get("/api/dsh/tasks?status=ok").get_json()

        assert [t["status"] for t in body["tasks"]] == ["ok"]
        # stats 固定看最近 100 条，不随筛选跳动
        assert body["stats"]["rejected"] == 1
        assert body["stats"]["n"] == 1
        assert body["stats"]["window"] == 100

    def test_session_filter(self, db, client, monkeypatch):
        import main

        _row(db, "ok", session="private:u1")
        _row(db, "ok", session="group:g1")
        monkeypatch.setattr(main, "db", db)

        tasks = client.get("/api/dsh/tasks?session=private:u1").get_json()["tasks"]

        assert len(tasks) == 1
        assert tasks[0]["session_key"] == "private:u1"

    def test_limit_is_capped_and_reported(self, db, client, monkeypatch):
        import main

        _row(db, "ok")
        monkeypatch.setattr(main, "db", db)

        assert client.get("/api/dsh/tasks?limit=999").get_json()["filters"]["limit"] == 100


class TestDshDetailFrontendSafety:
    def _js(self) -> str:
        from pathlib import Path

        return (Path(__file__).resolve().parents[1] / "KirikoBot/static/js/app.js").read_text(
            encoding="utf-8",
        )

    def test_user_content_is_written_with_textcontent(self):
        js = self._js()
        assert "v.textContent = value" in js, "arguments/result 必须用 textContent 渲染"
        assert "console.log(t.arguments" not in js
        assert "console.log(t.result" not in js

    def test_rows_use_event_delegation_not_inline_onclick(self):
        js = self._js()
        assert "row.dataset.taskId" in js
        assert "box.addEventListener('click'" in js
        assert "onclick=\"toggleDsh" not in js
