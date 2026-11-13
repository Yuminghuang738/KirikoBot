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
#   receive              — the OneBot webhook (carries its own HMAC signature)
#   static               — Flask's static file endpoint
#   healthz              — container healthcheck; must stay reachable without credentials
#   download_shared_file — 公共产物下载直链（供群友直接点击下载生成的乐谱/文件）
_WEBHOOK_ENDPOINTS = {"receive", "static", "healthz", "download_shared_file"}
_PASSWORD_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".dashboard_password")


def _tighten_permissions(path: str) -> None:
    """把口令文件收紧到 600。老部署里可能是 777，chmod 只在新建时做过。"""
    try:
        os.chmod(path, 0o600)
    except OSError:
        logger.warning("无法把 %s 的权限收紧到 600", path, exc_info=True)


def _load_password() -> str:
    """Configured password, else a generated one persisted to a local file."""
    if Config.DASHBOARD_PASSWORD and Config.DASHBOARD_PASSWORD.strip():
        return Config.DASHBOARD_PASSWORD.strip()
    try:
        if os.path.isfile(_PASSWORD_FILE):
            with open(_PASSWORD_FILE, encoding="utf-8") as fh:
                saved = fh.read().strip()
            if saved:
                # 已存在的文件也要收紧权限，不能只依赖首次创建时的 chmod。
                _tighten_permissions(_PASSWORD_FILE)
                return saved
    except OSError:
        logger.warning("Could not read %s", _PASSWORD_FILE, exc_info=True)

    generated = secrets.token_urlsafe(12)
    try:
        # 先按 600 创建，再写入，避免出现短暂的 644/777 窗口。
        fd = os.open(_PASSWORD_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(generated)
        _tighten_permissions(_PASSWORD_FILE)
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
        # 口令绝不能进日志：日志会进 docker logs、SSE 面板和备份。
        logger.info("面板密码已保存到 %s（出于安全不在日志中显示）", _PASSWORD_FILE)

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
