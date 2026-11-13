"""分层记忆 A/B replay（3b）：baseline 16 条 vs layered 24 条 + 摘要。

* 摘要只用目标回合**之前**的消息生成，绝不把未来信息喂进去。
* 本工具**只测记忆层**：system 用 PERSONA，不挂工具 schema / 群背景 / 画像，
  结果不能代表完整生产链路的绝对质量，只能用于 baseline vs layered 的相对比较。
* 只读 DB 副本；不写生产库。
* 双顺序 pairwise judge（A/B 与 B/A 各一次）抵消位置偏差，外加锚点 yes/no。
* 输出 eval/out/memory_<ts>.json；`--dry-run` 不联网，只验证构造与输出结构。

用法：
    python3 eval/memory_replay.py --db <副本> --cases eval/memory_cases.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

APP = Path(__file__).resolve().parents[1] / "KirikoBot"
sys.path.insert(0, str(APP))

from chat_history import to_messages  # noqa: E402
from memory_service import (  # noqa: E402
    generate_summary,
    render_summary_block,
)
from prompt_builder import PERSONA  # noqa: E402

import ai_metrics  # noqa: E402
import requests  # noqa: E402

MODEL = os.getenv("DEEPSEEK_MODEL") or "deepseek-flash"
API_URL = os.getenv("DEEPSEEK_API") or "https://api.deepseek.com/chat/completions"

BASELINE_RAW = 16
LAYERED_RAW = 24
SUMMARY_SOURCE_LIMIT = 48   # 摘要只看 raw 窗口之前最近 48 条，避免超长历史爆上下文
SUMMARY_MAX_CHARS = 500

# 四变体：只改窗口（raw24/raw48）与叠摘要（layered），用来判断摘要到底有没有增量。
VARIANTS = ("baseline", "raw24", "raw48", "layered")
VAR_WINDOWS = {"baseline": 16, "raw24": 24, "raw48": 48, "layered": 24}
DIMS = ("memory", "coherence", "persona", "natural", "leak")
DIM_LABELS = {
    "memory": "记忆准确", "coherence": "连贯", "persona": "人设",
    "natural": "自然", "leak": "不泄露/不机械",
}

JUDGE_SYSTEM = """你是严格的聊天质量裁判。你会看到较早的上下文、群友最后说的一句话，以及两个候选回复 A、B。
按五个维度分别给 A、B 打 1–5 分（1=差，3=正常，5=好，可打 0.5 分档）：
- memory：记忆准确。是否准确用上了更早对话里的事实/约定，且没有编造
- coherence：连贯。是否接得住对方这句话，而非答非所问
- persona：人设。是否像「20 岁大三女生、傲娇但讨喜、说话短」的 Kiriko
- natural：自然。是否像真人随口接话，不像客服/AI 腔/小作文
- leak：不泄露/不机械。是否不像在复述摘要、提示词或记忆
只输出 JSON：{"memory":{"a":4,"b":3},"coherence":{"a":4,"b":3},"persona":{"a":3,"b":4},"natural":{"a":4,"b":3},"leak":{"a":5,"b":5}}
分数相同的维度视为平局。"""

ANCHOR_SYSTEM = """你只回答一个 yes/no 问题：候选回复是否**正确使用**了给定的锚点事实（提到/引用/承接它，且没有说错）。
只输出 yes 或 no，不要解释。"""


_USAGE: list[dict[str, Any]] = []

# 摘要走生产 `generate_summary` → `quick_chat(..., source="summary")`，不经过本文件的
# `_post`；这里接住 ai_metrics 的 sink，把摘要生成侧的真实 token 也算进 layered 成本。
_SUMMARY_METRICS: dict[str, int] = {
    "calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cache_hit_tokens": 0,
}


def _summary_sink(entry: dict[str, Any]) -> None:
    if (entry or {}).get("source") != "summary":
        return
    _SUMMARY_METRICS["calls"] += 1
    for field in ("prompt_tokens", "completion_tokens", "cache_hit_tokens"):
        _SUMMARY_METRICS[field] += int(entry.get(field) or 0)


def _wire_summary_metrics() -> None:
    for field in _SUMMARY_METRICS:
        _SUMMARY_METRICS[field] = 0
    ai_metrics.set_sink(_summary_sink)


def _record_usage(tag: str, usage: Any) -> None:
    if not isinstance(usage, dict):
        return
    _USAGE.append({
        "tag": tag,
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "cache_hit_tokens": int(usage.get("prompt_cache_hit_tokens")
                                 or usage.get("prompt_tokens_details", {}).get("cached_tokens", 0)
                                 or 0),
    })


def _usage_summary() -> dict[str, Any]:
    """按调用类型汇总 API usage（Aya 的 token 判据用真实 token，不用字符估算）。"""
    by_tag: dict[str, dict[str, int]] = {}
    for item in _USAGE:
        agg = by_tag.setdefault(item["tag"], {
            "calls": 0, "prompt_tokens": 0,
            "completion_tokens": 0, "cache_hit_tokens": 0,
        })
        agg["calls"] += 1
        for key in ("prompt_tokens", "completion_tokens", "cache_hit_tokens"):
            agg[key] += int(item.get(key) or 0)
    return by_tag


def _read_token() -> str:
    env_file = APP / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("DEEPSEEK_TOKEN"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return os.getenv("DEEPSEEK_TOKEN") or ""


def _post(payload: dict[str, Any], key: str, retries: int = 3, tag: str = "") -> str:
    last = ""
    for i in range(retries):
        try:
            resp = requests.post(
                API_URL, json=payload, timeout=90,
                headers={"Authorization": f"Bearer {key}"},
            )
            if resp.status_code == 200:
                data = resp.json()
                _record_usage(tag, data.get("usage"))
                message = data["choices"][0]["message"]
                return (message.get("content") or "").strip()
            last = f"HTTP {resp.status_code}: {resp.text[:120]}"
        except Exception as exc:  # noqa: BLE001
            last = str(exc)
        time.sleep(1.5 * (i + 1))
    return f"[ERROR] {last}"


def connect_ro(db_path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def fetch_context_rows(conn: sqlite3.Connection, case: dict[str, Any]) -> list[dict[str, Any]]:
    """目标回合之前、该 user+scope 的所有消息（不含 target 本身）。"""
    scope = "group_id IS NULL" if case.get("group_id") is None else "group_id = ?"
    params: tuple[Any, ...] = (case["user_id"],) if case.get("group_id") is None \
        else (case["user_id"], str(case["group_id"]))
    rows = conn.execute(
        "SELECT id, role, content, tool_calls FROM history "
        f"WHERE user_id = ? AND {scope} AND id < ? AND role IN ('user','assistant') "
        "ORDER BY id ASC",
        (*params, int(case["target_history_id"])),
    ).fetchall()
    return [
        {"id": r[0], "role": r[1], "content": r[2] or "", "tool_calls": r[3] or ""}
        for r in rows
    ]


def window_rows(rows: list[dict[str, Any]], size: int) -> list[dict[str, Any]]:
    return rows[-size:]


def _as_messages(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return to_messages([(r["role"], r["content"], r["tool_calls"], "") for r in rows])


def summarize_older(older: list[dict[str, Any]], key: str) -> str:
    """把 raw 窗口之外最近的 48 条压成 ≤500 字摘要；与生产走同一个 generate_summary。"""
    older = older[-SUMMARY_SOURCE_LIMIT:]
    lines = []
    for row in older:
        content = (row.get("content") or "").strip()
        if not content or content == "[已调用工具处理]":
            continue
        lines.append(f"{'用户' if row['role'] == 'user' else 'Kiriko'}：{content}")
    if not lines:
        return ""
    summary, _ = generate_summary("\n".join(lines))
    return summary


def generate_reply(history: list[dict[str, Any]], user_text: str, key: str,
                   tag: str = "") -> str:
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": PERSONA},
            *history,
            {"role": "user", "content": user_text},
        ],
        "max_tokens": 800,
        "temperature": 0,
        "thinking": {"type": "enabled"},
        "reasoning_effort": os.getenv("DEEPSEEK_REASONING_EFFORT") or "low",
        "stream": False,
    }
    return _post(payload, key, tag=tag)


def _context_text(history: list[dict[str, Any]]) -> str:
    """judge 看到的上下文：最多最近 24 条（layered 实际窗口）。"""
    return "\n".join(
        f"{'用户' if m['role'] == 'user' else 'Kiriko'}：{m['content']}"
        for m in history[-LAYERED_RAW:]
    )


def judge_call(context: str, last_user: str, a: str, b: str, key: str,
               tag: str = "judge") -> dict[str, Any]:
    user = (f"【较早的上下文】\n{context}\n\n【群友最后说】\n{last_user}\n\n"
            f"【回复 A】\n{a}\n\n【回复 B】\n{b}")
    payload = {
        "model": MODEL,
        "temperature": 0,
        "max_tokens": 300,
        "stream": False,
        "thinking": {"type": "disabled"},
        "messages": [
            {"role": "system", "content": JUDGE_SYSTEM},
            {"role": "user", "content": user},
        ],
    }
    return _parse_judge(_post(payload, key, tag=tag))


def _parse_judge(raw: str) -> dict[str, Any]:
    match = re.search(r"\{.*\}", raw or "", re.S)
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
    except (json.JSONDecodeError, ValueError):
        return {}
    out: dict[str, Any] = {}
    for dim in DIMS:
        value = data.get(dim)
        if isinstance(value, str) and value in ("A", "B", "平"):
            out[dim] = value
        elif isinstance(value, dict):
            a_score = float(value.get("a", 0) or 0)
            b_score = float(value.get("b", 0) or 0)
            win = value.get("win") or value.get("winner")
            if win not in ("A", "B", "平"):
                win = "A" if a_score > b_score else ("B" if b_score > a_score else "平")
            out[dim] = {"win": win, "a": a_score, "b": b_score}
    return out


def anchor_call(context: str, last_user: str, reply: str, anchor_fact: str, key: str,
                tag: str = "anchor") -> bool | None:
    if not anchor_fact:
        return None
    user = (f"【上下文】\n{context}\n\n【群友最后说】\n{last_user}\n\n"
            f"【锚点事实】\n{anchor_fact}\n\n【候选回复】\n{reply}")
    payload = {
        "model": MODEL,
        "temperature": 0,
        "max_tokens": 8,
        "stream": False,
        "thinking": {"type": "disabled"},
        "messages": [
            {"role": "system", "content": ANCHOR_SYSTEM},
            {"role": "user", "content": user},
        ],
    }
    raw = _post(payload, key, tag=tag).strip().lower()
    if raw.startswith("yes") or raw.startswith("是"):
        return True
    if raw.startswith("no") or raw.startswith("否"):
        return False
    return None


def _normalize(entry: Any) -> tuple[str | None, float | None, float | None]:
    if isinstance(entry, str):
        return entry, None, None
    if isinstance(entry, dict):
        win = entry.get("win")
        a_score, b_score = entry.get("a"), entry.get("b")
        if win not in ("A", "B", "平") and a_score is not None and b_score is not None:
            win = "A" if float(a_score) > float(b_score) else (
                "B" if float(b_score) > float(a_score) else "平"
            )
        return win, a_score, b_score
    return None, None, None


def combine_pair(first: dict[str, Any], second: dict[str, Any],
                 variant: str = "layered") -> dict[str, Any]:
    """first 的 A=baseline/B=layered；second 的 A=layered/B=baseline。

    两次结论归一后一致才算赢，否则算平；分数取两次平均。
    """
    winners: dict[str, str] = {}
    scores: dict[str, dict[str, float]] = {"baseline": {}, variant: {}}
    for dim in DIMS:
        w1, a1, b1 = _normalize(first.get(dim))
        w2, a2, b2 = _normalize(second.get(dim))
        norm1 = {"A": "baseline", "B": variant, "平": "平"}.get(w1) if w1 else None
        norm2 = {"A": variant, "B": "baseline", "平": "平"}.get(w2) if w2 else None
        if norm1 and norm2 and norm1 == norm2:
            winners[dim] = norm1
        else:
            winners[dim] = "平"
        # first: a=baseline, b=layered；second: a=layered, b=baseline
        if a1 is not None and b2 is not None:
            scores["baseline"][dim] = round((a1 + b2) / 2, 2)
        if b1 is not None and a2 is not None:
            scores[variant][dim] = round((b1 + a2) / 2, 2)
    return {"winner": winners, "scores": scores}


def run(cases: list[dict[str, Any]], db_path: str, limit: int | None = None,
        dry_run: bool = False) -> dict[str, Any]:
    key = "" if dry_run else _read_token()
    _USAGE.clear()
    _wire_summary_metrics()
    conn = connect_ro(db_path)
    results: list[dict[str, Any]] = []

    for case in cases[: limit or len(cases)]:
        rows = fetch_context_rows(conn, case)
        if len(rows) < 2 or rows[-1]["role"] != "user":
            continue
        last_user = rows[-1]["content"]
        context = rows[:-1]
        summary = "" if dry_run else summarize_older(context[:-LAYERED_RAW], key)
        layered_user = last_user
        if summary:
            layered_user = render_summary_block(summary) + "\n" + last_user

        anchor_short = str(case.get("anchor_fact_short") or case.get("anchor_fact") or "").strip()
        anchor_semantic = str(case.get("anchor_semantic") or case.get("anchor_fact") or anchor_short).strip()

        started = time.perf_counter()
        entry: dict[str, Any] = {
            "case_id": case["case_id"],
            "kind": case.get("kind", "case"),
            "category": case.get("category"),
            "summary": summary,
            "anchor_fact_short": anchor_short,
            "anchor_fact_semantic": anchor_semantic,
            "anchor_in_summary": bool(anchor_short and anchor_short.lower() in summary.lower()),
            "variants": {},
            "replies": {},
            "anchors": {},
            "judge_vs_baseline": {},
        }
        histories: dict[str, list[dict[str, Any]]] = {}
        for variant in VARIANTS:
            hist = _as_messages(window_rows(context, VAR_WINDOWS[variant]))
            histories[variant] = hist
            user_text = layered_user if variant == "layered" else last_user
            reply = "" if dry_run else generate_reply(hist, user_text, key, tag=variant)
            entry["variants"][variant] = {
                "context_messages": len(hist),
                "reply_len": len(reply),
                "summary_len": len(summary) if variant == "layered" else 0,
            }
            entry["replies"][variant] = reply
        entry["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)

        if not dry_run:
            # judge 看最宽的窗口 + 摘要，避免把「更早事实」误判成编造。
            judge_ctx = _context_text(histories["raw48"])
            if summary:
                judge_ctx = f"【更早的摘要】\n{summary}\n{judge_ctx}"
            for variant in VARIANTS:
                entry["anchors"][variant] = anchor_call(
                    judge_ctx, last_user, entry["replies"][variant], anchor_semantic, key, tag=variant,
                )
            for variant in VARIANTS[1:]:
                entry["judge_vs_baseline"][variant] = combine_pair(
                    judge_call(judge_ctx, last_user, entry["replies"]["baseline"], entry["replies"][variant], key),
                    judge_call(judge_ctx, last_user, entry["replies"][variant], entry["replies"]["baseline"], key),
                    variant=variant,
                )
        results.append(entry)

    summary_stats = summarize_results(results)
    usage = _usage_summary()
    baseline_tokens = usage.get("baseline", {}).get("prompt_tokens", 0)
    summary_tokens = _SUMMARY_METRICS["prompt_tokens"] or usage.get("summary", {}).get("prompt_tokens", 0)
    token_cost: dict[str, Any] = {
        "baseline_prompt_tokens": baseline_tokens,
        "summary_prompt_tokens": summary_tokens,
        "summary_calls": _SUMMARY_METRICS["calls"],
        "summary_completion_tokens": _SUMMARY_METRICS["completion_tokens"],
        "variants": {},
    }
    for variant in VARIANTS[1:]:
        tokens = usage.get(variant, {}).get("prompt_tokens", 0) + (summary_tokens if variant == "layered" else 0)
        token_cost["variants"][variant] = {
            "prompt_tokens": tokens,
            "ratio": round((tokens - baseline_tokens) / baseline_tokens, 3) if baseline_tokens else None,
        }

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "model": MODEL,
        "dry_run": dry_run,
        "db": Path(db_path).name,
        "results": results,
        "summary": summary_stats,
        "usage": usage,
        "token_cost": token_cost,
        "notes": (
            "只测记忆层：system=PERSONA，不含工具 schema / 群背景 / 画像；"
            "四变体 baseline16 / raw24 / raw48 / layered(24+摘要)，"
            "结论只用于相对比较，不代表完整生产链路。"
        ),
    }


def measure_summary_cost(cases: list[dict[str, Any]], db_path: str,
                         dry_run: bool = False) -> dict[str, Any]:
    """只跑摘要生成，量真实 token / 调用次数 / 长度，不跑回复与 judge。

    用途：四变体 A/B 已跑完但摘要 token 漏记时，补这一趟即可算准 layered 成本。
    """
    key = "" if dry_run else _read_token()
    _wire_summary_metrics()
    conn = connect_ro(db_path)
    lengths: list[int] = []
    end_punct = 0
    for case in cases:
        rows = fetch_context_rows(conn, case)
        if len(rows) < 2 or rows[-1]["role"] != "user":
            continue
        older = rows[:-1][:-LAYERED_RAW]
        summary = "" if dry_run else summarize_older(older, key)
        if not summary:
            continue
        lengths.append(len(summary))
        if summary.rstrip().endswith(("。", "！", "？", "…", "」", "】", "!", "?")):
            end_punct += 1
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "dry_run": dry_run,
        "db": Path(db_path).name,
        "summary_count": len(lengths),
        "summary_len_mean": _mean([float(n) for n in lengths]),
        "summary_len_max": max(lengths) if lengths else None,
        "over_limit": sum(1 for n in lengths if n > SUMMARY_MAX_CHARS),
        "end_punct": end_punct,
        "usage": dict(_SUMMARY_METRICS),
    }


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


def summarize_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    compared = VARIANTS[1:]
    wins = {v: {dim: {"baseline": 0, v: 0, "平": 0} for dim in DIMS} for v in compared}
    scores: dict[str, dict[str, list[float]]] = {v: {dim: [] for dim in DIMS} for v in VARIANTS}
    baseline_scores: dict[str, dict[str, list[float]]] = {v: {dim: [] for dim in DIMS} for v in compared}
    anchors = {v: 0 for v in VARIANTS}
    anchor_total = 0
    reply_lens: dict[str, list[int]] = {v: [] for v in VARIANTS}

    for entry in results:
        for variant in compared:
            judge = (entry.get("judge_vs_baseline") or {}).get(variant) or {}
            for dim in DIMS:
                winner = (judge.get("winner") or {}).get(dim)
                if winner in wins[variant][dim]:
                    wins[variant][dim][winner] += 1
                dim_scores = judge.get("scores") or {}
                variant_score = (dim_scores.get(variant) or {}).get(dim)
                base_score = (dim_scores.get("baseline") or {}).get(dim)
                if variant_score is not None:
                    scores[variant][dim].append(float(variant_score))
                if base_score is not None:
                    baseline_scores[variant][dim].append(float(base_score))
        entry_anchors = entry.get("anchors") or {}
        if entry_anchors:
            anchor_total += 1
            for variant in VARIANTS:
                if entry_anchors.get(variant):
                    anchors[variant] += 1
        for variant in VARIANTS:
            reply_lens[variant].append(len((entry.get("replies") or {}).get(variant) or ""))

    score_means: dict[str, dict[str, Any]] = {}
    for variant in compared:
        score_means[variant] = {}
        for dim in DIMS:
            v_mean = _mean(scores[variant][dim])
            b_mean = _mean(baseline_scores[variant][dim])
            score_means[variant][dim] = {
                "baseline": b_mean, variant: v_mean,
                "diff": round(v_mean - b_mean, 2) if v_mean is not None and b_mean is not None else None,
            }

    base_rate = (anchors["baseline"] / anchor_total) if anchor_total else 0
    anchor_hit_rate: dict[str, Any] = {
        variant: round(anchors[variant] / anchor_total, 3) if anchor_total else 0
        for variant in VARIANTS
    }
    anchor_hit_rate["delta_pp"] = {
        variant: round((anchors[variant] / anchor_total - base_rate) * 100, 1) if anchor_total else 0
        for variant in compared
    }

    memory_wins = {variant: wins[variant]["memory"] for variant in compared}
    return {
        "case_count": len(results),
        "wins": wins,
        "memory_wins": memory_wins,
        "memory_win_rate": {
            variant: round(wins[variant]["memory"][variant] / len(results), 3) if results else 0
            for variant in compared
        },
        "score_means": score_means,
        "anchor_hit_rate": anchor_hit_rate,
        "reply_len_mean": {variant: _mean(reply_lens[variant]) for variant in VARIANTS},
    }


def print_summary(summary: dict[str, Any]) -> None:
    print(f"cases={summary['case_count']}")
    for variant in VARIANTS[1:]:
        row = summary["wins"][variant]["memory"]
        print(f"[{variant}] memory wins: {variant}={row[variant]} baseline={row['baseline']} 平={row['平']}")
        for dim in DIMS:
            mean = summary["score_means"][variant][dim]
            print(f"  {DIM_LABELS[dim]:<10} baseline={mean['baseline']} {variant}={mean[variant]} diff={mean['diff']}")
        print(f"  anchor {variant}={summary['anchor_hit_rate'][variant]} "
              f"baseline={summary['anchor_hit_rate']['baseline']} "
              f"delta={summary['anchor_hit_rate']['delta_pp'][variant]}pp")
    print("reply_len:", summary["reply_len_mean"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--cases", default="eval/memory_cases.json")
    parser.add_argument("--out", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--summary-only", action="store_true",
                        help="只跑摘要生成，量 token/长度，不跑回复与 judge")
    args = parser.parse_args(argv)

    raw = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    cases = [*raw.get("cases", []), *raw.get("needles", [])]
    if args.summary_only:
        payload = measure_summary_cost(cases, args.db, dry_run=args.dry_run)
        out = Path(args.out) if args.out else (
            Path("eval/out") / f"summary_cost_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        )
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        print(f"-> {out}")
        return 0
    payload = run(cases, args.db, limit=args.limit or None, dry_run=args.dry_run)

    out = Path(args.out) if args.out else (
        Path("eval/out") / f"memory_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print_summary(payload["summary"])
    print(f"-> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
