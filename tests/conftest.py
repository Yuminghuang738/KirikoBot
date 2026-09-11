"""Shared pytest fixtures.

The application modules live in ``KirikoBot/`` (that is the Docker build
context), so put it on sys.path. Nothing here may import ``main`` — doing so
starts the scheduler, the LLBot client and the worker pool.
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "KirikoBot")
sys.path.insert(0, APP_DIR)

# Config.validate() runs at import time and requires these.
os.environ.setdefault("ROBOT_QQ", "10000")
os.environ.setdefault("ONEBOT_API", "http://localhost:3000")
os.environ.setdefault("ONEBOT_TOKEN", "test-token")
os.environ.setdefault("DEEPSEEK_TOKEN", "sk-test")
os.environ.setdefault("GROUP_ROLE", "测试角色")
os.environ.setdefault("PRIVATE_ROLE", "测试角色")
os.environ.setdefault("TAROT_ROLE", "测试角色")


@pytest.fixture()
def db_file(tmp_path):
    """A fresh, schema-initialised SQLite file."""
    from database_manager import DatabaseManager

    path = str(tmp_path / "test.db")
    DatabaseManager(path)
    return path


@pytest.fixture()
def db(db_file):
    from database_manager import DatabaseManager

    return DatabaseManager(db_file)


def make_legacy_profile_db(path: str) -> None:
    """Build a user_profiles table with the OLD (buggy) UNIQUE(user_id) schema."""
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE user_profiles(
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      TEXT NOT NULL UNIQUE,
            group_id     TEXT NOT NULL,
            user_name    TEXT NOT NULL,
            profile_json TEXT NOT NULL DEFAULT '{}',
            message_count INTEGER DEFAULT 0,
            last_updated DATETIME DEFAULT (datetime('now', 'localtime'))
        )"""
    )
    conn.execute(
        "INSERT INTO user_profiles (user_id, group_id, user_name, profile_json, message_count) "
        "VALUES ('u1', 'g1', '小明', '{\"personality\":\"开朗\"}', 30)"
    )
    conn.execute("CREATE INDEX idx_up_user ON user_profiles(user_id, group_id)")
    conn.commit()
    conn.close()
