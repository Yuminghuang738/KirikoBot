"""conversation_summaries 表结构、唯一约束、高水位与保留策略。"""
from __future__ import annotations

import sqlite3

import pytest

from database_manager import DatabaseManager

EXPECTED_COLUMNS = [
    "id", "user_id", "scope_id", "group_id",
    "from_history_id", "to_history_id", "summary", "facts_json",
    "msg_count", "model", "created_at",
]


def _count(db: DatabaseManager) -> int:
    return db.fetch_data("SELECT COUNT(*) FROM conversation_summaries")[0][0]


def test_table_has_expected_columns(db):
    cols = [row[1] for row in db.fetch_data("PRAGMA table_info(conversation_summaries)")]
    assert cols == EXPECTED_COLUMNS


def test_unique_constraint_keeps_one_row(db):
    assert db.save_summary("u1", "g1", "g1", 1, 24, "第一版") is True
    assert db.save_summary("u1", "g1", "g1", 1, 24, "重复") is False
    assert _count(db) == 1


def test_group_and_private_scopes_do_not_collide(db):
    assert db.save_summary("u1", "g1", "g1", 1, 24, "群") is True
    assert db.save_summary("u1", "private", None, 1, 24, "私聊") is True
    assert _count(db) == 2


def test_high_water_and_latest_summary(db):
    assert db.get_summary_high_water("u1", "g1") == 0
    assert db.get_latest_summary("u1", "g1") is None

    db.save_summary("u1", "g1", "g1", 1, 24, "旧")
    db.save_summary("u1", "g1", "g1", 25, 48, "新", facts_json='["f1"]')

    assert db.get_summary_high_water("u1", "g1") == 48
    latest = db.get_latest_summary("u1", "g1")
    assert latest["summary"] == "新"
    assert latest["facts_json"] == '["f1"]'
    assert len(db.list_recent_summaries("u1", "g1")) == 2


def test_prune_keeps_newest_rows_per_scope(db):
    for i in range(8):
        db.save_summary("u1", "g1", "g1", i * 24 + 1, (i + 1) * 24, f"s{i}")
    db.save_summary("u2", "g1", "g1", 1, 24, "别的用户")

    removed = db.prune_conversation_summaries(keep=3)

    assert removed == 5
    assert _count(db) == 4
    kept = [r["summary"] for r in db.list_recent_summaries("u1", "g1", limit=10)]
    assert kept == ["s7", "s6", "s5"]


def test_prune_by_cutoff(db):
    db.save_summary("u1", "g1", "g1", 1, 24, "old")
    db.execute_action(
        "UPDATE conversation_summaries SET created_at = '2000-01-01 00:00:00'",
    )
    removed = db.prune_conversation_summaries(cutoff="2020-01-01 00:00:00")
    assert removed == 1
    assert _count(db) == 0


def _seed_history(db, uid="u1", gid="g1", n=10):
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        db.deposit_chat_history(role, uid, gid, f"msg{i}", "", "")
    return [r[0] for r in db.fetch_data(
        "SELECT id FROM history WHERE user_id = ? ORDER BY id", (uid,),
    )]


def test_fetch_history_window_and_pending(db):
    ids = _seed_history(db, n=10)

    window = db.fetch_history_window("u1", "g1", 4)
    assert [r["id"] for r in window] == ids[-4:]
    assert window[-1]["content"] == "msg9"

    pending = db.fetch_pending_history("u1", "g1", ids[4], limit=10)
    assert [r["id"] for r in pending] == ids[5:]


def test_private_scope_uses_null_group_id(db):
    _seed_history(db, uid="u1", gid=None, n=4)
    window = db.fetch_history_window("u1", "private", 10)
    assert len(window) == 4
    assert db.fetch_history_window("u1", "g1", 10) == []


def test_takeout_chat_history_limit_param(db):
    _seed_history(db, n=10)

    assert len(db.takeout_chat_history("u1", "g1")) == 10      # 默认上限 40，行为不变
    assert len(db.takeout_chat_history("u1", "g1", limit=3)) == 3
    # limit 参数返回的是最近 3 条，仍按旧→新排序
    rows = db.takeout_chat_history("u1", "g1", limit=3)
    assert rows[-1][1] == "msg9"


def test_maintenance_prunes_old_summaries(db):
    import maintenance_service

    db.save_summary("u1", "g1", "g1", 1, 24, "很老的摘要")
    db.execute_action(
        "UPDATE conversation_summaries SET created_at = '2000-01-01 00:00:00'",
    )

    deleted = maintenance_service.prune_old_data(db, days=1)

    assert deleted.get("conversation_summaries") == 1
    assert _count(db) == 0
