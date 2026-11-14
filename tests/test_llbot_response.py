"""LLBot 调用是否真的成功：OneBot 失败常常也是 HTTP 200。

点歌事故里音乐卡片被 LLBot 拒绝（签名失败），但 `send` 只看了 status_code，
于是机器人以为发出去了、不回退。这里钉住「按响应体判断」。
"""
from __future__ import annotations

import pytest

from llbot_client import LLBotClient


class FakeResponse:
    def __init__(self, body):
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


@pytest.mark.parametrize("body", [
    {"status": "ok", "retcode": 0, "data": {"message_id": 1}},
    {"status": "async", "retcode": 1},
    {"data": {"message_id": 1}},          # 没有 status/retcode 的老格式
])
def test_success_bodies_body_are_ok(body):
    assert LLBotClient._response_ok(FakeResponse(body)) is True


@pytest.mark.parametrize("body", [
    {"status": "failed", "retcode": 100},
    {"status": "failed", "retcode": 0},
    {"status": "ok", "retcode": 1400, "wording": "签名音乐消息失败"},
    {"retcode": -1},
])
def test_failure_bodies_are_not_ok(body):
    assert LLBotClient._response_ok(FakeResponse(body)) is False


def test_unparseable_body_keeps_old_behaviour():
    assert LLBotClient._response_ok(FakeResponse(ValueError("no json"))) is True
    assert LLBotClient._response_ok(FakeResponse(["not", "a", "dict"])) is True


def test_failed_post_reports_false(monkeypatch):
    """HTTP 200 + retcode!=0 时 _post 必须返回 False（回退逻辑的前提）。"""
    client = LLBotClient("http://llbot:3001", "token")

    class Session:
        def post(self, url, json=None, timeout=None):
            return FakeResponse({"status": "failed", "retcode": 1400, "wording": "签名音乐消息失败"})

    monkeypatch.setattr(client, "_session", Session())
    assert client.send_group_msg("100", [{"type": "text", "data": {"text": "hi"}}]) is False
