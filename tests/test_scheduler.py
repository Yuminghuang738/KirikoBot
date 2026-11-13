"""BotScheduler：提醒触发、群开关、每日重排与线程生命周期。"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta

import pytest

import scheduler as scheduler_module
from scheduler import BotScheduler


class FakeDB:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.actions = []
        self.fetch_error = None

    def fetch_data(self, sql, params):
        if self.fetch_error:
            raise self.fetch_error
        return self.rows

    def execute_action(self, sql, params):
        self.actions.append((sql, params))


class FakeLLBot:
    def __init__(self):
        self.group_msgs = []
        self.private_msgs = []

    def send_group_msg(self, group_id, message):
        self.group_msgs.append((group_id, message))
        return True

    def send_private_msg(self, user_id, message):
        self.private_msgs.append((user_id, message))
        return True


class FakeGate:
    def __init__(self, enabled=True):
        self.enabled = enabled
        self.calls = []

    def is_enabled(self, scope, target, feature):
        self.calls.append((scope, target, feature))
        return self.enabled


def _row(rid=1, uid="u1", gid="g1", uname="小明", content="喝水", remind="2026-01-01 00:00:00", daily=0):
    return (rid, uid, gid, uname, content, remind, daily)


def test_check_reminders_fires_each_due_row(monkeypatch):
    sched = BotScheduler(FakeDB([_row(1), _row(2, uid="u2")]), FakeLLBot())
    fired = []
    monkeypatch.setattr(sched, "_fire_reminder", lambda *args: fired.append(args))

    sched._check_reminders()

    assert len(fired) == 2
    assert fired[0][0] == 1


def test_check_reminders_swallows_query_errors():
    db = FakeDB()
    db.fetch_error = RuntimeError("db down")
    BotScheduler(db, FakeLLBot())._check_reminders()  # 不抛


def test_fire_reminder_group_sends_and_marks_fired():
    llbot = FakeLLBot()
    db = FakeDB()
    BotScheduler(db, llbot)._fire_reminder(1, "u1", "g1", "小明", "喝水", "2026-01-01 00:00:00")

    gid, payload = llbot.group_msgs[0]
    assert gid == "g1"
    assert "喝水" in str(payload)
    assert db.actions[-1][0] == "UPDATE reminders SET fired=1 WHERE id=?"
    assert db.actions[-1][1] == (1,)


def test_fire_reminder_private_sends_to_user():
    llbot = FakeLLBot()
    BotScheduler(FakeDB(), llbot)._fire_reminder(2, "u2", None, "小红", "睡觉", "2026-01-01 00:00:00")

    uid, payload = llbot.private_msgs[0]
    assert uid == "u2" and "睡觉" in str(payload)


def test_fire_reminder_skips_when_group_disabled():
    llbot = FakeLLBot()
    db = FakeDB()
    gate = FakeGate(enabled=False)
    BotScheduler(db, llbot, gate)._fire_reminder(3, "u1", "g1", "小明", "喝水", "2026-01-01 00:00:00")

    assert llbot.group_msgs == []
    assert db.actions[-1][0] == "UPDATE reminders SET fired=1 WHERE id=?"
    assert gate.calls == [("group", "g1", "reminder")]


def test_fire_reminder_skips_when_user_disabled():
    llbot = FakeLLBot()
    db = FakeDB()
    gate = FakeGate(enabled=False)
    BotScheduler(db, llbot, gate)._fire_reminder(4, "u1", None, "小明", "喝水", "2026-01-01 00:00:00")

    assert llbot.private_msgs == []
    assert db.actions
    assert gate.calls == [("user", "u1", "reminder")]


def test_fire_reminder_sends_when_gate_enabled():
    llbot = FakeLLBot()
    BotScheduler(FakeDB(), llbot, FakeGate(enabled=True))._fire_reminder(
        5, "u1", "g1", "小明", "喝水", "2026-01-01 00:00:00",
    )
    assert len(llbot.group_msgs) == 1


def test_finalize_daily_reschedules_to_tomorrow():
    db = FakeDB()
    BotScheduler(db, FakeLLBot())._finalize_reminder(7, "2026-01-01 08:30:00", 1)

    sql, params = db.actions[-1]
    assert "remind_time=?" in sql
    expected = (datetime.strptime("2026-01-01 08:30:00", "%Y-%m-%d %H:%M:%S") + timedelta(days=1))
    assert params[0] == expected.strftime("%Y-%m-%d %H:%M:%S")
    assert params[1] == 7


def test_finalize_daily_bad_time_falls_back_to_fired():
    db = FakeDB()
    BotScheduler(db, FakeLLBot())._finalize_reminder(8, "not-a-time", 1)

    assert db.actions[-1] == ("UPDATE reminders SET fired=1 WHERE id=?", (8,))


def test_start_is_idempotent_and_stop_ends_thread(monkeypatch):
    sched = BotScheduler(FakeDB(), FakeLLBot())
    sched._loop = lambda: None  # 不真正跑调度循环

    sched.start()
    first_thread = sched._thread
    sched.start()  # 第二次不应新建线程
    assert sched._thread is first_thread
    assert isinstance(first_thread, threading.Thread)

    first_thread.join(timeout=2)
    sched.stop()
    assert sched._running is False


def test_loop_survives_check_errors(monkeypatch):
    sched = BotScheduler(FakeDB(), FakeLLBot())
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        raise RuntimeError("loop error")

    monkeypatch.setattr(sched, "_check_reminders", boom)
    monkeypatch.setattr(scheduler_module.time, "sleep", lambda _s: setattr(sched, "_running", False))
    sched._running = True
    sched._loop()
    assert calls["n"] == 1
