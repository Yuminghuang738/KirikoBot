"""HTTP Basic auth for the KirikoBot dashboard.

The panel is far more powerful than it looks: it can delete a group and all of
its data, push messages to every QQ group, read every chat log, and (through
the LLBot bridge) drive the QQ account itself. It therefore must not be
reachable without a credential.

Design notes
------------
* HTTP Basic is used deliberately: it needs no session store, and the browser
  replays the credentials for same-origin subresources (stickers) and
  ``EventSource`` streams, which a naive cookie login would have to special-case.
* The OneBot webhook is exempt: it carries its own HMAC signature (see
  ``main._webhook_signature_ok``) and cannot present dashboard credentials.
* Basic auth over plain HTTP is only as private as the network. Put the panel
  behind HTTPS (or a reverse proxy that terminates TLS) before exposing it.
"""
from __future__ import annotations

import hmac
import logging
import os
import secrets

from flask import Response, request

from config import Config

logger = logging.getLogger(__name__)

# Endpoint names exempt from dashboard auth:
#   receive  — the OneBot webhook (carries its own HMAC signature)
#   static   — Flask's static file endpoint
#   healthz  — container healthcheck; must stay reachable without credentials
_WEBHOOK_ENDPOINTS = {"receive", "static", "healthz"}
_PASSWORD_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".dashboard_password")


def _load_password() -> str:
    """Configured password, else a generated one persisted to a local file."""
    if Config.DASHBOARD_PASSWORD and Config.DASHBOARD_PASSWORD.strip():
        return Config.DASHBOARD_PASSWORD.strip()
    try:
        if os.path.isfile(_PASSWORD_FILE):
            with open(_PASSWORD_FILE, encoding="utf-8") as fh:
                saved = fh.read().strip()
            if saved:
                return saved
    except OSError:
        logger.warning("Could not read %s", _PASSWORD_FILE, exc_info=True)

    generated = secrets.token_urlsafe(12)
    try:
        with open(_PASSWORD_FILE, "w", encoding="utf-8") as fh:
            fh.write(generated)
        os.chmod(_PASSWORD_FILE, 0o600)
    except OSError:
        logger.warning("Could not persist the generated dashboard password", exc_info=True)
    return generated


def init_app(app) -> None:
    """Install the auth gate on a Flask app."""
    if not Config.DASHBOARD_AUTH_ENABLED:
        logger.warning(
            "DASHBOARD_AUTH=0 —— 面板未启用鉴权，任何能访问本端口的人都能删除群数据、"
            "冒充机器人发言并操作 QQ 账号。请勿在不可信网络中这样运行。"
        )
        return

    password = _load_password()
    user = Config.DASHBOARD_USER

    logger.info("面板已启用访问鉴权（用户名 %s）", user)
    if not (Config.DASHBOARD_PASSWORD and Config.DASHBOARD_PASSWORD.strip()):
        logger.info("面板密码（自动生成，保存在 %s）：%s", _PASSWORD_FILE, password)

    @app.before_request
    def _require_dashboard_auth():
        if request.endpoint in _WEBHOOK_ENDPOINTS:
            return None

        supplied = request.authorization
        ok_user = bool(supplied) and hmac.compare_digest(supplied.username or "", user)
        ok_pass = bool(supplied) and hmac.compare_digest(supplied.password or "", password)
        if ok_user and ok_pass:
            return None

        return Response(
            "需要登录才能访问 KirikoBot 控制台",
            401,
            {"WWW-Authenticate": 'Basic realm="KirikoBot", charset="UTF-8"'},
        )
