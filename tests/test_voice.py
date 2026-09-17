"""Speaking instead of typing (QQ AI voice via LLOneBot).

The endpoint accepts an unknown character id and then silently fails to
deliver, so the id is validated against `get_ai_characters` first, and any
failure falls back to text — a voice message that never arrives would look to
the group like the bot ignoring them.
"""
from __future__ import annotations

from typing import Any

import pytest


# The wire shape from get_ai_characters (character_id / character_name).
WIRE_CHARACTERS = [
    {"character_id": "lucy-voice-f38", "character_name": "傲娇少女"},
    {"character_id": "lucy-voice-xueling", "character_name": "元气少女"},
    {"character_id": "lucy-voice-houge", "character_name": "猴哥"},
]
# The flattened shape the client hands back.
CHARACTERS = [
    {"id": "lucy-voice-f38", "name": "傲娇少女", "category": "现代"},
    {"id": "lucy-voice-xueling", "name": "元气少女", "category": "现代"},
    {"id": "lucy-voice-houge", "name": "猴哥", "category": "搞怪"},
]


class FakeLLBot:
    def __init__(self, ok=True, characters=None):
        self.ok = ok
        self.characters = CHARACTERS if characters is None else characters
        self.sent: list[tuple[str, str, str]] = []

    def send_ai_voice(self, group_id, character, text):
        self.sent.append((group_id, character, text))
        return self.ok

    def get_ai_characters(self):
        return self.characters


class Robot:
    msg_type, group_id, user_id, user_name = "group", "g1", "u1", "小明"


class PrivateRobot(Robot):
    msg_type, group_id = "private", None


class AI:
    def __init__(self, args: str = '{"text": "哼，知道啦"}'):
        self.ai_message = {"tool_calls": [{"id": "c1", "function": {
            "name": "send_voice", "arguments": args}}]}
        self.tool_result_text = ""
        self.user_text = ""


def make(llbot):
    from ai_tools import VoiceTool

    return VoiceTool(None, None, llbot)


class TestSending:
    def test_it_speaks_the_text(self):
        bot = FakeLLBot()
        make(bot).voice_call(Robot(), AI())
        assert bot.sent == [("g1", "lucy-voice-f38", "哼，知道啦")]

    def test_it_uses_the_requested_voice(self):
        bot = FakeLLBot()
        make(bot).voice_call(Robot(), AI('{"text":"哇","voice":"lucy-voice-xueling"}'))
        assert bot.sent[0][1] == "lucy-voice-xueling"

    def test_the_model_is_told_not_to_repeat_itself_in_text(self):
        ai = AI()
        make(FakeLLBot()).voice_call(Robot(), ai)
        assert "不要再打字重复" in ai.tool_result_text

    def test_the_model_is_not_told_to_announce_it(self):
        """Saying "I sent you a voice message" is exactly the AI voice."""
        ai = AI()
        make(FakeLLBot()).voice_call(Robot(), ai)
        assert "不要说明你发了语音" in ai.tool_result_text


class TestFallbacks:
    def test_a_failed_send_falls_back_to_text(self):
        ai = AI()
        make(FakeLLBot(ok=False)).voice_call(Robot(), ai)
        assert "没发出去" in ai.tool_result_text
        assert "直接用文字" in ai.tool_result_text

    def test_a_raising_client_falls_back_to_text(self):
        class Boom(FakeLLBot):
            def send_ai_voice(self, *a):
                raise RuntimeError("llbot down")

        ai = AI()
        make(Boom()).voice_call(Robot(), ai)
        assert "直接用文字" in ai.tool_result_text

    def test_an_unknown_voice_falls_back_to_the_default(self):
        bot = FakeLLBot()
        make(bot).voice_call(Robot(), AI('{"text":"喂","voice":"lucy-voice-nope"}'))
        assert bot.sent[0][1] == "lucy-voice-f38"

    def test_an_empty_character_list_trusts_what_was_asked(self):
        """Some deployments don't expose the list; don't block on that."""
        bot = FakeLLBot(characters=[])
        make(bot).voice_call(Robot(), AI('{"text":"喂","voice":"whatever"}'))
        assert bot.sent[0][1] == "whatever"

    def test_a_broken_character_lookup_does_not_block_sending(self):
        class Boom(FakeLLBot):
            def get_ai_characters(self):
                raise RuntimeError("no list")

        bot = Boom()
        make(bot).voice_call(Robot(), AI('{"text":"喂","voice":"lucy-voice-xueling"}'))
        assert bot.sent[0][1] == "lucy-voice-xueling"

    def test_private_chat_uses_text(self):
        bot = FakeLLBot()
        ai = AI()
        make(bot).voice_call(PrivateRobot(), ai)
        assert not bot.sent
        assert "只支持群" in ai.tool_result_text

    def test_empty_text_sends_nothing(self):
        bot = FakeLLBot()
        make(bot).voice_call(Robot(), AI('{"text":"   "}'))
        assert not bot.sent

    def test_missing_arguments_do_not_raise(self):
        bot = FakeLLBot()
        make(bot).voice_call(Robot(), AI("{}"))
        assert not bot.sent

    def test_broken_json_does_not_raise(self):
        bot = FakeLLBot()
        make(bot).voice_call(Robot(), AI("not json"))
        assert not bot.sent

    def test_a_default_voice_that_is_also_missing_still_picks_something(self):
        bot = FakeLLBot(characters=[{"id": "only-one", "name": "唯一", "category": ""}])
        make(bot).voice_call(Robot(), AI('{"text":"喂","voice":"nope"}'))
        assert bot.sent[0][1] == "only-one"


