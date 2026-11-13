"""AiServer / quick_chat / vision 的请求构造、错误分支与埋点。"""
from __future__ import annotations

import json
import time

import pytest
import requests
from requests.adapters import HTTPAdapter

import ai_server
from ai_server import AiServer, quick_chat
from config import Config


class FakeResponse:
    def __init__(self, data=None, *, ok=True, status_code=200, text="", json_exc=None):
        self.data = data
        self.ok = ok
        self.status_code = status_code
        self.text = text
        self.json_exc = json_exc

    def raise_for_status(self):
        if not self.ok:
            raise requests.HTTPError(response=self)

    def json(self):
        if self.json_exc is not None:
            raise self.json_exc
        return self.data


class FakeSession:
    def __init__(self, response=None, exc=None):
        self.response = response
        self.exc = exc
        self.calls: list[tuple[str, dict]] = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.exc is not None:
            raise self.exc
        return self.response


@pytest.fixture()
def records(monkeypatch):
    captured: list[dict] = []

    def fake_record(**kwargs):
        captured.append(kwargs)

    monkeypatch.setattr(ai_server.ai_metrics, "record", fake_record)
    return captured


@pytest.fixture()
def use_session(monkeypatch):
    def install(session):
        monkeypatch.setattr(AiServer, "_create_session", staticmethod(lambda: session))
        return session

    return install


def _message_response(content="你好", *, reasoning="", tool_calls=None):
    message = {"content": content}
    if reasoning:
        message["reasoning_content"] = reasoning
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return FakeResponse({"choices": [{"message": message}]})


# ── quick_chat ────────────────────────────────────────────────────

