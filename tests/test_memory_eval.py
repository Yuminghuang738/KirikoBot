"""分层记忆 eval：样本抽样、四变体构造、双顺序 judge 与汇总。"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "eval"))

import memory_cases  # noqa: E402
import memory_replay  # noqa: E402

CASE_KEYS = {
    "case_id", "user_id", "group_id", "target_history_id",
    "anchor_history_id", "anchor_fact", "category", "kind",
}


def _make_db(tmp_path: Path, rows_per_scope: int = 60, scopes: int = 10) -> str:
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
                "INSERT INTO history(user_id, group_id, role, content, tool_calls) VALUES(?,?,?,?,?)",
                (f"g_user{n}", str(700000 + n), role, f"群消息{n}-{i}", ""),
            )
        for i in range(rows_per_scope):
            role = "user" if i % 2 == 0 else "assistant"
            conn.execute(
                "INSERT INTO history(user_id, group_id, role, content, tool_calls) VALUES(?,?,?,?,?)",
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


def _case(target_id=60):
    return {
        "case_id": "G001", "user_id": "g_user0", "group_id": "700000",
        "target_history_id": target_id, "anchor_history_id": 1,
        "anchor_fact_short": "在练琴", "anchor_semantic": "用户答应帮朋友听录音",
        "category": "group", "kind": "case",
    }


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
    assert len(picked["needles"]) == 10


def test_samples_store_no_message_text(tmp_path):
    db = _make_db(tmp_path)
    scopes = memory_cases.load_scope_rows(memory_cases.connect_ro(db))
    picked = memory_cases.pick_cases(scopes, n=20, needles=5)
    for case in picked["cases"] + picked["needles"]:
        assert set(case) == CASE_KEYS
        assert "群消息" not in json.dumps(case, ensure_ascii=False)


def test_anchor_gaps_match_spec(tmp_path):
    db = _make_db(tmp_path)
    conn = memory_cases.connect_ro(db)
    scopes = memory_cases.load_scope_rows(conn)
    picked = memory_cases.pick_cases(scopes, n=20, needles=5)
    for case in picked["cases"]:
        ids = _scope_ids(conn, case)
        gap = ids.index(case["target_history_id"]) - ids.index(case["anchor_history_id"])
        assert gap >= memory_cases.RAW_WINDOW
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


# ── memory_replay 构造 ───────────────────────────────────────────

def test_fetch_context_excludes_target_and_future(tmp_path):
    db = _make_db(tmp_path)
    conn = memory_replay.connect_ro(db)
    rows = memory_replay.fetch_context_rows(conn, _case(target_id=21))
    assert len(rows) == 20
    assert all(r["id"] < 21 for r in rows)


def test_window_rows_sizes():
    rows = [{"id": i} for i in range(60)]
    assert len(memory_replay.window_rows(rows, 16)) == 16
    assert memory_replay.window_rows(rows, 48)[0]["id"] == 12
    assert memory_replay.VAR_WINDOWS == {"baseline": 16, "raw24": 24, "raw48": 48, "layered": 24}


def test_summarize_older_limits_to_recent_48(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        memory_replay, "generate_summary",
        lambda chunk: (captured.update(chunk=chunk) or ("摘要", [])),
    )
    older = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"OLD{i}", "id": i}
        for i in range(60)
    ]

    summary = memory_replay.summarize_older(older, "k")

    assert summary == "摘要"
    assert "OLD0" not in captured["chunk"] and "OLD11" not in captured["chunk"]
    assert "OLD12" in captured["chunk"]


def test_summarize_older_empty_returns_empty():
    assert memory_replay.summarize_older([], "k") == ""


# ── judge / anchor ───────────────────────────────────────────────

def test_parse_judge_accepts_strings_and_score_objects():
    raw = ('```json\n{"memory":"A","coherence":"平",'
           '"persona":{"win":"B","a":3,"b":4},"natural":"A",'
           '"leak":{"a":5,"b":4}}\n```')
    parsed = memory_replay._parse_judge(raw)

    assert parsed["memory"] == "A"
    assert parsed["coherence"] == "平"
    assert parsed["persona"]["win"] == "B"
    assert parsed["leak"] == {"win": "A", "a": 5.0, "b": 4.0}
    assert memory_replay._parse_judge("not json") == {}


def test_combine_pair_supports_any_variant_and_scores():
    first = {"memory": {"a": 5, "b": 2}}
    second = {"memory": {"a": 3, "b": 4}}

    combined = memory_replay.combine_pair(first, second, variant="raw48")

    assert combined["scores"]["baseline"]["memory"] == 4.5
    assert combined["scores"]["raw48"]["memory"] == 2.5
    assert combined["winner"]["memory"] == "baseline"


def test_anchor_call_parses_yes_no_and_skips_empty(monkeypatch):
    replies = iter(["yes", "No", "maybe"])
    monkeypatch.setattr(memory_replay, "_post", lambda payload, key, **kw: next(replies))

    assert memory_replay.anchor_call("c", "u", "r", "fact", "k") is True
    assert memory_replay.anchor_call("c", "u", "r", "fact", "k") is False
    assert memory_replay.anchor_call("c", "u", "r", "fact", "k") is None
    called = []
    monkeypatch.setattr(memory_replay, "_post", lambda payload, key, **kw: called.append(1) or "yes")
    assert memory_replay.anchor_call("c", "u", "r", "", "k") is None
    assert called == []


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
    assert memory_replay._usage_summary()["baseline"]["prompt_tokens"] == 100
    memory_replay._USAGE.clear()


# ── run 四变体 ───────────────────────────────────────────────────

def test_run_dry_run_builds_four_variants(tmp_path):
    db = _make_db(tmp_path)
    payload = memory_replay.run([_case()], db, dry_run=True)
    entry = payload["results"][0]

    assert set(entry["variants"]) == {"baseline", "raw24", "raw48", "layered"}
    ctx = {v: entry["variants"][v]["context_messages"] for v in entry["variants"]}
    assert ctx == {"baseline": 16, "raw24": 24, "raw48": 48, "layered": 24}
    assert entry["variants"]["layered"]["summary_len"] == 0
    assert all(reply == "" for reply in entry["replies"].values())
    assert payload["notes"].startswith("只测记忆层")
    assert set(payload["token_cost"]["variants"]) == {"raw24", "raw48", "layered"}


def test_run_keeps_replies_and_judges_each_variant(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    monkeypatch.setattr(memory_replay, "_read_token", lambda: "k")
    monkeypatch.setattr(memory_replay, "summarize_older", lambda older, key: "用户在练琴")
    monkeypatch.setattr(
        memory_replay, "generate_reply",
        lambda hist, user, key, **kwargs: f"reply-{kwargs.get('tag')}",
    )
    judge_contexts: list[str] = []

    def fake_judge(context, last_user, a, b, key):
        judge_contexts.append(context)
        # 双顺序下都判 baseline 更好：A=baseline 时 a>b，反序时 b>a
        if a == "reply-baseline":
            return {"memory": {"a": 4, "b": 3}}
        return {"memory": {"a": 3, "b": 4}}

    monkeypatch.setattr(memory_replay, "judge_call", fake_judge)
    monkeypatch.setattr(memory_replay, "anchor_call", lambda *a, **k: True)

    payload = memory_replay.run([_case()], db)
    entry = payload["results"][0]

    assert entry["replies"]["raw48"] == "reply-raw48"
    assert entry["summary"] == "用户在练琴"
    assert entry["anchor_in_summary"] is True
    assert set(entry["anchors"]) == {"baseline", "raw24", "raw48", "layered"}
    assert set(entry["judge_vs_baseline"]) == {"raw24", "raw48", "layered"}
    assert len(judge_contexts) == 6          # 3 个变体 × 双顺序
    assert all("【更早的摘要】" in c for c in judge_contexts)
    # 每个变体都是 baseline 胜（A=4/B=3，换序后被归一）
    for variant in ("raw24", "raw48", "layered"):
        assert entry["judge_vs_baseline"][variant]["winner"]["memory"] == "baseline"


def test_run_skips_case_without_trailing_user(tmp_path):
    db = _make_db(tmp_path)
    payload = memory_replay.run([_case(target_id=2)], db, dry_run=True)
    assert payload["results"] == []


def test_anchor_in_summary_is_case_insensitive(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    case = dict(_case(), anchor_fact_short="Tele", anchor_semantic="Telegram")
    monkeypatch.setattr(memory_replay, "_read_token", lambda: "k")
    monkeypatch.setattr(memory_replay, "summarize_older", lambda older, key: "用户提到 telegram")
    monkeypatch.setattr(memory_replay, "generate_reply", lambda *a, **k: "回复")
    monkeypatch.setattr(memory_replay, "judge_call", lambda *a, **k: {})
    monkeypatch.setattr(memory_replay, "anchor_call", lambda *a, **k: None)

    payload = memory_replay.run([case], db)
    assert payload["results"][0]["anchor_in_summary"] is True


def test_token_cost_counts_summary_generation(tmp_path, monkeypatch):
    """layered 的成本必须含摘要生成侧：生产 generate_summary 走 ai_metrics sink。"""
    db = _make_db(tmp_path)
    monkeypatch.setattr(memory_replay, "_read_token", lambda: "k")

    def fake_summarize(older, key):
        memory_replay.ai_metrics.record(
            source="summary", kind="quick",
            usage={"usage": {"prompt_tokens": 1000, "completion_tokens": 50}},
        )
        return "摘要"

    def fake_reply(hist, user, key, tag=""):
        memory_replay._record_usage(tag, {"prompt_tokens": 100, "completion_tokens": 10})
        return "回复"

    monkeypatch.setattr(memory_replay, "summarize_older", fake_summarize)
    monkeypatch.setattr(memory_replay, "generate_reply", fake_reply)
    monkeypatch.setattr(memory_replay, "judge_call", lambda *a, **k: {})
    monkeypatch.setattr(memory_replay, "anchor_call", lambda *a, **k: None)

    payload = memory_replay.run([_case()], db)
    cost = payload["token_cost"]

    assert cost["summary_prompt_tokens"] == 1000
    assert cost["summary_calls"] == 1
    assert cost["variants"]["layered"]["prompt_tokens"] - cost["variants"]["raw24"]["prompt_tokens"] == 1000


def test_summary_sink_ignores_other_sources():
    memory_replay._SUMMARY_METRICS.update(
        {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cache_hit_tokens": 0},
    )
    memory_replay._summary_sink({"source": "judge", "prompt_tokens": 999})
    memory_replay._summary_sink({"source": "summary", "prompt_tokens": 120,
                                "completion_tokens": 30, "cache_hit_tokens": 80})

    assert memory_replay._SUMMARY_METRICS == {
        "calls": 1, "prompt_tokens": 120, "completion_tokens": 30, "cache_hit_tokens": 80,
    }


# ── 摘要成本单跑 ─────────────────────────────────────────────────

def test_measure_summary_cost_reports_tokens_and_lengths(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    monkeypatch.setattr(memory_replay, "_read_token", lambda: "k")

    def fake_summarize(older, key):
        memory_replay.ai_metrics.record(
            source="summary", kind="quick",
            usage={"usage": {"prompt_tokens": 700, "completion_tokens": 40}},
        )
        return "摘要。"

    monkeypatch.setattr(memory_replay, "summarize_older", fake_summarize)

    payload = memory_replay.measure_summary_cost([_case()], db)

    assert payload["summary_count"] == 1
    assert payload["end_punct"] == 1
    assert payload["end_chars"] == {"。": 1}
    assert payload["over_limit"] == 0
    assert payload["usage"]["calls"] == 1
    assert payload["usage"]["prompt_tokens"] == 700


def test_run_checkpoints_each_case_and_resumes(tmp_path, monkeypatch):
    """一次跑 20+ 分钟被杀时，checkpoint 让续跑只补未完成的 case。"""
    db = _make_db(tmp_path)
    monkeypatch.setattr(memory_replay, "_read_token", lambda: "k")
    monkeypatch.setattr(memory_replay, "summarize_older", lambda older, key: "")
    monkeypatch.setattr(memory_replay, "generate_reply", lambda *a, **k: "回复")
    monkeypatch.setattr(memory_replay, "judge_call", lambda *a, **k: {})
    monkeypatch.setattr(memory_replay, "anchor_call", lambda *a, **k: None)
    jsonl = tmp_path / "run.jsonl"
    case = _case()

    payload = memory_replay.run([case], db, jsonl_path=str(jsonl))

    assert payload["summary"]["case_count"] == 1
    assert len(jsonl.read_text(encoding="utf-8").splitlines()) == 1
    assert memory_replay._load_done_ids(str(jsonl)) == {case["case_id"]}

    again = memory_replay.run([case], db, jsonl_path=str(jsonl), resume=True)
    assert again["results"] == []
    # 不带 --resume 重跑会清空旧 checkpoint，不会把上一次的条目算进来
    fresh = memory_replay.run([case], db, jsonl_path=str(jsonl))
    assert fresh["summary"]["case_count"] == 1


# ── 汇总 ─────────────────────────────────────────────────────────

def test_summarize_results_reads_scores_dim_variant_shape():
    def entry(memory_variant, base):
        return {
            "judge_vs_baseline": {"layered": {
                "winner": {"memory": memory_variant},
                "scores": {"baseline": {"memory": base}, "layered": {"memory": 4}},
            }},
            "anchors": {"baseline": True, "raw24": False, "raw48": True, "layered": True},
            "replies": {"baseline": "四个字", "raw24": "回复", "raw48": "回复回复", "layered": "六个字回复"},
        }

    summary = memory_replay.summarize_results([entry("layered", 3), entry("平", 4)])

    assert summary["wins"]["layered"]["memory"] == {"baseline": 0, "layered": 1, "平": 1}
    assert summary["score_means"]["layered"]["memory"] == {
        "baseline": 3.5, "layered": 4.0, "diff": 0.5,
    }
    assert summary["anchor_hit_rate"]["layered"] == 1.0
    assert summary["anchor_hit_rate"]["raw24"] == 0.0
    assert summary["anchor_hit_rate"]["delta_pp"]["raw48"] == 0.0
    assert summary["reply_len_mean"]["layered"] == 5.0
