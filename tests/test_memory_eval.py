"""分层记忆 eval：样本抽样、变体构造（无未来信息）、双顺序 judge 与输出结构。"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "eval"))

import memory_cases  # noqa: E402
import memory_replay  # noqa: E402

CASE_KEYS = {
    "case_id", "user_id", "group_id", "target_history_id",
    "anchor_history_id", "anchor_fact", "category", "kind",
}


def _make_db(tmp_path: Path, rows_per_scope: int = 60, scopes: int = 10) -> str:
    """scopes 个群 + 同数量私聊 scope，每条交替 user/assistant。"""
    path = tmp_path / "copy.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE history("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, group_id TEXT,"
        "role TEXT, content TEXT, tool_calls TEXT)"
    )
    for n in range(scopes):
        for i in range(rows_per_scope):
            role = "user" if i % 2 == 0 else "assistant"
            conn.execute(
                "INSERT INTO history(user_id, group_id, role, content, tool_calls) "
                "VALUES(?,?,?,?,?)",
                (f"g_user{n}", str(700000 + n), role, f"群消息{n}-{i}", ""),
            )
        for i in range(rows_per_scope):
            role = "user" if i % 2 == 0 else "assistant"
            conn.execute(
                "INSERT INTO history(user_id, group_id, role, content, tool_calls) "
                "VALUES(?,?,?,?,?)",
                (f"p_user{n}", None, role, f"私聊{n}-{i}", ""),
            )
    conn.commit()
    conn.close()
    return str(path)


def _scope_ids(conn, case):
    scope = "group_id IS NULL" if case["group_id"] is None else "group_id = ?"
    params = (case["user_id"],) if case["group_id"] is None else (case["user_id"], case["group_id"])
    return [r[0] for r in conn.execute(
        f"SELECT id FROM history WHERE user_id = ? AND {scope} ORDER BY id", params,
    )]


# ── memory_cases ─────────────────────────────────────────────────

def test_pick_cases_quotas(tmp_path):
    db = _make_db(tmp_path)
    scopes = memory_cases.load_scope_rows(memory_cases.connect_ro(db))

    picked = memory_cases.pick_cases(scopes, n=20, needles=5, seed=7)

    assert len(picked["cases"]) == 20
    assert sum(1 for c in picked["cases"] if c["group_id"] is not None) == 10
    assert sum(1 for c in picked["cases"] if c["group_id"] is None) == 10
    assert len(picked["needles"]) == 5


def test_pick_cases_supports_40_plus_10(tmp_path):
    db = _make_db(tmp_path, scopes=20)
    scopes = memory_cases.load_scope_rows(memory_cases.connect_ro(db))

    picked = memory_cases.pick_cases(scopes, n=40, needles=10, seed=7)

    assert len(picked["cases"]) == 40
    assert sum(1 for c in picked["cases"] if c["group_id"] is not None) == 20
    assert sum(1 for c in picked["cases"] if c["group_id"] is None) == 20
    assert len(picked["needles"]) == 10


def test_samples_store_no_message_text(tmp_path):
    db = _make_db(tmp_path)
    scopes = memory_cases.load_scope_rows(memory_cases.connect_ro(db))
    picked = memory_cases.pick_cases(scopes, n=20, needles=5)

    for case in picked["cases"] + picked["needles"]:
        assert set(case) == CASE_KEYS
        assert "群消息" not in json.dumps(case, ensure_ascii=False)
        assert "私聊" not in json.dumps(case, ensure_ascii=False)


def test_anchor_gaps_match_spec(tmp_path):
    db = _make_db(tmp_path)
    conn = memory_cases.connect_ro(db)
    scopes = memory_cases.load_scope_rows(conn)
    picked = memory_cases.pick_cases(scopes, n=20, needles=5)

    for case in picked["cases"]:
        ids = _scope_ids(conn, case)
        gap = ids.index(case["target_history_id"]) - ids.index(case["anchor_history_id"])
        assert gap >= memory_cases.RAW_WINDOW
        # 目标必须是 assistant 行
        role = conn.execute("SELECT role FROM history WHERE id = ?",
                            (case["target_history_id"],)).fetchone()[0]
        assert role == "assistant"

    for needle in picked["needles"]:
        ids = _scope_ids(conn, needle)
        gap = ids.index(needle["target_history_id"]) - ids.index(needle["anchor_history_id"])
        assert memory_cases.NEEDLE_GAP[0] <= gap <= memory_cases.NEEDLE_GAP[1]


def test_case_file_cli_roundtrip(tmp_path):
    db = _make_db(tmp_path)
    out = tmp_path / "cases.json"
    assert memory_cases.main(["--db", db, "--n", "20", "--needles", "5", "--out", str(out)]) == 0

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert len(payload["cases"]) == 20 and len(payload["needles"]) == 5
    assert "generated_at" in payload


# ── memory_replay 构造 ───────────────────────────────────────────

def test_fetch_context_excludes_target_and_future(tmp_path):
    db = _make_db(tmp_path)
    conn = memory_replay.connect_ro(db)
    case = {"user_id": "g_user0", "group_id": "700000", "target_history_id": 21}

    rows = memory_replay.fetch_context_rows(conn, case)

    assert len(rows) == 20
    assert rows[-1]["id"] == 20
    assert all(r["id"] < case["target_history_id"] for r in rows)


def test_split_variants_window_sizes_and_no_future():
    rows = [{"id": i, "role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"}
            for i in range(40)]

    baseline, layered, older = memory_replay.split_variants(rows)

    assert len(baseline) == memory_replay.BASELINE_RAW
    assert len(layered) == memory_replay.LAYERED_RAW
    assert len(older) == 40 - memory_replay.LAYERED_RAW
    assert layered[0]["id"] == 16
    assert max(r["id"] for r in layered) == 39
    assert max(r["id"] for r in older) == 15


def test_summarize_older_only_uses_given_rows(monkeypatch):
    captured = {}

    def fake_post(payload, key, **kwargs):
        captured["text"] = payload["messages"][-1]["content"]
        return '{"summary":"用户在练琴","facts":[]}'

    monkeypatch.setattr(memory_replay, "_post", fake_post)
    older = [{"role": "user", "content": "很老的事实A", "tool_calls": "", "id": 1}]

    summary = memory_replay.summarize_older(older, "k")

    assert summary == "用户在练琴"
    assert "很老的事实A" in captured["text"]


def test_summarize_older_empty_returns_empty():
    assert memory_replay.summarize_older([], "k") == ""


def test_summarize_older_limits_to_recent_48_and_truncates(monkeypatch):
    captured = {}

    def fake_post(payload, key, **kwargs):
        captured["text"] = payload["messages"][-1]["content"]
        return json.dumps({"summary": "字" * 600, "facts": []})

    monkeypatch.setattr(memory_replay, "_post", fake_post)
    older = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"OLD{i}", "id": i}
        for i in range(60)
    ]

    summary = memory_replay.summarize_older(older, "k")

    assert len(summary) == memory_replay.SUMMARY_MAX_CHARS   # 强制截到 400
    assert "OLD0" not in captured["text"]
    assert "OLD11" not in captured["text"]                   # 只保留最近 48 条
    assert "OLD12" in captured["text"]


def test_parse_judge_accepts_strings_and_score_objects():
    raw = ('```json\n{"memory":"A","coherence":"平",'
           '"persona":{"win":"B","a":3,"b":4},"natural":"A",'
           '"leak":{"a":5,"b":4}}\n```')
    parsed = memory_replay._parse_judge(raw)

    assert parsed["memory"] == "A"
    assert parsed["coherence"] == "平"
    assert parsed["persona"]["win"] == "B"
    # 1–5 分格式：没写 winner 时按分数推断
    assert parsed["leak"] == {"win": "A", "a": 5.0, "b": 4.0}
    assert memory_replay._parse_judge("not json") == {}


def test_combine_pair_requires_both_orders_to_agree():
    first = {"memory": "A", "coherence": "B", "persona": "平", "natural": "A", "leak": "平"}
    second = {"memory": "B", "coherence": "A", "persona": "平", "natural": "A", "leak": "平"}

    combined = memory_replay.combine_pair(first, second)

    assert combined["winner"]["memory"] == "baseline"   # 两次都指向 baseline
    assert combined["winner"]["coherence"] == "layered"
    assert combined["winner"]["persona"] == "平"
    assert combined["winner"]["natural"] == "平"  # 两次指向不同变体 → 平


def test_combine_pair_averages_scores_and_infers_winners():
    first = {"memory": {"a": 5, "b": 2}}
    second = {"memory": {"a": 3, "b": 4}}

    combined = memory_replay.combine_pair(first, second)

    assert combined["scores"]["baseline"]["memory"] == 4.5
    assert combined["scores"]["layered"]["memory"] == 2.5
    # first 判 A=baseline，second 判 B=baseline → 两次一致
    assert combined["winner"]["memory"] == "baseline"


def test_anchor_call_parses_yes_no_and_skips_empty(monkeypatch):
    replies = iter(["yes", "No", "maybe"])

    monkeypatch.setattr(memory_replay, "_post", lambda payload, key, **kwargs: next(replies))

    assert memory_replay.anchor_call("c", "u", "r", "fact", "k") is True
    assert memory_replay.anchor_call("c", "u", "r", "fact", "k") is False
    assert memory_replay.anchor_call("c", "u", "r", "fact", "k") is None
    called = []
    monkeypatch.setattr(memory_replay, "_post", lambda payload, key: called.append(1) or "yes")
    assert memory_replay.anchor_call("c", "u", "r", "", "k") is None
    assert called == []


def test_run_dry_run_structure(tmp_path):
    db = _make_db(tmp_path)
    conn = memory_replay.connect_ro(db)
    case = {
        "case_id": "G001", "user_id": "g_user0", "group_id": "700000",
        "target_history_id": 42, "anchor_history_id": 1, "anchor_fact": "",
        "category": "group", "kind": "case",
    }

    payload = memory_replay.run([case], db, dry_run=True)

    assert payload["dry_run"] is True
    assert len(payload["results"]) == 1
    entry = payload["results"][0]
    assert entry["variants"]["baseline"]["context_messages"] == 16
    assert entry["variants"]["layered"]["context_messages"] == 24
    assert entry["variants"]["layered"]["summary_len"] == 0
    assert entry["baseline_reply"] == "" and entry["layered_reply"] == ""
    assert entry["summary"] == "" and entry["anchor_in_summary"] is False
    assert "judge" not in entry and "anchor" not in entry
    assert payload["summary"]["case_count"] == 1
    assert payload["notes"].startswith("只测记忆层")


def test_run_skips_case_without_trailing_user(tmp_path):
    db = _make_db(tmp_path)
    # target=2 时上下文只有 1 条 assistant 行，没有可回答的 user，跳过
    case = {
        "case_id": "X", "user_id": "g_user0", "group_id": "700000",
        "target_history_id": 2, "anchor_history_id": 1, "anchor_fact": "",
        "category": "group", "kind": "case",
    }
    payload = memory_replay.run([case], db, dry_run=True)
    assert payload["results"] == []


def test_run_keeps_replies_for_audit(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    case = {
        "case_id": "G001", "user_id": "g_user0", "group_id": "700000",
        "target_history_id": 42, "anchor_history_id": 1, "anchor_fact": "在练琴",
        "category": "group", "kind": "case",
    }
    monkeypatch.setattr(memory_replay, "_read_token", lambda: "k")
    monkeypatch.setattr(memory_replay, "summarize_older", lambda older, key: "用户在练琴")
    replies = iter(["baseline 回复原文", "layered 回复原文"])
    monkeypatch.setattr(memory_replay, "generate_reply", lambda hist, user, key, **kwargs: next(replies))
    judge_contexts = []

    def fake_judge(context, last_user, a, b, key):
        judge_contexts.append(context)
        return {}

    monkeypatch.setattr(memory_replay, "judge_call", fake_judge)
    monkeypatch.setattr(memory_replay, "anchor_call", lambda *a, **k: None)

    payload = memory_replay.run([case], db)

    entry = payload["results"][0]
    assert entry["baseline_reply"] == "baseline 回复原文"
    assert entry["layered_reply"] == "layered 回复原文"
    assert entry["summary"] == "用户在练琴"
    assert entry["anchor_fact_short"] == "在练琴"
    assert entry["anchor_fact_semantic"] == "在练琴"
    assert entry["anchor_in_summary"] is True
    assert entry["variants"]["layered"]["summary_len"] == len("用户在练琴")
    # judge 必须看到 layered 的摘要上下文，而不是只有 baseline 的 16 条
    assert len(judge_contexts) == 2
    assert all("【更早的摘要】" in ctx and "用户在练琴" in ctx for ctx in judge_contexts)


def test_anchor_in_summary_false_when_fact_missing(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    case = {
        "case_id": "G002", "user_id": "g_user0", "group_id": "700000",
        "target_history_id": 42, "anchor_history_id": 1, "anchor_fact": "养了一只猫",
        "category": "group", "kind": "needle",
    }
    monkeypatch.setattr(memory_replay, "_read_token", lambda: "k")
    monkeypatch.setattr(memory_replay, "summarize_older", lambda older, key: "用户在练琴")
    monkeypatch.setattr(memory_replay, "generate_reply", lambda hist, user, key, **kwargs: "回复")
    monkeypatch.setattr(memory_replay, "judge_call", lambda *a, **k: {})
    monkeypatch.setattr(memory_replay, "anchor_call", lambda *a, **k: None)

    payload = memory_replay.run([case], db)

    assert payload["results"][0]["anchor_in_summary"] is False


def test_post_records_usage(monkeypatch):
    class FakeResp:
        status_code = 200

        def json(self):
            return {
                "choices": [{"message": {"content": " hi "}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20,
                          "prompt_cache_hit_tokens": 40},
            }

    monkeypatch.setattr(memory_replay.requests, "post", lambda *a, **k: FakeResp())
    memory_replay._USAGE.clear()

    assert memory_replay._post({}, "k", tag="baseline") == "hi"
    summary = memory_replay._usage_summary()
    assert summary["baseline"] == {
        "calls": 1, "prompt_tokens": 100,
        "completion_tokens": 20, "cache_hit_tokens": 40,
    }
    memory_replay._USAGE.clear()


def test_summarize_results_reports_means_and_lengths():
    results = [
        {"judge": {"winner": {"memory": "layered"},
                   "scores": {"memory": {"baseline": 3, "layered": 4}}},
         "anchor": {"baseline": True, "layered": True},
         "baseline_reply": "四个字", "layered_reply": "六个字回复"},
        {"judge": {"winner": {"memory": "平"},
                   "scores": {"memory": {"baseline": 4, "layered": 4}}},
         "anchor": {"baseline": False, "layered": True},
         "baseline_reply": "回复", "layered_reply": "回复回复"},
    ]

    summary = memory_replay.summarize_results(results)

    assert summary["wins"]["memory"] == {"baseline": 0, "layered": 1, "平": 1}
    assert summary["score_means"]["memory"] == {"baseline": 3.5, "layered": 4.0, "diff": 0.5}
    assert summary["anchor_hit_rate"] == {"baseline": 0.5, "layered": 1.0, "delta_pp": 50.0}
    assert summary["reply_len_mean"] == {"baseline": 2.5, "layered": 4.5}


def test_anchor_in_summary_is_case_insensitive(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    case = {
        "case_id": "G003", "user_id": "g_user0", "group_id": "700000",
        "target_history_id": 42, "anchor_history_id": 1, "anchor_fact_short": "Tele",
        "anchor_semantic": "Telegram", "category": "group", "kind": "case",
    }
    monkeypatch.setattr(memory_replay, "_read_token", lambda: "k")
    monkeypatch.setattr(memory_replay, "summarize_older", lambda older, key: "用户提到 telegram 的使用")
    monkeypatch.setattr(memory_replay, "generate_reply", lambda *a, **k: "回复")
    monkeypatch.setattr(memory_replay, "judge_call", lambda *a, **k: {})
    monkeypatch.setattr(memory_replay, "anchor_call", lambda *a, **k: None)

    payload = memory_replay.run([case], db)

    assert payload["results"][0]["anchor_in_summary"] is True
