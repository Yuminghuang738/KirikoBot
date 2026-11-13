"""私聊进入画像取样 + 分析并发上限。"""
from __future__ import annotations

import threading
import time

import pytest

from profile_service import ProfileService


def _seed(db, uid="u1", gid="g1", group_n=3, private_n=3):
    for i in range(group_n):
        db.record_group_message(gid, uid, "小明", f"群消息{i}")
    for i in range(private_n):
        db.deposit_chat_history("user", uid, None, f"私聊{i}", "", "")


def test_get_user_messages_includes_private(db):
    _seed(db)
    contents = [row[0] for row in db.get_user_messages("u1", limit=50)]

    assert "群消息0" in contents
    assert "私聊0" in contents


def test_get_user_messages_dedupes_same_minute(db):
    db.record_group_message("g1", "u1", "小明", "一起去吃饭")
    db.deposit_chat_history("user", "u1", None, "一起去吃饭", "", "")

    rows = db.get_user_messages("u1", limit=50)

    assert [row[0] for row in rows].count("一起去吃饭") == 1


def test_count_user_messages_matches_merged_samples(db):
    _seed(db, group_n=4, private_n=5)

    rows = db.get_user_messages("u1", limit=100)
    assert db.count_user_messages("u1") == len(rows)


def test_private_messages_are_not_double_counted(db):
    db.record_group_message("g1", "u1", "小明", "同一句话")
    db.deposit_chat_history("user", "u1", None, "同一句话", "", "")
    db.deposit_chat_history("assistant", "u1", None, "回答", "", "")

    assert db.count_user_messages("u1") == 1


def test_should_analyze_triggers_on_private_growth(db, monkeypatch):
    service = ProfileService()
    monkeypatch.setattr(service, "_is_bot", lambda uid: False)
    monkeypatch.setattr(db, "get_user_profile", lambda uid: {"message_count": 0})

    for i in range(service.REANALYZE_GAP + 1):
        db.deposit_chat_history("user", "u1", None, f"私聊{i}", "", "")

    assert service.should_analyze(db, "u1") is True


def test_analyze_user_concurrency_limit(monkeypatch):
    service = ProfileService()
    monkeypatch.setattr(service, "_is_bot", lambda uid: False)
    entered: list[str] = []
    release = threading.Event()
    lock = threading.Lock()

    def fake_do_analyze(db, user_id, user_name, group_id=""):
        with lock:
            entered.append(user_id)
        release.wait(timeout=3)
        return {}

    monkeypatch.setattr(service, "_do_analyze", fake_do_analyze)

    threads = [
        threading.Thread(target=service.analyze_user, args=(None, f"u{i}", f"n{i}"))
        for i in range(2)
    ]
    for thread in threads:
        thread.start()

    deadline = time.time() + 3
    while len(entered) < 2 and time.time() < deadline:
        time.sleep(0.02)

    # 2 个名额已被占满，第三个必须立刻返回 None（不等锁）
    assert len(entered) == 2
    assert service.analyze_user(None, "u_blocked", "n_blocked") is None

    release.set()
    for thread in threads:
        thread.join(timeout=3)


def test_extract_memories_prepends_summary(db, monkeypatch):
    captured = {}

    def fake_quick_chat(system, convo, **kwargs):
        captured["convo"] = convo
        return '{"memories":[]}'

    monkeypatch.setattr("profile_service.quick_chat", fake_quick_chat)
    db.deposit_chat_history("user", "u1", "g1", "最近聊的事", "", "")
    db.save_summary("u1", "g1", "g1", 1, 24, "用户答应帮他听录音")

    ProfileService().extract_memories(db, "u1", "小明")

    assert captured["convo"].startswith("摘要（较早的对话）：")
    assert "用户答应帮他听录音" in captured["convo"]
    assert "最近聊的事" in captured["convo"]
