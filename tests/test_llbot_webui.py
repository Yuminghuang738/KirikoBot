"""LLBot WebUI 同源桥：密码解析、token 注入、代理与 SSE 转发。"""
from __future__ import annotations

import hashlib

import pytest
import requests
from flask import Flask

import llbot_webui
from config import Config
from llbot_webui import llbot_bp


class FakeResp:
    def __init__(self, *, data=None, status=200, content=b"", headers=None, chunks=None, exc=None):
        self._data = data
        self.status_code = status
        self.content = content
        self.headers = headers or {"Content-Type": "application/json"}
        self._chunks = chunks or []
        self._exc = exc
        self.closed = False

    def json(self):
        return self._data

    def iter_content(self, chunk_size=None):
        for chunk in self._chunks:
            yield chunk
        if self._exc:
            raise self._exc

    def close(self):
        self.closed = True


@pytest.fixture()
def app():
    app = Flask(__name__)
    app.register_blueprint(llbot_bp)
    return app


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def configured(monkeypatch, tmp_path):
    token_file = tmp_path / "webui_token.txt"
    token_file.write_text("secret-pass", encoding="utf-8")
    monkeypatch.setattr(Config, "LLBOT_WEBUI_TOKEN", None)
    monkeypatch.setattr(Config, "LLBOT_TOKEN_PATHS", [str(token_file)])
    monkeypatch.setattr(Config, "LLBOT_WEBUI_URL", "http://llbot:3080/")
    return token_file


# ── password / headers ──────────────────────────────────────────

def test_resolve_password_prefers_env(monkeypatch, tmp_path):
    monkeypatch.setattr(Config, "LLBOT_WEBUI_TOKEN", " from-env ")
    monkeypatch.setattr(Config, "LLBOT_TOKEN_PATHS", [str(tmp_path / "missing")])
    assert llbot_webui.resolve_password() == "from-env"


def test_resolve_password_reads_token_file(configured):
    assert llbot_webui.resolve_password() == "secret-pass"


def test_resolve_password_none_when_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(Config, "LLBOT_WEBUI_TOKEN", None)
    monkeypatch.setattr(Config, "LLBOT_TOKEN_PATHS", [str(tmp_path / "nope")])
    assert llbot_webui.resolve_password() is None


def test_auth_headers_hashes_password(configured):
    headers = llbot_webui.auth_headers()
    assert headers == {"x-webui-token": hashlib.sha256(b"secret-pass").hexdigest()}


def test_auth_headers_none_without_password(monkeypatch, tmp_path):
    monkeypatch.setattr(Config, "LLBOT_WEBUI_TOKEN", None)
    monkeypatch.setattr(Config, "LLBOT_TOKEN_PATHS", [str(tmp_path / "nope")])
    assert llbot_webui.auth_headers() is None


def test_upstream_and_stream_detection():
    assert llbot_webui._upstream("/login-info") == "http://llbot:3080/api/login-info"
    assert llbot_webui._is_stream("v1/stream") is True
    assert llbot_webui._is_stream("v1/events?x=1") is True
    assert llbot_webui._is_stream("login-info") is False


# ── bridge status ───────────────────────────────────────────────

def test_bridge_status_without_password(client, monkeypatch, tmp_path):
    monkeypatch.setattr(Config, "LLBOT_WEBUI_TOKEN", None)
    monkeypatch.setattr(Config, "LLBOT_TOKEN_PATHS", [str(tmp_path / "nope")])
    body = client.get("/llbot-api").get_json()
    assert body["ok"] is False and body["reason"] == "no-password"


def test_bridge_status_unreachable(client, configured, monkeypatch):
    monkeypatch.setattr(
        llbot_webui.requests, "get",
        lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("down")),
    )
    body = client.get("/llbot-api").get_json()
    assert body["reason"] == "unreachable"


def test_bridge_status_bad_password(client, configured, monkeypatch):
    monkeypatch.setattr(llbot_webui.requests, "get", lambda *a, **k: FakeResp(status=403, data={}))
    body = client.get("/llbot-api").get_json()
    assert body["reason"] == "bad-password"


def test_bridge_status_ready(client, configured, monkeypatch):
    resp = FakeResp(data={"success": True, "data": {"name": "llbot"}})
    monkeypatch.setattr(llbot_webui.requests, "get", lambda *a, **k: resp)
    body = client.get("/llbot-api").get_json()
    assert body["ok"] is True and body["reason"] == "ready"
    assert body["data"] == {"name": "llbot"}


# ── proxy ───────────────────────────────────────────────────────

def test_proxy_requires_password(client, monkeypatch, tmp_path):
    monkeypatch.setattr(Config, "LLBOT_WEBUI_TOKEN", None)
    monkeypatch.setattr(Config, "LLBOT_TOKEN_PATHS", [str(tmp_path / "nope")])
    resp = client.get("/llbot-api/anything")
    assert resp.status_code == 503


def test_proxy_forwards_method_body_and_query(client, configured, monkeypatch):
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update(method=method, url=url, kwargs=kwargs)
        return FakeResp(content=b'{"ok":1}', headers={"Content-Type": "application/json"})

    monkeypatch.setattr(llbot_webui.requests, "request", fake_request)

    resp = client.post("/llbot-api/login?x=1", data=b'{"a":1}', content_type="application/json")

    assert resp.status_code == 200
    assert resp.data == b'{"ok":1}'
    assert captured["method"] == "POST"
    assert captured["url"] == "http://llbot:3080/api/login"
    assert captured["kwargs"]["params"] == {"x": "1"}
    assert captured["kwargs"]["data"] == b'{"a":1}'
    assert captured["kwargs"]["headers"]["x-webui-token"]


def test_proxy_timeout_returns_504(client, configured, monkeypatch):
    monkeypatch.setattr(
        llbot_webui.requests, "request",
        lambda *a, **k: (_ for _ in ()).throw(requests.Timeout()),
    )
    assert client.get("/llbot-api/x").status_code == 504


def test_proxy_connection_error_returns_502(client, configured, monkeypatch):
    monkeypatch.setattr(
        llbot_webui.requests, "request",
        lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError()),
    )
    assert client.get("/llbot-api/x").status_code == 502


def test_proxy_streams_sse(client, configured, monkeypatch):
    upstream = FakeResp(
        chunks=[b"data: one\n\n", b"data: two\n\n"],
        headers={"Content-Type": "text/event-stream"},
    )
    monkeypatch.setattr(llbot_webui.requests, "request", lambda *a, **k: upstream)

    resp = client.get("/llbot-api/v1/stream")

    assert resp.status_code == 200
    assert resp.data == b"data: one\n\ndata: two\n\n"
    assert resp.headers["Cache-Control"] == "no-cache"
    assert upstream.closed is True  # 流结束后关闭上游


def test_relay_swallows_request_exceptions():
    upstream = FakeResp(chunks=[b"x"], exc=requests.RequestException("boom"))
    assert list(llbot_webui._relay(upstream)) == [b"x"]
    assert upstream.closed is True
