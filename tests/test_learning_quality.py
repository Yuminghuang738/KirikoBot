"""自学习的质量：判定要准，注入要有用。

线上数据（4791 条笔记）：
  · 「回复不当」1828 条。人工复判 60 条，只有 1 条是真正的用户不满（1%），
    65% 是换话题，11% 是继续玩梗。判官被强制三选一，换话题也只能选「回复不当」。
  · 注入的 12 条里 68% 是「表现良好」，段落标题却叫「你之前踩过的坑」。
  · 笔记 53% 来自同一个用户，换到别人身上没有参考价值。
  · 「回复不当」还会扣好感度（-0.3），误判直接伤害关系分数。
"""
from __future__ import annotations

import json

import pytest


# ── 判官：允许「不适用」，门槛写清楚 ──────────────────────────

class TestJudgeRubric:
    def _prompt(self):
        from judge_service import JudgeService

        class Robot:
            group_name = "测试群"
            user_name = "小明"
            msg = "看下上一条消息的日志"

        return JudgeService(learning_service=None)._build_prompt(
            Robot(), {"user_msg": "一张猫图", "tool": "", "ai_text": "哈哈"}, None)

    def test_the_prompt_offers_a_not_applicable_option(self):
        assert "type 填 null" in self._prompt()

    def test_topic_change_is_named_as_not_a_complaint(self):
        p = self._prompt()
        assert "换话题" in p and "不是不满" in p

    def test_a_null_type_is_not_recorded(self):
        from judge_service import JudgeService
        ltype, _note = JudgeService._clean_learning({"learning": {"type": None, "note": "随便写点"}})
        assert ltype is None

    def test_an_invented_type_is_dropped(self):
        from judge_service import JudgeService
        ltype, _ = JudgeService._clean_learning({"learning": {"type": "很生气", "note": "很长的一段话很长很长"}})
        assert ltype is None


# ── 注入：只给有效的负向笔记，去重，截止编号之前的误判退役 ───────

class TestInjection:
    def _add(self, db, uid, note, row_id=None):
        db.execute_action(
            "INSERT INTO learning_log (user_id, note, user_msg, ai_text, tool_name) VALUES (?, ?, '', '', '')",
            (uid, note))

    def test_positive_lessons_are_never_injected(self, db):
        self._add(db, "u1", "[表现良好] 准确解答了问题")
        from learning_service import LearningService
        assert LearningService().get_context(db, "u1") == ""

    def test_tool_mistakes_are_injected(self, db):
        self._add(db, "u1", "[工具选择错误] 想要点歌却调用了天气")
        from learning_service import LearningService
        out = LearningService().get_context(db, "u1")
        assert "工具选择错误" in out and "需要留意的问题" in out

    def test_old_reply_complaints_are_retired_by_the_cutoff(self, db):
        """截止编号之前的「回复不当」是误判高发的旧数据：保留，但不再注入。"""
        self._add(db, "u1", "[回复不当] 用户发图后追问日志，AI却只调侃图片")
        cutoff = db.fetch_data("SELECT MAX(id) FROM learning_log")[0][0]
        db.set_state("learning_rubric_cutoff_id", str(cutoff))
        from learning_service import LearningService
        assert LearningService().get_context(db, "u1") == ""
        assert db.fetch_data("SELECT COUNT(*) FROM learning_log")[0][0] == 1, "只是退役，不删除"

    def test_new_reply_complaints_after_the_cutoff_are_injected(self, db):
        db.set_state("learning_rubric_cutoff_id", "0")
        self._add(db, "u1", "[回复不当] 用户明确说没听懂，AI应先确认再答")
        from learning_service import LearningService
        assert "没听懂" in LearningService().get_context(db, "u1")

    def test_duplicates_are_shown_once(self, db):
        db.set_state("learning_rubric_cutoff_id", "0")
        for _ in range(3):
            self._add(db, "u1", "[工具选择错误] 点歌时别调用天气")
        from learning_service import LearningService
        out = LearningService().get_context(db, "u1")
        assert out.count("点歌时别调用天气") == 1

    def test_only_the_users_own_notes_are_used(self, db):
        self._add(db, "other", "[工具选择错误] 别人的问题")
        from learning_service import LearningService
        assert LearningService().get_context(db, "u1") == ""

    def test_the_cap_limits_how_many_are_injected(self, db):
        from learning_service import LearningService
        for i in range(LearningService.MAX_INJECT + 5):
            self._add(db, "u1", f"[工具选择错误] 具体问题{i:02d}")
        out = LearningService().get_context(db, "u1")
        assert out.count("  · ") == LearningService.MAX_INJECT


class TestCutoffIsSetOnce:
    def test_a_fresh_database_gets_a_cutoff(self, db):
        assert db.get_state("learning_rubric_cutoff_id", "") != ""

    def test_the_cutoff_is_not_moved_by_later_notes(self, db):
        before = db.get_state("learning_rubric_cutoff_id", "")
        db.execute_action("INSERT INTO learning_log (user_id, note) VALUES ('u', '[回复不当] 新的')")
        db.execute_action(
            "INSERT OR IGNORE INTO app_state (key, value) VALUES "
            "('learning_rubric_cutoff_id', CAST(COALESCE((SELECT MAX(id) FROM learning_log), 0) AS TEXT))")
        assert db.get_state("learning_rubric_cutoff_id", "") == before


