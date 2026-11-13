"""记忆探针 eval：关键词判定、temperature=0、四变体结构。"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "eval"))

import memory_probe  # noqa: E402
import memory_replay  # noqa: E402


def _make_db(tmp_path: Path, rows: int = 60) -> str:
    path = tmp_path / "probe.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE history("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, group_id TEXT,"
        "role TEXT, content TEXT, tool_calls TEXT)"
    )
    for i in range(rows):
        role = "user" if i % 2 == 0 else "assistant"
        conn.execute(
            "INSERT INTO history(user_id, group_id, role, content, tool_calls) VALUES(?,?,?,?,?)",
            ("u1", "g1", role, f"消息{i}", ""),
        )
    conn.commit()
    conn.close()
    return str(path)


def _case(**overrides):
    case = {
        "case_id": "P001", "user_id": "u1", "group_id": "g1",
        "target_history_id": 61, "kind": "case", "category": "group",
        "probe_question": "用户喜欢哪把琴？",
        "probe_answer_keywords": ["PAC612"],
    }
    case.update(overrides)
    return case


# ── 关键词判定 ───────────────────────────────────────────────────

def test_score_answer_all_requires_every_keyword():
    assert memory_probe.score_answer("他最后买了 PAC612", ["PAC612"]) == (True, ["PAC612"])
    assert memory_probe.score_answer("明天去琴行", ["明天", "琴行"]) == (True, ["明天", "琴行"])
    assert memory_probe.score_answer("明天再说", ["明天", "琴行"]) == (False, ["明天"])


def test_score_answer_any_mode():
    hit, matched = memory_probe.score_answer("明天再说", ["明天", "琴行"], match="any")
    assert hit is True
    assert matched == ["明天"]


def test_score_answer_is_case_insensitive_and_ignores_blank_keywords():
    assert memory_probe.score_answer("推荐 pac612", ["PAC612"]) == (True, ["PAC612"])
    assert memory_probe.score_answer("推荐 pac612", ["", "  "]) == (False, [])


# ── 单次探针调用 ─────────────────────────────────────────────────

def test_probe_call_is_temperature_zero_and_short(monkeypatch):
    captured = {}

    def fake_post(payload, key, retries=3, tag=""):
        captured["payload"] = payload
        captured["tag"] = tag
        return "PAC612"

    monkeypatch.setattr(memory_probe, "_post", fake_post)
    answer, latency = memory_probe.probe_call("上下文", "问题", "k", tag="probe_baseline")

    assert answer == "PAC612"
    payload = captured["payload"]
    assert payload["temperature"] == 0
    assert payload["max_tokens"] == memory_probe.PROBE_MAX_TOKENS
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["messages"][0]["content"] == memory_probe.PROBE_SYSTEM
    assert "【上下文】" in payload["messages"][1]["content"]
    assert "【问题】" in payload["messages"][1]["content"]
    assert captured["tag"] == "probe_baseline"
    assert latency >= 0


# ── 四变体跑通 ───────────────────────────────────────────────────

def test_run_dry_run_builds_four_variants_and_skips_missing_probe(tmp_path):
    db = _make_db(tmp_path)
    jsonl = tmp_path / "probe.jsonl"

    payload = memory_probe.run(
        [_case(), _case(case_id="P002", probe_question="", probe_answer_keywords=[])],
        db, dry_run=True, jsonl_path=str(jsonl),
    )

    assert payload["case_count"] == 1
    assert payload["skipped_missing_probe"] == 1
    entry = payload["results"][0]
    assert set(entry["variants"]) == {"baseline", "raw24", "raw48", "layered"}
    assert entry["variants"]["baseline"]["hit"] is False
    assert len(jsonl.read_text(encoding="utf-8").splitlines()) == 1
    assert payload["summary"]["thresholds"]["raw48_pass"] is False


def test_run_resume_skips_done_cases(tmp_path):
    db = _make_db(tmp_path)
    jsonl = tmp_path / "probe.jsonl"
    case = _case()

    memory_probe.run([case], db, dry_run=True, jsonl_path=str(jsonl))
    again = memory_probe.run([case], db, dry_run=True, jsonl_path=str(jsonl), resume=True)

    assert again["results"] == []
    assert again["case_count"] == 0


# ── 汇总与判据 ───────────────────────────────────────────────────

def _entry(hits: dict[str, bool], kind="case", category="group"):
    return {
        "case_id": f"c{len(hits)}", "kind": kind, "category": category,
        "variants": {v: {"hit": h} for v, h in hits.items()},
    }


def test_summarize_probe_thresholds():
    # 10 条：baseline 5、raw48 7（+20pp，过 15）、layered 8（比 raw48 +10pp，过 10）
    results = []
    for i in range(10):
        results.append(_entry({
            "baseline": i < 5,
            "raw24": i < 5,
            "raw48": i < 7,
            "layered": i < 8,
        }))

    summary = memory_probe.summarize_probe(results)

    assert summary["overall"]["accuracy"]["baseline"] == 0.5
    assert summary["thresholds"]["raw48_vs_baseline_pp"] == 20.0
    assert summary["thresholds"]["raw48_pass"] is True
    assert summary["thresholds"]["layered_vs_raw48_pp"] == 10.0
    assert summary["thresholds"]["layered_pass"] is True


def test_summarize_probe_stratifies_kind_and_category():
    results = [
        _entry({"baseline": True, "raw24": True, "raw48": True, "layered": True}, "case", "group"),
        _entry({"baseline": False, "raw24": True, "raw48": True, "layered": False}, "needle", "private"),
    ]

    summary = memory_probe.summarize_probe(results)

    assert summary["by_kind"]["case"]["case_count"] == 1
    assert summary["by_kind"]["needle"]["accuracy"]["baseline"] == 0.0
    assert summary["by_category"]["private"]["accuracy"]["raw24"] == 1.0


def test_cli_dry_run_writes_json(tmp_path, capsys):
    db = _make_db(tmp_path)
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps({"cases": [_case()], "needles": []}), encoding="utf-8")
    out = tmp_path / "probe_out.json"

    rc = memory_probe.main([
        "--db", db, "--cases", str(cases), "--out", str(out), "--dry-run",
    ])

    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["case_count"] == 1
    assert payload["db"] == "probe.db"