class TestRegistration:
    def test_it_is_self_contained(self):
        """The voice IS the reply; a follow-up would add a typed duplicate."""
        import ast
        import os

        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "KirikoBot", "main.py",
        )
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                names = [t.id for t in node.targets if isinstance(t, ast.Name)]
                if "SELF_CONTAINED_TOOLS" in names:
                    assert "send_voice" in {e.value for e in node.value.elts}
                    return
        raise AssertionError("SELF_CONTAINED_TOOLS not found")

    def test_the_feature_gate_knows_it(self):
        from feature_gate import FEATURE_KEYS, TOOL_FEATURE

        assert "voice" in FEATURE_KEYS
        assert TOOL_FEATURE["send_voice"] == "voice"

    def test_the_schema_is_offered_with_the_default_voice(self):
        from ai_tools_list import AiTools

        tools = {t["function"]["name"]: t["function"] for t in AiTools().ai_tools()}
        assert "send_voice" in tools
        enum = tools["send_voice"]["parameters"]["properties"]["voice"]["enum"]
        assert "lucy-voice-f38" in enum, "the tsundere-girl voice is the default"

    def test_the_schema_asks_for_spoken_language(self):
        from ai_tools_list import AiTools

        tools = {t["function"]["name"]: t["function"] for t in AiTools().ai_tools()}
        desc = tools["send_voice"]["parameters"]["properties"]["text"]["description"]
        assert "口语" in desc
        assert "颜文字" in desc, "emoji read terribly out loud"

    def test_the_default_character_is_configurable(self):
        from config import Config

        assert Config.VOICE_DEFAULT_CHARACTER == "lucy-voice-f38"
        assert Config.VOICE_ENABLED is True


class TestClient:
    def test_the_character_list_is_flattened(self):
        from llbot_client import LLBotClient

        client = LLBotClient("http://x", "t")
        client._session = type("S", (), {
            "post": staticmethod(lambda *a, **k: type("R", (), {
                "raise_for_status": lambda self: None,
                "json": lambda self: {"data": [
                    {"type": "现代", "characters": WIRE_CHARACTERS[:2]},
                    {"type": "搞怪", "characters": WIRE_CHARACTERS[2:]},
                ]},
            })()),
        })()
        chars = client.get_ai_characters()
        ids = [c["id"] for c in chars]
        assert ids == ["lucy-voice-f38", "lucy-voice-xueling", "lucy-voice-houge"]
        assert chars[0]["name"] == "傲娇少女"
        assert all(c["id"] != "" for c in chars), "duplicates dropped, blanks skipped"

    def test_a_broken_response_yields_no_characters(self):
        from llbot_client import LLBotClient

        client = LLBotClient("http://x", "t")

        class Boom:
            def post(self, *a, **k):
                raise RuntimeError("nope")

        client._session = Boom()
        assert client.get_ai_characters() == []

    def test_empty_text_is_not_sent(self):
        from llbot_client import LLBotClient

        client = LLBotClient("http://x", "t")
        assert client.send_ai_voice("g1", "v", "   ") is False


class TestMainActuallyImportsIt:
    """main.py cannot be imported in tests (it starts the scheduler), so the
    tool wiring is checked statically instead. This exists because a rename
    silently missed the import list and the container crash-looped on
    `NameError: name 'VoiceTool' is not defined` — every other test passed,
    because they import ai_tools directly.
    """

    def _main_ast(self):
        import ast
        import os

        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "KirikoBot", "main.py",
        )
        with open(path, encoding="utf-8") as fh:
            return ast.parse(fh.read())

    def test_every_instantiated_tool_is_imported(self):
        import ast

        tree = self._main_ast()
        imported: set[str] = set()
        defined: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "ai_tools":
                imported |= {a.name for a in node.names}
            if isinstance(node, (ast.ClassDef, ast.FunctionDef)):
                defined.add(node.name)

        used = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id.endswith("Tool")
        }
        missing = used - imported - defined
        assert not missing, f"used but never imported: {sorted(missing)}"
