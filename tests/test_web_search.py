"""联网搜索：走 DeepSeek 官方 Search API。

这里只测**不联网**的部分：地址推导、请求体、响应映射、错误分类。
真正发请求那一步不放进单元测试 —— 把外部网络塞进测试，本身就是不可靠的测试。

（历史：这个文件以前测的是一整套自己爬搜索引擎的实现 —— Bing 的 base64 跳转
解包、360 的 JS 跳转壳、CAPTCHA 识别、维基百科标题过滤。换成官方 API 之后那些
代码连同测试一起删掉了，因为它们要解决的问题已经不存在了。）
"""
from __future__ import annotations

from datetime import datetime

import pytest

import web_search
from web_search import WebSearch


class FakeResp:
    """够用的 requests.Response 替身。"""

    def __init__(self, status: int = 200, payload=None, text: str = "") -> None:
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


@pytest.fixture
def ws(monkeypatch):
    """一个带着 token、不会真发请求的 WebSearch。"""
    monkeypatch.setattr(web_search.Config, "DEEPSEEK_TOKEN", "test-key", raising=False)
    monkeypatch.setattr(
        web_search.Config, "DEEPSEEK_API",
        "https://api.deepseek.com/chat/completions", raising=False,
    )
    return WebSearch()


def _patch_post(monkeypatch, resp):
    calls: list[dict] = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append({"url": url, "headers": headers or {}, "json": json or {}, "timeout": timeout})
        if isinstance(resp, Exception):
            raise resp
        return resp

    monkeypatch.setattr(web_search.requests, "post", fake_post)
    return calls


class TestEndpoint:
    def test_base_url_strips_the_chat_path(self, monkeypatch):
        """ .env 里 DEEPSEEK_API 是完整的 chat/completions 地址，要剥成根。 """
        monkeypatch.setattr(
            web_search.Config, "DEEPSEEK_API",
            "https://api.deepseek.com/chat/completions", raising=False,
        )
        assert WebSearch._base_url() == "https://api.deepseek.com"
        assert WebSearch.search_url() == "https://api.deepseek.com/v1/tools/search"

    def test_a_custom_gateway_is_respected(self, monkeypatch):
        monkeypatch.setattr(
            web_search.Config, "DEEPSEEK_API",
            "https://my-gateway.example/v1/chat/completions", raising=False,
        )
        assert WebSearch.search_url() == "https://my-gateway.example/v1/tools/search"

    def test_it_tolerates_a_trailing_slash(self, monkeypatch):
        monkeypatch.setattr(
            web_search.Config, "DEEPSEEK_API", "https://api.deepseek.com/", raising=False,
        )
        assert WebSearch.search_url() == "https://api.deepseek.com/v1/tools/search"


class TestRequest:
    def test_it_sends_query_count_and_need_content(self, ws, monkeypatch):
        calls = _patch_post(monkeypatch, FakeResp(payload={"results": []}))
        ws._search("中国首都")
        body = calls[0]["json"]
        assert body["query"] == "中国首都"
        assert body["need_content"] is False, "基准结论：不取正文，用 summary 即可，资料体积小一半"
        assert body["count"] == WebSearch.MAX_RESULTS, "只需要 5 条，多要的正文白白下载"
        assert 1 <= body["count"] <= 50, "API 限制 count 在 1~50"

    def test_it_uses_bearer_auth(self, ws, monkeypatch):
        calls = _patch_post(monkeypatch, FakeResp(payload={"results": []}))
        ws._search("x")
        assert calls[0]["headers"]["Authorization"] == "Bearer test-key"
        assert calls[0]["url"].endswith("/v1/tools/search")

    def test_a_missing_token_never_hits_the_network(self, ws, monkeypatch):
        monkeypatch.setattr(web_search.Config, "DEEPSEEK_TOKEN", "", raising=False)
        calls = _patch_post(monkeypatch, FakeResp(payload={"results": []}))
        assert ws._search("x") == []
        assert calls == [], "没配 key 就不该发请求"
        assert ws.last_reason == "auth"


class TestResponseMapping:
    def _results(self, **over):
        base = {
            "title": "标题", "url": "https://example.com/a",
            "snippet": "短摘要", "summary": "长摘要", "content": "正文",
        }
        base.update(over)
        return {"results": [base]}

    def test_it_prefers_summary_then_content(self, ws, monkeypatch):
        """summary 是给模型读的长摘要，优先用它；正文只作为兜底。"""
        _patch_post(monkeypatch, FakeResp(payload=self._results()))
        out = ws.search_and_fetch("x")
        assert "长摘要" in out
        assert "正文" not in out
        assert "https://example.com/a" in out, "要给 AI 看来源链接"

    def test_it_falls_back_to_summary(self, ws, monkeypatch):
        _patch_post(monkeypatch, FakeResp(payload=self._results(content="")))
        assert "长摘要" in ws.search_and_fetch("x")

    def test_it_falls_back_to_snippet(self, ws, monkeypatch):
        _patch_post(monkeypatch, FakeResp(payload=self._results(content="", summary="")))
        assert "短摘要" in ws.search_and_fetch("x")

    def test_a_result_without_any_text_is_skipped(self, ws, monkeypatch):
        _patch_post(monkeypatch, FakeResp(
            payload=self._results(content="", summary="", snippet="")))
        assert ws.search_and_fetch("x") == ""

    def test_long_content_is_truncated(self, ws, monkeypatch):
        """摘要优先，所以要只剩正文时，超长的正文才会被用到并截断。"""
        _patch_post(monkeypatch, FakeResp(
            payload=self._results(content="啊" * 5000, summary="")))
        out = ws.search_and_fetch("x")
        assert len(out) < 5000
        assert out.rstrip().endswith("…")

    def test_it_caps_the_number_of_results(self, ws, monkeypatch):
        many = [{
            "title": f"t{i}", "url": f"https://e.com/{i}",
            "snippet": "", "summary": "s", "content": "c",
        } for i in range(30)]
        _patch_post(monkeypatch, FakeResp(payload={"results": many}))
        out = ws.search_and_fetch("x")
        assert out.count("【来源：") == WebSearch.MAX_RESULTS