def test_quick_chat_success(monkeypatch, records):
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update(json=json, timeout=timeout)
        return FakeResponse({"choices": [{"message": {"content": "  你好  "}}]})

    monkeypatch.setattr(ai_server.requests, "post", fake_post)

    assert quick_chat("系统", "问题", max_tokens=123, temperature=0.2) == "你好"
    assert captured["json"]["max_tokens"] == 123
    assert captured["json"]["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in captured["json"]
    assert records and records[0]["success"] is True
    assert records[0]["kind"] == "quick"


def test_quick_chat_thinking_adds_reasoning_effort(monkeypatch, records):
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update(json=json)
        return FakeResponse({"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(ai_server.requests, "post", fake_post)
    quick_chat("s", "u", thinking=True, source="judge")

    assert captured["json"]["thinking"] == {"type": "enabled"}
    assert captured["json"]["reasoning_effort"] == Config.DEEPSEEK_REASONING_EFFORT
    assert records[0]["source"] == "judge"


def test_quick_chat_without_system_prompt(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update(json=json)
        return FakeResponse({"choices": [{"message": {"content": "x"}}]})

    monkeypatch.setattr(ai_server.requests, "post", fake_post)
    quick_chat("", "只有用户")
    assert captured["json"]["messages"] == [{"role": "user", "content": "只有用户"}]


def test_quick_chat_http_error_returns_none(monkeypatch, records):
    def fake_post(url, headers=None, json=None, timeout=None):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(ai_server.requests, "post", fake_post)

    assert quick_chat("s", "u") is None
    assert records[0]["success"] is False
    assert "ConnectionError" in records[0]["error"]


def test_quick_chat_bad_shape_returns_none(monkeypatch, records):
    monkeypatch.setattr(
        ai_server.requests, "post",
        lambda *a, **k: FakeResponse({"choices": []}),
    )

    assert quick_chat("s", "u") is None
    # success 埋点在解析 choices 之前就记了，解析失败会再补一条失败记录。
    assert records[-1]["success"] is False


# ── session / metrics ─────────────────────────────────────────────

def test_create_session_mounts_retrying_adapters():
    session = AiServer._create_session()
    adapter = session.get_adapter("https://api.deepseek.com")
    assert isinstance(adapter, HTTPAdapter)
    assert adapter.max_retries.total == Config.MAX_RETRIES
    assert "POST" in adapter.max_retries.allowed_methods


def test_record_carries_attribution(records):
    server = AiServer("s", "u")
    server.source = "tarot"
    server.group_id = "g123"
    server._record("tarot", time.perf_counter(), success=False, error="boom")

    assert records[0]["source"] == "tarot"
    assert records[0]["group_id"] == "g123"
    assert records[0]["kind"] == "tarot"
    assert records[0]["success"] is False


# ── ai_request ────────────────────────────────────────────────────

def test_ai_request_success_parses_message(use_session, records):
    tool_calls = [{"id": "c1", "type": "function", "function": {"name": "x", "arguments": "{}"}}]
    session = use_session(FakeSession(_message_response("**加粗**", reasoning="想一想", tool_calls=tool_calls)))

    server = AiServer("系统", "问题", history_list=[{"role": "user", "content": "旧"}], tools=[{"t": 1}])
    server.ai_request()

    assert server.ai_message["tool_calls"] == tool_calls
    assert server.reasoning_content == "想一想"
    assert "**" not in server.ai_text and "加粗" in server.ai_text
    body = json.loads(session.calls[0][1]["data"])
    assert body["model"] == Config.DEEPSEEK_MODEL
    assert body["tools"] == [{"t": 1}]
    assert body["thinking"] == {"type": "disabled"}
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][-1] == {"content": "问题", "role": "user"}
    assert records[0]["kind"] == "chat" and records[0]["success"] is True


def test_ai_request_thinking_enabled_sends_effort(use_session, records):
    session = use_session(FakeSession(_message_response("ok")))
    server = AiServer("s", "u", thinking_type="enabled", reasoning_effort="high")
    server.ai_request()

    body = json.loads(session.calls[0][1]["data"])
    assert body["thinking"] == {"type": "enabled"}
    assert body["reasoning_effort"] == "high"


def test_ai_request_timeout_sets_apology(use_session, records):
    use_session(FakeSession(exc=requests.exceptions.Timeout()))
    server = AiServer("s", "u")
    server.ai_request()

    assert "思考时间" in server.ai_text
    assert records[0]["error"] == "Timeout"


def test_ai_request_connection_error_sets_apology(use_session, records):
    use_session(FakeSession(exc=requests.exceptions.ConnectionError("x")))
    server = AiServer("s", "u")
    server.ai_request()

    assert "网络连接" in server.ai_text
    assert records[0]["success"] is False


@pytest.mark.parametrize("status,expected", [
    (401, "配置有问题"),
    (429, "太频繁"),
    (500, "暂时不可用"),
    (503, "暂时不可用"),
    (418, "异常状态(418)"),
])
def test_ai_request_http_errors_map_to_messages(use_session, records, status, expected):
    resp = FakeResponse(ok=False, status_code=status, text="error body")
    use_session(FakeSession(resp))
    server = AiServer("s", "u")
    server.ai_request()

    assert expected in server.ai_text
    assert records[0]["success"] is False


def test_ai_request_non_json_response(use_session, records):
    use_session(FakeSession(FakeResponse(json_exc=ValueError("bad"))))
    server = AiServer("s", "u")
    server.ai_request()

    assert "异常数据" in server.ai_text
    assert records[0]["error"] == "non-JSON response"


def test_ai_request_unexpected_shape(use_session, records):
    use_session(FakeSession(FakeResponse({"error": {"message": "bad"}})))
    server = AiServer("s", "u")
    server.ai_request()

    assert "意外的数据格式" in server.ai_text
    assert records[0]["error"] == "unexpected response shape"


def test_ai_request_format_failure_falls_back_to_raw(use_session, records, monkeypatch):
    use_session(FakeSession(_message_response("原始内容")))
    monkeypatch.setattr(AiServer, "_format_for_qq", staticmethod(lambda text: (_ for _ in ()).throw(ValueError("boom"))))
    server = AiServer("s", "u")
    server.ai_request()

    assert server.ai_text == "原始内容"


def test_add_tool_result_appends():
    server = AiServer("s", "u")
    server.add_tool_result("c1", "结果")
    assert server.tool_results == [{"tool_call_id": "c1", "content": "结果"}]


# ── follow_up_request ─────────────────────────────────────────────

def test_follow_up_request_includes_tool_results(use_session, records):
    session = use_session(FakeSession(_message_response("整合后", reasoning="r2")))
    server = AiServer("系统", "问题")
    server.ai_message = {"content": "原始", "reasoning_content": "r1"}
    server.add_tool_result("c1", "tool result")
    calls = [{"id": "c1", "type": "function", "function": {"name": "x", "arguments": "{}"}}]

    server.follow_up_request(calls)

    body = json.loads(session.calls[0][1]["data"])
    roles = [m["role"] for m in body["messages"]]
    assert roles[-2] == "assistant" and roles[-1] == "tool"
    assert body["messages"][-2]["reasoning_content"] == "r1"
    assert body["messages"][-1]["tool_call_id"] == "c1"
    assert server.ai_text == "整合后"
    assert records[0]["kind"] == "followup"


def test_follow_up_request_http_error_clears_text(use_session, records):
    resp = FakeResponse(ok=False, status_code=500, text="boom")
    use_session(FakeSession(resp))
    server = AiServer("s", "u")
    server.ai_text = "旧的"

    server.follow_up_request([])

    assert server.ai_text == ""
    assert records[0]["success"] is False
    assert "HTTP 500" in records[0]["error"]


def test_follow_up_request_generic_error_clears_text(use_session, records):
    use_session(FakeSession(exc=RuntimeError("boom")))
    server = AiServer("s", "u")
    server.follow_up_request([])

    assert server.ai_text == ""
    assert records[0]["success"] is False


# ── vision ────────────────────────────────────────────────────────

def test_vision_disabled_returns_none(monkeypatch):
    monkeypatch.setattr(Config, "VISION_ENABLED", False)
    assert AiServer.vision_analyze("whatever.png") is None


def test_vision_local_file_success(use_session, records, monkeypatch, tmp_path):
    monkeypatch.setattr(Config, "VISION_ENABLED", True)
    image = tmp_path / "cat.png"
    image.write_bytes(b"PNGDATA")
    session = use_session(FakeSession(FakeResponse({"choices": [{"message": {"content": "一只猫"}}]})))

    result = AiServer.vision_analyze(str(image))

    assert result == "一只猫"
    body = session.calls[0][1]["json"]
    assert body["model"] == Config.VISION_MODEL
    url = body["messages"][0]["content"][1]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    assert records[-1]["kind"] == "vision" and records[-1]["success"] is True


def test_vision_missing_file_returns_none(use_session, monkeypatch, tmp_path):
    monkeypatch.setattr(Config, "VISION_ENABLED", True)
    use_session(FakeSession(FakeResponse({})))
    assert AiServer.vision_analyze(str(tmp_path / "nope.png")) is None


def test_vision_remote_url_downloads_first(use_session, records, monkeypatch):
    monkeypatch.setattr(Config, "VISION_ENABLED", True)

    class FakeGet:
        ok = True
        def raise_for_status(self):
            return None
        def iter_content(self, size):
            return [b"IMG"]

    monkeypatch.setattr(ai_server.requests, "get", lambda *a, **k: FakeGet())
    session = use_session(FakeSession(FakeResponse({"choices": [{"message": {"content": "远程图"}}]})))

    result = AiServer.vision_analyze("https://example.com/a.jpg")

    assert result == "远程图"
    payload = session.calls[0][1]["json"]
    assert payload["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_vision_api_failure_returns_none(use_session, records, monkeypatch, tmp_path):
    monkeypatch.setattr(Config, "VISION_ENABLED", True)
    image = tmp_path / "x.png"
    image.write_bytes(b"x")
    use_session(FakeSession(FakeResponse(ok=False, status_code=500, text="denied")))

    assert AiServer.vision_analyze(str(image)) is None
    assert records[-1]["success"] is False


def test_vision_json_format_appends_instruction(use_session, records, monkeypatch, tmp_path):
    monkeypatch.setattr(Config, "VISION_ENABLED", True)
    image = tmp_path / "x.png"
    image.write_bytes(b"x")
    session = use_session(FakeSession(FakeResponse({"choices": [{"message": {"content": "{}"}}]})))

    AiServer.vision_analyze(str(image), prompt="描述", response_format="json")

    system_prompt = session.calls[0][1]["json"]["messages"][0]["content"][0]["text"]
    assert "纯JSON" in system_prompt


# ── vision_analyze_with_category ──────────────────────────────────

def test_vision_category_parses_json(monkeypatch):
    monkeypatch.setattr(AiServer, "vision_analyze", staticmethod(lambda *a, **k: '{"description":"d","emotion":"e","category":"可爱"}'))

    from sticker_collector import STICKER_CATEGORIES
    category = "可爱" if "可爱" in STICKER_CATEGORIES else sorted(STICKER_CATEGORIES)[0]
    monkeypatch.setattr(
        AiServer, "vision_analyze",
        staticmethod(lambda *a, **k: '{"description":"d","emotion":"e","category":"%s"}' % category),
    )
    assert AiServer.vision_analyze_with_category("x.png") == {
        "description": "d", "emotion": "e", "category": category,
    }


def test_vision_category_strips_markdown_fence(monkeypatch):
    monkeypatch.setattr(
        AiServer, "vision_analyze",
        staticmethod(lambda *a, **k: '```json\n{"description":"d","emotion":"e","category":"其他"}\n```'),
    )
    result = AiServer.vision_analyze_with_category("x.png")
    assert result is not None and result["category"] == "其他"


def test_vision_category_invalid_json_returns_none(monkeypatch):
    monkeypatch.setattr(AiServer, "vision_analyze", staticmethod(lambda *a, **k: "not json"))
    assert AiServer.vision_analyze_with_category("x.png") is None


def test_vision_category_unknown_category_falls_back(monkeypatch):
    monkeypatch.setattr(
        AiServer, "vision_analyze",
        staticmethod(lambda *a, **k: '{"description":"d","emotion":"e","category":"不存在的分类"}'),
    )
    result = AiServer.vision_analyze_with_category("x.png")
    assert result is not None and result["category"] == "其他"


# ── _format_for_qq ────────────────────────────────────────────────

def test_format_for_qq_strips_markdown_and_truncates():
    text = "**加粗**\n\n\n\n末尾"
    out = AiServer._format_for_qq(text)
    assert "**" not in out and "加粗" in out
    assert "\n\n\n" not in out

    long_text = "字" * 3000
    truncated = AiServer._format_for_qq(long_text)
    assert len(truncated) <= 2500
    assert "内容过长已截断" in truncated
