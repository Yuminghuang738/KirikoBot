"""面板口令安全：不进日志、文件权限必须是 600。"""
from __future__ import annotations

import logging
import os
import stat

import pytest
from flask import Flask

import dashboard_auth
from config import Config


def _mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.fixture()
def password_file(tmp_path, monkeypatch):
    path = tmp_path / ".dashboard_password"
    monkeypatch.setattr(dashboard_auth, "_PASSWORD_FILE", str(path))
    monkeypatch.setattr(Config, "DASHBOARD_PASSWORD", None)
    monkeypatch.setattr(Config, "DASHBOARD_AUTH_ENABLED", True)
    return path


def test_generated_password_is_not_logged(password_file, caplog):
    app = Flask("test-dashboard-auth")
    with caplog.at_level(logging.INFO):
        dashboard_auth.init_app(app)

    password = password_file.read_text(encoding="utf-8").strip()
    assert password, "应生成并保存口令"
    assert password not in caplog.text
    assert "已保存到" in caplog.text
    assert _mode(password_file) == 0o600


def test_existing_loose_file_is_tightened_to_600(password_file):
    password_file.write_text("saved-password", encoding="utf-8")
    os.chmod(password_file, 0o777)

    assert dashboard_auth._load_password() == "saved-password"
    assert _mode(password_file) == 0o600


def test_chmod_failure_warns_but_still_returns_password(password_file, monkeypatch, caplog):
    password_file.write_text("saved-password", encoding="utf-8")

    def boom(*args, **kwargs):
        raise OSError("read-only fs")

    monkeypatch.setattr(dashboard_auth.os, "chmod", boom)
    with caplog.at_level(logging.WARNING):
        assert dashboard_auth._load_password() == "saved-password"
    assert "无法把" in caplog.text
    assert "600" in caplog.text


def test_env_password_never_touches_the_file(password_file, monkeypatch):
    monkeypatch.setattr(Config, "DASHBOARD_PASSWORD", "from-env")
    assert dashboard_auth._load_password() == "from-env"
    assert not password_file.exists()
