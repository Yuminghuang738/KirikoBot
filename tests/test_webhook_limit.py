"""R03：/webhook 在验签前限制请求体大小，未鉴权大包直接 413。"""
from __future__ import annotations

import main
from config import Config
from webhook_auth import expected_signature


def _client():
    return main.app.test_client()


def test_limit_is_one_megabyte():
    assert main.MAX_WEBHOOK_BODY == 1024 * 1024
    assert main.app.config["MAX_CONTENT_LENGTH"] == main.MAX_WEBHOOK_BODY


def test_oversized_body_is_rejected_before_hmac(monkeypatch):
    hmac_calls: list[int] = []

    def spy(raw: bytes) -> bool:
        hmac_calls.append(len(raw))
        return True

    monkeypatch.setattr(main, "_webhook_signature_ok", spy)
    body = b"x" * (2 * 1024 * 1024)

    resp = _client().post("/webhook", data=body, content_type="application/json")

    assert resp.status_code == 413
    assert resp.get_json()["status"] == "too_large"
    # 关键：验签函数一次都没被调用，请求体没有进内存处理链。
    assert hmac_calls == []


def test_small_valid_event_passes_through():
    body = b'{"post_type":"notice","notice_type":"notify","user_id":1}'
    token = Config.WEBHOOK_TOKEN
    assert token, "conftest 应设置 ONEBOT_TOKEN"
    resp = _client().post(
        "/webhook",
        data=body,
        content_type="application/json",
        headers={"X-Signature": expected_signature(token, body)},
    )
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ignored"


def test_small_event_with_bad_signature_is_still_403():
    resp = _client().post(
        "/webhook",
        data=b'{"post_type":"notice"}',
        content_type="application/json",
        headers={"X-Signature": "sha1=deadbeef"},
    )
    assert resp.status_code == 403
