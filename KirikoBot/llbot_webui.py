"""Same-origin bridge to the LLBot WebUI API.

The LLBot WebUI (React SPA, port 3080) guards every ``/api/*`` call with

    x-webui-token: sha256(<plaintext password>)

where the plaintext password is stored in ``llbot_config/webui_token.txt``.
This module exposes that API under ``/llbot-api/*`` on the KirikoBot dashboard
origin, injecting the hashed token server-side, so the dashboard can render
LLBot status / login / logs natively without the operator logging in twice.

Everything is read-through: we never cache responses, and the token is never
sent to the browser.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Iterator

import requests
from flask import Blueprint, Response, jsonify, request

from config import Config

logger = logging.getLogger(__name__)

llbot_bp = Blueprint("llbot_bridge", __name__)

# Streaming endpoints (SSE) must not be buffered or given a short read timeout.
_STREAM_HINTS = ("/stream", "/events")
_CONNECT_TIMEOUT = 5
_READ_TIMEOUT = 20
_STREAM_READ_TIMEOUT = 600


def resolve_password() -> str | None:
    """Plaintext WebUI password: explicit env first, then the token file."""
    if Config.LLBOT_WEBUI_TOKEN and Config.LLBOT_WEBUI_TOKEN.strip():
        return Config.LLBOT_WEBUI_TOKEN.strip()
    for path in Config.LLBOT_TOKEN_PATHS:
        try:
            with open(path, encoding="utf-8") as fh:
                value = fh.read().strip()
        except OSError:
            continue
        if value:
            return value
    return None


def auth_headers() -> dict[str, str] | None:
    """Headers that satisfy LLBot's auth middleware, or None if unconfigured."""
    password = resolve_password()
    if not password:
        return None
    digest = hashlib.sha256(password.encode("utf-8")).hexdigest()
    return {"x-webui-token": digest}


def _upstream(path: str) -> str:
    return f"{Config.LLBOT_WEBUI_URL.rstrip('/')}/api/{path.lstrip('/')}"


def _is_stream(path: str) -> bool:
    return any(hint in path for hint in _STREAM_HINTS)


def _relay(resp: requests.Response) -> Iterator[bytes]:
    """Forward an upstream SSE / chunked body until the client goes away."""
    try:
        for chunk in resp.iter_content(chunk_size=None):
            if chunk:
                yield chunk
    except requests.RequestException:
        logger.debug("LLBot stream interrupted", exc_info=True)
    finally:
        resp.close()


@llbot_bp.route("/llbot-api", methods=["GET"], strict_slashes=False)
def llbot_bridge_status():
    """Health of the bridge itself, used by the dashboard footer/status pill."""
    headers = auth_headers()
    if headers is None:
        return jsonify({
            "ok": False,
            "reason": "no-password",
            "message": "未找到 LLBot WebUI 密码（llbot_config/webui_token.txt）",
        })
    try:
        resp = requests.get(_upstream("login-info"), headers=headers, timeout=_CONNECT_TIMEOUT)
        payload = resp.json()
    except (requests.RequestException, ValueError):
        return jsonify({"ok": False, "reason": "unreachable",
                        "message": f"无法连接 {Config.LLBOT_WEBUI_URL}"})
    if resp.status_code == 403:
        return jsonify({"ok": False, "reason": "bad-password",
                        "message": "LLBot WebUI 密码不匹配，请检查 webui_token.txt"})
    return jsonify({"ok": bool(payload.get("success")), "reason": "ready",
                    "message": "已连接", "data": payload.get("data")})


@llbot_bp.route("/llbot-api/<path:sub>", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def llbot_proxy(sub: str):
    """Transparent, token-injecting proxy for LLBot's ``/api/<sub>``."""
    headers = auth_headers()
    if headers is None:
        return jsonify({"success": False,
                        "message": "未找到 LLBot WebUI 密码，请在 .env 配置 LLBOT_WEBUI_TOKEN"}), 503

    stream = _is_stream(sub)
    kwargs: dict = {
        "headers": headers,
        "params": request.args.to_dict(flat=True),
        "timeout": (_CONNECT_TIMEOUT, _STREAM_READ_TIMEOUT if stream else _READ_TIMEOUT),
        "stream": stream,
    }
    body = request.get_data()
    if body and request.method in ("POST", "PUT", "PATCH", "DELETE"):
        kwargs["data"] = body
        headers["Content-Type"] = request.content_type or "application/json"

    try:
        resp = requests.request(request.method, _upstream(sub), **kwargs)
    except requests.Timeout:
        return jsonify({"success": False, "message": "LLBot WebUI 响应超时"}), 504
    except requests.RequestException:
        logger.warning("LLBot proxy failed for /api/%s", sub, exc_info=True)
        return jsonify({"success": False, "message": "无法连接 LLBot WebUI"}), 502

    if stream:
        # NB: never set hop-by-hop headers (Connection/Transfer-Encoding) here —
        # WSGI rejects them, and waitress manages the socket itself.
        return Response(
            _relay(resp),
            status=resp.status_code,
            content_type=resp.headers.get("Content-Type", "text/event-stream"),
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return Response(
        resp.content,
        status=resp.status_code,
        content_type=resp.headers.get("Content-Type", "application/json"),
    )
