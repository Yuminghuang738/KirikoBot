"""共同记忆增量提取。

线上问题：profile_memories 一直是 0 条。原因是记忆只在画像分析时才提取，而画像分析
门槛高、很少触发。现在改成按 bot 对话轮数增量提取，和画像分析分开。
"""
from __future__ import annotations

import json

import pytest

import profile_service as ps
from profile_service import ProfileService


def _talk(db, uid: str, turns: int) -> None:
    for i in range(turns):
        db.deposit_chat_history("user", uid, "g1", f"第{i}句", "", "")
        db.deposit_chat_history("assistant", uid, "g1", f"回{i}", "", "")


@pytest.fixture
def svc(monkeypatch):
    service = ProfileService()
    service.replies: list[str] = []  # 测试里预置的模型返回

    def fake_quick_chat(system, user, **kw):
        return service.replies.pop(0) if service.replies else None

    monkeypatch.setattr(ps, "quick_chat", fake_quick_chat)
    return service


class TestGating:
    def test_nothing_before_the_gap(self, db, svc):
        _talk(db, "u1", ProfileService.MEMORY_GAP - 1)
        assert svc.should_extract_memory(db, "u1") is False

    def test_fires_once_the_gap_is_reached(self, db, svc):
        _talk(db, "u1", ProfileService.MEMORY_GAP)
        assert svc.should_extract_memory(db, "u1") is True

    def test_counter_advances_so_the_same_turns_are_not_reprocessed(self, db, svc):
        _talk(db, "u1", ProfileService.MEMORY_GAP)
        svc.replies.append('{"memories": []}')
        svc.extract_memories(db, "u1", "小明")
        assert svc.should_extract_memory(db, "u1") is False
        _talk(db, "u1", ProfileService.MEMORY_GAP)
        assert svc.should_extract_memory(db, "u1") is True

    def test_counter_advances_even_when_nothing_is_extracted(self, db, svc):
        """提取不到东西也要推进计数，否则同一批对话会被反复送去模型。"""
        _talk(db, "u1", ProfileService.MEMORY_GAP)
        svc.replies.append('{"memories": []}')
        assert svc.extract_memories(db, "u1", "小明") == []
        assert db.get_state("memory_turns:u1") == str(ProfileService.MEMORY_GAP)

    def test_the_bot_itself_is_never_profiled(self, db, svc, monkeypatch):
        monkeypatch.setattr(ps.Config, "BOT_QQ_LIST", ["bot1"], raising=False)
        _talk(db, "bot1", ProfileService.MEMORY_GAP)
        assert svc.should_extract_memory(db, "bot1") is False

    def test_counts_bot_turns_not_group_chatter(self, db, svc):
        """门控数的是 bot 对话轮数：群里闲聊不 @ 机器人，不该触发。"""
        for i in range(ProfileService.MEMORY_GAP + 50):
            db.record_group_message("g1", "u1", "小明", f"闲聊{i}")
        assert svc.should_extract_memory(db, "u1") is False


class TestExtraction:
    def test_saves_what_the_model_found(self, db, svc):
        _talk(db, "u1", ProfileService.MEMORY_GAP)
        svc.replies.append(json.dumps({"memories": [
            {"content": "小明答应周三帮 Kiriko 听一段录音", "kind": "约定"},
            {"content": "小明给 Kiriko 起过外号「小猫」", "kind": "外号"},
        ]}, ensure_ascii=False))
        saved = svc.extract_memories(db, "u1", "小明")
        assert len(saved) == 2
        stored = [m["content"] for m in db.get_profile_memories("u1", limit=5)]
        assert "小明答应周三帮 Kiriko 听一段录音" in stored

    def test_at_most_two_per_run(self, db, svc):
        _talk(db, "u1", ProfileService.MEMORY_GAP)
        svc.replies.append(json.dumps({"memories": [
            {"content": f"具体的事{i}号", "kind": "事件"} for i in range(5)]}))
        assert len(svc.extract_memories(db, "u1", "小明")) == ProfileService.MEMORY_MAX_NEW

    def test_code_fenced_json_is_accepted(self, db, svc):
        _talk(db, "u1", ProfileService.MEMORY_GAP)
        svc.replies.append('```json\n{"memories": [{"content": "他最近在修一把老吉他", "kind": "事件"}]}\n```')
        assert svc.extract_memories(db, "u1", "小明") == ["他最近在修一把老吉他"]

    def test_garbage_output_saves_nothing_and_does_not_raise(self, db, svc):
        _talk(db, "u1", ProfileService.MEMORY_GAP)
        svc.replies.append("抱歉我没法输出 JSON")
        assert svc.extract_memories(db, "u1", "小明") == []
        assert db.get_profile_memories("u1") == []

    def test_model_outage_is_silent(self, db, svc):
        _talk(db, "u1", ProfileService.MEMORY_GAP)
        assert svc.extract_memories(db, "u1", "小明") == []   # fake 返回 None

    def test_duplicates_are_not_stored_twice(self, db, svc):
        _talk(db, "u1", ProfileService.MEMORY_GAP)
        same = json.dumps({"memories": [{"content": "约定周三听录音", "kind": "约定"}]})
        svc.replies.extend([same, same])
        svc.extract_memories(db, "u1", "小明")
        _talk(db, "u1", ProfileService.MEMORY_GAP)
        svc.extract_memories(db, "u1", "小明")
        assert len(db.get_profile_memories("u1", limit=10)) == 1

    def test_the_store_still_keeps_only_five(self, db, svc):
        """提取会不停地产生新记忆，淘汰上限必须在提取路径上也生效。"""
        for batch in range(4):
            _talk(db, "u1", ProfileService.MEMORY_GAP)
            svc.replies.append(json.dumps({"memories": [
                {"content": f"第{batch}批第{j}件事", "kind": "事件"} for j in range(2)]}))
            svc.extract_memories(db, "u1", "小明")
        assert len(db.get_profile_memories("u1", limit=20)) <= db.MEMORY_KEEP


