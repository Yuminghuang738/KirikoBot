"""The persona prompt and the frontend escaping guard.

`prompt_builder` is importable on its own; `main.py` is still parsed with `ast`
because importing it starts the scheduler and the worker pool.
"""
from __future__ import annotations

import ast
import os
import re

import pytest

from conftest import APP_DIR

MAIN_PY = os.path.join(APP_DIR, "main.py")
APP_JS = os.path.join(APP_DIR, "static", "js", "app.js")

from prompt_builder import (  # noqa: E402  (needs conftest's sys.path setup)
    PERSONA,
    build_role_prompt,
    build_system_prompt,
    build_user_message,
)


class _Incoming:
    has_images = False


class _Robot:
    def __init__(self, msg_type="group", msg="你好", has_images=False):
        self.msg_type = msg_type
        self.msg = msg
        self.group_id = "g1"
        self.user_id = "u1"
        self.user_name = "小明"
        self.group_name = "测试群"
        self.incoming = _Incoming()
        self.incoming.has_images = has_images


class TestStyleGuide:
    def test_is_substantial(self):
        assert len(PERSONA) > 300

    @pytest.mark.parametrize("keyword", ["傲娇", "立场", "AI"])
    def test_covers_the_persona_requirements(self, keyword):
        assert keyword in PERSONA

    def test_bans_ai_isms(self):
        for phrase in ("作为一个AI", "希望对你有帮助", "首先", "总结"):
            assert phrase in PERSONA, f"missing anti-AI-ism rule for {phrase!r}"


class TestSystemPrompt:
    def test_includes_persona_and_time(self):
        prompt = build_system_prompt(_Robot())
        assert PERSONA in prompt
        assert "当前时间：" in prompt

    def test_private_drops_the_group_rule(self):
        prompt = build_system_prompt(_Robot(msg_type="private"))
        assert PERSONA in prompt
        assert "只在群内回复" not in prompt

    def test_env_notes_are_appended_but_subordinate(self):
        """A stale .env line must not be able to redefine who she is."""
        prompt = build_role_prompt("你是聊天小助手，可以使用颜文字")
        assert prompt.startswith(PERSONA)
        assert "部署方补充设定" in prompt
        assert "冲突时以上面为准" in prompt

    def test_empty_extra_yields_the_persona_alone(self):
        assert build_role_prompt("") == PERSONA
        assert build_role_prompt(None) == PERSONA

    def test_group_gets_the_group_rule(self):
        assert "只在群内回复" in build_system_prompt(_Robot())

    def test_disabled_features_are_announced(self):
        from feature_gate import FEATURE_DEFS

        key = FEATURE_DEFS[0]["key"]
        prompt = build_system_prompt(_Robot(), disabled={key})
        assert "已关闭的功能" in prompt
        assert FEATURE_DEFS[0]["label"] in prompt

    def test_context_service_failure_does_not_break_the_prompt(self):
        class Boom:
            def build_context_prompt(self, *a, **k):
                raise RuntimeError("boom")

            def get_context(self, *a, **k):
                raise RuntimeError("boom")

        prompt = build_system_prompt(
            _Robot(), db=object(), profile_service=Boom(),
            learning_service=Boom(), affection_service=Boom(),
        )
        assert PERSONA in prompt

    def test_context_services_are_used_when_provided(self):
        class Stub:
            def build_context_prompt(self, *a, **k):
                return "【上下文】测试标记"

            def get_context(self, *a, **k):
                return "【笔记】测试标记"

        prompt = build_system_prompt(
            _Robot(), db=object(), profile_service=Stub(),
            learning_service=Stub(), affection_service=Stub(),
        )
        assert "测试标记" in prompt


class TestUserMessage:
    def test_includes_speaker_and_group(self):
        text = build_user_message(_Robot(msg="在吗"))
        assert "小明" in text and "在吗" in text and "测试群" in text

    def test_empty_message_falls_back(self):
        assert build_user_message(_Robot(msg="   ")) .endswith("[空消息]")
        assert "[图片消息]" in build_user_message(_Robot(msg="", has_images=True))


class TestFrontendEscaping:
    """Guard against re-introducing stored XSS in the dashboard.

    A QQ nickname or message body flows into these template literals, so any
    interpolation of user-controlled data must go through esc().
    """

    USER_FIELDS = (
        "user_name", "content", "msg", "request", "summary", "note", "ai_text",
        "user_msg", "group_name", "card", "description", "emotion", "category",
        "relationship", "last_interaction", "display",
    )
    # confirm() renders plain text, not HTML.
    ALLOWED = {"filename"}

    def test_no_unescaped_user_data_in_innerhtml(self):
        src = open(APP_JS, encoding="utf-8").read()
        offenders = []
        for m in re.finditer(r"\$\{([^{}]*)\}", src):
            expr = m.group(1)
            fields = [f for f in self.USER_FIELDS if re.search(r"\b" + f + r"\b", expr)]
            if not fields or "esc(" in expr or "logLineHTML" in expr:
                continue
            if any(f in self.ALLOWED for f in fields) and "confirm(" in src[max(0, m.start() - 120):m.start()]:
                continue
            offenders.append((src[:m.start()].count("\n") + 1, expr.strip()[:70]))
        assert not offenders, f"未转义的用户数据插值: {offenders}"

    def test_esc_helper_escapes_the_dangerous_characters(self):
        src = open(APP_JS, encoding="utf-8").read()
        m = re.search(r"function esc\(v\)\{(.*?)\}", src)
        assert m, "esc() helper missing"
        for ch in ("&", "<", ">", '"', "'"):
            assert ch in m.group(1), f"esc() does not handle {ch!r}"


class TestLogStreamEscapes:
    def test_display_is_html_escaped(self):
        src = open(os.path.join(APP_DIR, "log_stream.py"), encoding="utf-8").read()
        assert "html.escape" in src
        assert 'entry["msg"]}' not in src, "raw message interpolated into display HTML"


class TestMainStaysThin:
    def test_prompt_building_lives_in_prompt_builder(self):
        """The persona/rules block must not creep back into main.py."""
        source = open(MAIN_PY, encoding="utf-8").read()
        assert "def _build_system_prompt" not in source
        assert "PERSONA = " not in source
        assert "persona" not in source.lower() or "build_role_prompt" in source