# ── 注入位置：说话人块，群聊私聊都有，系统提示词里没有 ───────────

class TestWhereItLives:
    class _Learn:
        def get_context(self, db, uid):
            return "【需要留意的问题】测试标记"

    def _robot(self, msg_type, group_id):
        class R:
            pass
        r = R()
        r.msg_type = msg_type
        r.group_id = group_id
        r.user_id = "u1"
        r.user_name = "小明"
        return r

    def test_it_is_not_in_the_system_prompt(self):
        from prompt_builder import build_system_prompt

        class Robot:
            msg_type = "group"
            group_id = "g1"
            group_name = "测试群"
            user_id = "u1"
            user_name = "小明"
            msg = "x"

            class incoming:
                has_images = False
                text = "x"
                image_urls: list = []
                is_at_bot = True

        sp = build_system_prompt(Robot(), db=object(), profile_service=None,
                                 learning_service=self._Learn(), affection_service=None)
        assert "需要留意的问题" not in sp

    def test_private_chats_get_it_in_the_speaker_block(self, monkeypatch):
        import main
        monkeypatch.setattr(main, "learning_service", self._Learn())
        out = main._speaker_context(self._robot("private", None), set())
        assert "测试标记" in out

    def test_group_chats_get_it_in_the_speaker_block(self, monkeypatch, db):
        import main
        monkeypatch.setattr(main, "learning_service", self._Learn())
        monkeypatch.setattr(main, "db", db)
        out = main._speaker_context(self._robot("group", "g1"), set())
        assert "测试标记" in out


# ── 历史扫描（旧路径）：NONE 不记录 ───────────────────────────

class TestLegacyEvaluator:
    def test_none_is_not_saved(self, db, monkeypatch):
        import learning_service as ls
        monkeypatch.setattr(ls, "quick_chat", lambda *a, **k: "NONE")
        svc = ls.LearningService()
        out = svc.evaluate_prev(db, "u1", {"user_msg": "x", "ai_text": "y", "tool": ""}, "换个话题吧")
        assert out is None
        assert db.fetch_data("SELECT COUNT(*) FROM learning_log")[0][0] == 0

    def test_the_prompt_allows_none_for_topic_changes(self, db, monkeypatch):
        import learning_service as ls
        seen = {}

        def fake(system, prompt, **k):
            seen["p"] = prompt
            return None

        monkeypatch.setattr(ls, "quick_chat", fake)
        ls.LearningService().evaluate_prev(db, "u1", {"user_msg": "x", "ai_text": "y", "tool": ""}, "z")
        assert "NONE" in seen["p"] and "换话题" in seen["p"]


class TestPositiveNeedsEvidence:
    """「表现良好」必须有明确认可的依据。没有抱怨不等于认可（实测：60 对样本里 0 条真认可，
    却有 41% 被判成表现良好）。"""

    def _prompt(self):
        from judge_service import JudgeService

        class Robot:
            group_name = "测试群"
            user_name = "小明"
            msg = "x"

        return JudgeService(learning_service=None)._build_prompt(
            Robot(), {"user_msg": "a", "tool": "", "ai_text": "b"}, None)

    def test_praise_needs_an_explicit_endorsement(self):
        assert "必须有明确的认可词" in self._prompt()

    def test_absence_of_complaint_is_not_approval(self):
        assert "不要因为「没有抱怨」就判表现良好" in self._prompt()


class TestLegacyRubricRequiresExplicitComplaint:
    """私聊的学习判定（旧路径）：「回复不当」只能在用户明确不满时记录；工具失误是客观的，不需要抱怨。
    线上独立抽查：改前有效教训精确率 35%，改后 41%（样本 40 对，幅度小，结论保守）。"""

    def _prompt(self, monkeypatch):
        import learning_service as ls
        seen = {}

        def fake(system, prompt, **k):
            seen["p"] = prompt
            return None

        monkeypatch.setattr(ls, "quick_chat", fake)

        class Db:
            def execute_action(self, *a, **k):
                pass

        ls.LearningService().evaluate_prev(Db(), "u1", {"user_msg": "x", "ai_text": "y", "tool": ""}, "z")
        return seen["p"]

    def test_reply_complaints_need_explicit_displeasure(self, monkeypatch):
        assert "只能在用户**明确表达不满**时记录" in self._prompt(monkeypatch)

    def test_tool_misses_do_not_need_a_complaint(self, monkeypatch):
        p = self._prompt(monkeypatch)
        assert "不需要用户抱怨" in p and "遗漏工具" in p

    def test_no_explicit_displeasure_means_not_recorded_as_a_reply_complaint(self, monkeypatch):
        assert "不要因为「AI 本可以更好」而记「回复不当」" in self._prompt(monkeypatch)