class TestNegativeMemoriesAreRefused:
    """辱骂/威胁/骚扰不能变成共同记忆，否则之后会被当成梗翻出来（实测线上出现过）。"""

    @pytest.mark.parametrize("bad", [
        "欧石楠对Kiriko说过「你妈死了」这句伤人的话",
        "欧石楠自称是群主，并以踢出群威胁Kiriko",
        "某人骂Kiriko是废物",
        "对方发了别人的住址",
    ])
    def test_model_output_with_negative_content_is_dropped(self, db, svc, bad):
        _talk(db, "u1", ProfileService.MEMORY_GAP)
        svc.replies.append(json.dumps({"memories": [
            {"content": bad, "kind": "事件"},
            {"content": "小明答应周三帮 Kiriko 听一段录音", "kind": "约定"},
        ]}, ensure_ascii=False))
        saved = svc.extract_memories(db, "u1", "小明")
        assert bad not in saved, "负面内容不该入库"
        assert saved == ["小明答应周三帮 Kiriko 听一段录音"], "正面的那条照常保留"

    def test_the_prompt_forbids_recording_insults(self):
        assert "辱骂、威胁、骚扰" in ProfileService.MEMORY_SYSTEM
        assert "一律不记" in ProfileService.MEMORY_SYSTEM


class TestTriggerWiring:
    def test_the_main_trigger_submits_extraction(self):
        """_trigger_profile_update 必须把记忆提取也提交出去（之前只提交画像分析）。"""
        import inspect

        import main
        src = inspect.getsource(main._trigger_profile_update)
        assert "should_extract_memory" in src
        assert "extract_memories" in src


class TestHistoryIndex:
    def test_history_is_indexed_by_user_and_group(self, db):
        """记忆门控每条群消息都会按用户计数，必须走索引而不是全表扫。"""
        rows = db.fetch_data(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='history'")
        names = {r[0] for r in rows}
        assert "idx_history_user_group" in names


class TestTriggerFiresForPrivateChats:
    """线上事故：记忆提取只挂在群聊分支上，而对话绝大多数发生在私聊里，
    部署后 26 次私聊回复、只有 1 次群聊回复，记忆一条都没有新增。"""

    class _Robot:
        def __init__(self, msg_type, group_id, user_id="u1", user_name="小明"):
            self.msg_type = msg_type
            self.group_id = group_id
            self.user_id = user_id
            self.user_name = user_name

    @pytest.fixture
    def wired(self, db, monkeypatch):
        import main
        submitted: list[str] = []

        class Recorder:
            def submit(self, fn, *args, **kwargs):
                submitted.append(fn.__name__)

        monkeypatch.setattr(main, "db", db)
        monkeypatch.setattr(main, "executor", Recorder())
        return main, submitted

    def test_a_private_message_submits_memory_extraction(self, db, wired):
        main, submitted = wired
        _talk(db, "u1", ProfileService.MEMORY_GAP)
        main._trigger_profile_update(self._Robot("private", None), set())
        assert "extract_memories" in submitted

    def test_private_chats_do_not_trigger_profile_analysis(self, db, wired):
        """画像分析的原有行为不变：只在群里触发。"""
        main, submitted = wired
        _talk(db, "u1", ProfileService.MEMORY_GAP)
        main._trigger_profile_update(self._Robot("private", None), set())
        assert "analyze_user" not in submitted

    def test_a_group_message_submits_both_jobs(self, db, wired):
        main, submitted = wired
        _talk(db, "u1", ProfileService.MEMORY_GAP)
        main._trigger_profile_update(self._Robot("group", "g1"), set())
        assert "extract_memories" in submitted
        assert "analyze_user" in submitted

    def test_the_profiles_toggle_still_disables_everything(self, db, wired):
        main, submitted = wired
        _talk(db, "u1", ProfileService.MEMORY_GAP)
        main._trigger_profile_update(self._Robot("private", None), {"profiles"})
        assert submitted == []

    def test_bot_accounts_are_skipped(self, db, wired, monkeypatch):
        main, submitted = wired
        monkeypatch.setattr(main.Config, "BOT_QQ_LIST", ["bot1"], raising=False)
        _talk(db, "bot1", ProfileService.MEMORY_GAP)
        main._trigger_profile_update(self._Robot("private", None, user_id="bot1"), set())
        assert submitted == []