class TestFailureClassification:
    """失败原因要分得开 —— 额度用完、被限流、配置错了，对用户说的话不一样。"""

    @pytest.mark.parametrize("status,reason", [
        (402, "quota"),
        (429, "rate_limited"),
        (401, "auth"),
        (403, "auth"),
        (500, "error"),
        (504, "error"),
        (400, "error"),
    ])
    def test_status_maps_to_reason(self, ws, monkeypatch, status, reason):
        _patch_post(monkeypatch, FakeResp(status=status, text="boom"))
        assert ws._search("x") == []
        assert ws.last_reason == reason

    def test_empty_results_is_not_an_error(self, ws, monkeypatch):
        """空结果不计费，就是真没搜到 —— 不能跟「出问题」混为一谈。"""
        _patch_post(monkeypatch, FakeResp(payload={"results": []}))
        assert ws._search("x") == []
        assert ws.last_reason == "no_results"

    def test_a_network_error_is_an_error(self, ws, monkeypatch):
        _patch_post(monkeypatch, web_search.requests.ConnectionError("boom"))
        assert ws._search("x") == []
        assert ws.last_reason == "error"

    def test_a_non_json_body_is_an_error(self, ws, monkeypatch):
        _patch_post(monkeypatch, FakeResp(status=200, payload=None, text="<html>"))
        assert ws._search("x") == []
        assert ws.last_reason == "error"

    def test_success_resets_the_reason(self, ws, monkeypatch):
        _patch_post(monkeypatch, FakeResp(status=429, text="slow down"))
        ws._search("x")
        assert ws.last_reason == "rate_limited"
        _patch_post(monkeypatch, FakeResp(
            payload={"results": [{"title": "t", "url": "u", "content": "c"}]}))
        ws._search("x")
        assert ws.last_reason == "ok"


class TestFailureMessages:
    def test_every_reason_has_a_message(self):
        for reason in ("quota", "rate_limited", "auth", "error", "no_results"):
            assert reason in WebSearch.REASON_MESSAGES

    def test_messages_differ_per_reason(self):
        """都一样的话，这个分类就白做了。"""
        msgs = list(WebSearch.REASON_MESSAGES.values())
        assert len(set(msgs)) == len(msgs)

    def test_no_results_is_not_described_as_a_service_failure(self):
        """「没搜到」别让用户以为服务坏了。"""
        ws = WebSearch()
        ws.last_reason = "no_results"
        assert "额度" not in ws.failure_message()
        assert "限流" not in ws.failure_message()

    def test_an_unknown_reason_falls_back(self):
        ws = WebSearch()
        ws.last_reason = "something-new"
        assert ws.failure_message() == WebSearch.REASON_MESSAGES["error"]


class TestYearNormalization:
    """线上实测：主模型按训练知识（截止 2025）写查询，「最近发售的游戏」会变成「2025年…」。"""

    NOW = datetime(2026, 10, 31)

    def test_a_stale_year_on_a_recency_query_becomes_this_year(self):
        from web_search import normalize_years
        assert normalize_years("2025年最近发售的游戏", self.NOW) == "2026年最近发售的游戏"

    def test_historical_queries_are_left_alone(self):
        from web_search import normalize_years
        assert normalize_years("2020年的疫情", self.NOW) == "2020年的疫情"

    def test_a_correct_year_is_untouched(self):
        from web_search import normalize_years
        assert normalize_years("2026年最近发售的游戏", self.NOW) == "2026年最近发售的游戏"

    def test_no_year_is_untouched(self):
        from web_search import normalize_years
        assert normalize_years("最新 DeepSeek 模型", self.NOW) == "最新 DeepSeek 模型"

    def test_the_request_goes_out_with_the_corrected_year(self, ws, monkeypatch):
        calls = _patch_post(monkeypatch, FakeResp(payload={"results": []}))
        ws._search("2025年最近发售的游戏")
        assert calls[0]["json"]["query"].startswith(str(datetime.now().year))


class TestSynthesisSpeed:
    """综合回答不开思考模式（基准：思考开 中位 2.8s，关 1.9s，质量持平）。"""

    def test_the_tool_answers_without_thinking(self, monkeypatch):
        from ai_tools import WebSearchTool

        class FakeSearch:
            last_reason = "ok"

            def search_and_fetch(self, query):
                return "【来源：x】\n一段资料"

        class FakeAI:
            ai_message = {"tool_calls": [{"id": "c", "function": {"name": "web_search",
                                                                  "arguments": '{"query": "x"}'}}]}
            thinking_type = "enabled"
            tool_result_text = ""
            user_text = ""
            ai_text = "回答"
            history_list = []
            airesponse_tool_calls = []
            airesponse_tool_id = ""

            def ai_request(self):
                self.seen_thinking = self.thinking_type

        class Robot:
            msg = "问题"
            replied = []

            def reply(self, text):
                self.replied.append(text)

        ai = FakeAI()
        WebSearchTool(FakeSearch(), None).web_search_call(Robot(), ai)
        assert ai.seen_thinking == "disabled"

    def test_the_tool_description_states_the_year_rule(self):
        import ai_tools_list
        src = open(ai_tools_list.__file__, encoding="utf-8").read()
        assert "年份必须写用户消息里「当前时间」的那一年" in src
