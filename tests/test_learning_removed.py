"""自学习已全面移除。这里锁住「不再存在、不再注入、不再写库」，防止它被悄悄加回来。

依据：自学习的笔记只能记教训、不能改模型本身；工具误用的纠正效果没有被证明，
还白白占用输入 token。决定由用户做出，见目标记录。
"""
from __future__ import annotations

import importlib
import sqlite3

import pytest


def test_the_learning_module_is_gone():
    with pytest.raises(ImportError):
        importlib.import_module("learning_service")


def test_the_feature_toggle_is_gone():
    from feature_gate import FEATURE_DEFS

    assert "learning" not in {f["key"] for f in FEATURE_DEFS}


def test_the_judge_prompt_has_no_learning_section(monkeypatch):
    from judge_service import JudgeService

    class Robot:
        group_name = "测试群"
        user_name = "小明"
        msg = "今天好开心"

    prompt = JudgeService()._build_prompt(Robot(), None)
    assert "learning" not in prompt
    assert "上一轮" not in prompt
    assert "sentiment" in prompt


def test_the_judge_only_scores_sentiment(db, monkeypatch):
    """判官的输出只有情绪分；学习字段即使模型给了也不会被使用。"""
    import judge_service as js
    from judge_service import JudgeService

    class Robot:
        group_name = "测试群"
        user_name = "小明"
        user_id = "u1"
        group_id = "g1"
        msg = "谢谢你"

    monkeypatch.setattr(js, "quick_chat", lambda *a, **k: '{"sentiment":{"delta":0.3,"reason":"感谢"},"learning":{"type":"回复不当","note":"乱写"}}')
    out = JudgeService().judge_turn(db, Robot(), affection_on=True)
    assert out is not None
    assert set(out) == {"sentiment", "delta"}
    assert out["delta"] == pytest.approx(0.3)


def test_the_judge_writes_no_learning_note(db, monkeypatch):
    import judge_service as js
    from judge_service import JudgeService

    class Robot:
        group_name = "测试群"
        user_name = "小明"
        user_id = "u1"
        group_id = "g1"
        msg = "你好"

    monkeypatch.setattr(js, "quick_chat", lambda *a, **k: '{"sentiment":{"delta":0.2,"reason":"x"},"learning":{"type":"回复不当","note":"乱写"}}')
    JudgeService().judge_turn(db, Robot(), affection_on=True)
    assert "learning_log" not in {r[0] for r in db.fetch_data("SELECT name FROM sqlite_master WHERE type='table'")}


def test_the_speaker_block_has_no_learning_text(db, monkeypatch):
    import main

    class Robot:
        msg_type = "private"
        group_id = None
        user_id = "u1"
        user_name = "小明"

    assert main._speaker_context(Robot(), set()) == ""


def test_the_learning_table_is_dropped_on_startup(db_file):
    """已废弃表在启动时删除（和之前的 group_subscriptions 等一样，幂等）。"""
    from database_manager import DatabaseManager

    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE IF NOT EXISTS learning_log (id INTEGER PRIMARY KEY, note TEXT)")
    conn.commit()
    conn.close()

    DatabaseManager(db_file)  # 再次启动

    conn = sqlite3.connect(db_file)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "learning_log" not in names


def test_the_judge_affection_path_still_works_without_learning(db, monkeypatch):
    """情绪分仍然影响好感度（移除学习后，好感度只由情绪分驱动）。"""
    from affection_service import AffectionService, SCORE_DEFAULT

    AffectionService.apply_judge_delta(db, "u1", "g1", "小明", sentiment=0.3)
    score = AffectionService.get_or_create(db, "u1", "g1", "小明")["affection_score"]
    assert score > SCORE_DEFAULT


def test_apply_judge_delta_no_longer_takes_a_learning_argument():
    import inspect
    from affection_service import AffectionService

    assert "learning" not in inspect.signature(AffectionService.apply_judge_delta).parameters
