"""主链路记忆开关：关闭时 16 条原文、开启时 24 条+摘要、落库后异步摘要。"""
from __future__ import annotations

import memory_service
import main


class Robot:
    user_id = "u1"
    group_id = "g1"
    msg_type = "group"


def test_memory_off_keeps_plain_history(monkeypatch):
    monkeypatch.setattr(main.feature_gate, "is_enabled", lambda *args: False)
    calls = []
    monkeypatch.setattr(
        main, "_load_history",
        lambda uid, gid: calls.append((uid, gid)) or [{"role": "user", "content": "旧"}],
    )
    monkeypatch.setattr(
        memory_service, "load_context",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("关闭时不应查摘要")),
    )

    history, summary, memory_on = main._memory_context(Robot())

    assert memory_on is False
    assert summary == ""
    assert history == [{"role": "user", "content": "旧"}]
    assert calls == [("u1", "g1")]


def test_memory_on_uses_layered_context(monkeypatch):
    monkeypatch.setattr(main.feature_gate, "is_enabled", lambda *args: True)
    monkeypatch.setattr(
        memory_service, "load_context",
        lambda db, uid, gid: ([{"role": "user", "content": "y"}], "更早的摘要"),
    )

    history, summary, memory_on = main._memory_context(Robot())

    assert memory_on is True
    assert summary == "更早的摘要"
    assert history == [{"role": "user", "content": "y"}]


def test_load_context_failure_falls_back_to_plain_history(monkeypatch):
    monkeypatch.setattr(main.feature_gate, "is_enabled", lambda *args: True)
    monkeypatch.setattr(
        memory_service, "load_context",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")),
    )
    monkeypatch.setattr(main, "_load_history", lambda uid, gid: [{"role": "user", "content": "x"}])

    history, summary, memory_on = main._memory_context(Robot())

    assert memory_on is False
    assert summary == ""
    assert history == [{"role": "user", "content": "x"}]


def test_memory_flag_requires_global_switch(monkeypatch):
    monkeypatch.setattr(main.Config, "MEMORY_LAYERS_ENABLED", False)
    monkeypatch.setattr(main.feature_gate, "is_enabled", lambda *args: True)
    monkeypatch.setattr(main, "_load_history", lambda uid, gid: [])

    _, summary, memory_on = main._memory_context(Robot())

    assert memory_on is False and summary == ""


def test_schedule_summary_submits_once():
    class Executor:
        def __init__(self):
            self.calls = []

        def submit(self, fn, *args):
            self.calls.append((fn, args))

    executor = Executor()
    main._schedule_memory_summary(executor, Robot(), True)

    assert len(executor.calls) == 1
    fn, args = executor.calls[0]
    assert fn is memory_service.maybe_summarize
    assert args == (main.db, "u1", "g1", True)


def test_schedule_summary_swallows_submit_errors():
    class BrokenExecutor:
        def submit(self, *args):
            raise RuntimeError("pool closed")

    main._schedule_memory_summary(BrokenExecutor(), Robot(), True)  # 不抛


def test_schedule_summary_still_called_when_disabled():
    """关闭时也提交：maybe_summarize 自己会立即返回，保持调用点统一。"""
    class Executor:
        def __init__(self):
            self.calls = []

        def submit(self, fn, *args):
            self.calls.append((fn, args))

    executor = Executor()
    main._schedule_memory_summary(executor, Robot(), False)

    assert executor.calls[0][1][-1] is False
