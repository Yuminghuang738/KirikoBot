"""记忆探针 eval：不问 judge 主观打分，直接测「上下文里有没有那条事实」。

对每个 case、每个窗口变体（baseline16 / raw24 / raw48 / layered）各问一次
`probe_question`，temperature=0、只答一题，按 `probe_answer_keywords` 命中判对错。
不生成回复、不调 judge，噪声只剩生成侧；原 `memory_replay.py` 保留作辅助。

数据字段（Hina 的 v4 数据集提供）：
    probe_question         str   —— 只能靠较早上下文回答的问题
    probe_answer_keywords  list  —— 答案必须命中的关键词
    probe_match            str   —— "all"（默认，全部命中）或 "any"（命中一个即可）

判据：
    raw48 相对 baseline 准确率 ≥ +15pp    → 窗口扩容达标
    layered 相对 raw48  准确率 ≥ +10pp    → 摘要带来增量

用法：
    python3 eval/memory_probe.py --db <副本> --cases eval/memory_cases.json
    python3 eval/memory_probe.py --db <副本> --cases ... --resume   # 被杀后续跑
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from memory_replay import (  # noqa: E402
    LAYERED_RAW,
    MODEL,
    VARIANTS,
    VAR_WINDOWS,
    _as_messages,
    _post,
    _read_token,
    _SUMMARY_METRICS,
    _usage_summary,
    _wire_summary_metrics,
    connect_ro,
    fetch_context_rows,
    render_summary_block,
    summarize_older,
    window_rows,
)

PROBE_SYSTEM = """你只根据【上下文】回答问题。不要编造上下文里没有的信息，不要寒暄，不要解释。
只输出答案本身，尽量短（人名、数字、专有名词优先）。"""

PROBE_MAX_TOKENS = 60
RAW48_MIN_GAIN_PP = 15.0
LAYERED_MIN_GAIN_PP = 10.0


def _ctx_text(messages: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"{'用户' if m['role'] == 'user' else 'Kiriko'}：{m['content']}"
        for m in messages
    )


def score_answer(answer: str, keywords: list[str], match: str = "all") -> tuple[bool, list[str]]:
    """关键词命中判定：默认要求全部命中，`match="any"` 时命中一个即可。"""
    text = str(answer or "").lower()
    cleaned = [str(kw).strip() for kw in (keywords or []) if str(kw).strip()]
    matched = [kw for kw in cleaned if kw.lower() in text]
    if not cleaned:
        return False, []
    hit = bool(matched) if match == "any" else len(matched) == len(cleaned)
    return hit, matched


def probe_call(context: str, question: str, key: str, tag: str) -> tuple[str, float]:
    user = f"【上下文】\n{context}\n\n【问题】\n{question}"
    payload = {
        "model": MODEL,
        "temperature": 0,
        "max_tokens": PROBE_MAX_TOKENS,
        "stream": False,
        "thinking": {"type": "disabled"},
        "messages": [
            {"role": "system", "content": PROBE_SYSTEM},
            {"role": "user", "content": user},
        ],
    }
    started = time.perf_counter()
    answer = _post(payload, key, tag=tag)
    return answer, round((time.perf_counter() - started) * 1000, 1)


def _build_contexts(rows: list[dict[str, Any]], summary: str) -> dict[str, str]:
    contexts: dict[str, str] = {}
    for variant in VARIANTS:
        history = _as_messages(window_rows(rows, VAR_WINDOWS[variant]))
        text = _ctx_text(history)
        if variant == "layered" and summary:
            text = render_summary_block(summary) + "\n" + text
        contexts[variant] = text
    return contexts


def _load_done_ids(jsonl_path: str) -> set[str]:
    done: set[str] = set()
    for line in Path(jsonl_path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            done.add(str(json.loads(line)["case_id"]))
        except (ValueError, KeyError, TypeError):
            continue
    return done


def probe_case(case: dict[str, Any], conn: Any, key: str, dry_run: bool) -> dict[str, Any] | None:
    question = str(case.get("probe_question") or "").strip()
    keywords = [str(k).strip() for k in (case.get("probe_answer_keywords") or []) if str(k).strip()]
    if not question or not keywords:
        return None
    match = str(case.get("probe_match") or "all").lower()
    if match not in ("all", "any"):
        match = "all"

    rows = fetch_context_rows(conn, case)
    summary = "" if dry_run else summarize_older(rows[:-LAYERED_RAW], key)
    contexts = _build_contexts(rows, summary)

    variants: dict[str, dict[str, Any]] = {}
    for variant in VARIANTS:
        if dry_run:
            answer, latency = "", 0.0
        else:
            answer, latency = probe_call(contexts[variant], question, key, tag=f"probe_{variant}")
        hit, matched = score_answer(answer, keywords, match)
        variants[variant] = {
            "answer": answer,
            "hit": hit,
            "matched": matched,
            "error": answer.startswith("[ERROR]"),
            "latency_ms": latency,
        }
    return {
        "case_id": case["case_id"],
        "kind": case.get("kind", "case"),
        "category": case.get("category"),
        "probe_question": question,
        "probe_answer_keywords": keywords,
        "probe_match": match,
        "summary_len": len(summary),
        "variants": variants,
    }


def _accuracy(entries: list[dict[str, Any]]) -> dict[str, Any]:
    if not entries:
        return {"case_count": 0, "accuracy": {}, "delta_pp": {}}
    accuracy = {
        variant: round(sum(1 for e in entries if e["variants"][variant]["hit"]) / len(entries), 4)
        for variant in VARIANTS
    }
    base = accuracy["baseline"]
    delta = {
        variant: round((accuracy[variant] - base) * 100, 1)
        for variant in VARIANTS[1:]
    }
    return {"case_count": len(entries), "accuracy": accuracy, "delta_pp": delta}


def summarize_probe(results: list[dict[str, Any]]) -> dict[str, Any]:
    overall = _accuracy(results)
    by_kind = {
        kind: _accuracy([r for r in results if r.get("kind") == kind])
        for kind in ("case", "needle")
    }
    by_category = {
        category: _accuracy([r for r in results if r.get("category") == category])
        for category in ("group", "private")
    }
    raw48_gain = overall["delta_pp"].get("raw48", 0.0)
    accuracy = overall["accuracy"]
    layered_gain = round(
        (accuracy.get("layered", 0.0) - accuracy.get("raw48", 0.0)) * 100, 1,
    ) if accuracy else 0.0
    return {
        "overall": overall,
        "by_kind": by_kind,
        "by_category": by_category,
        "thresholds": {
            "raw48_vs_baseline_pp": raw48_gain,
            "raw48_min_pp": RAW48_MIN_GAIN_PP,
            "raw48_pass": raw48_gain >= RAW48_MIN_GAIN_PP,
            "layered_vs_raw48_pp": layered_gain,
            "layered_min_pp": LAYERED_MIN_GAIN_PP,
            "layered_pass": layered_gain >= LAYERED_MIN_GAIN_PP,
        },
    }


def run(cases: list[dict[str, Any]], db_path: str, limit: int | None = None,
        dry_run: bool = False, jsonl_path: str | None = None,
        resume: bool = False) -> dict[str, Any]:
    key = "" if dry_run else _read_token()
    _wire_summary_metrics()
    conn = connect_ro(db_path)
    selected = cases[: limit or len(cases)]
    done: set[str] = set()
    if jsonl_path:
        if resume and Path(jsonl_path).exists():
            done = _load_done_ids(jsonl_path)
        else:
            Path(jsonl_path).write_text("", encoding="utf-8")

    results: list[dict[str, Any]] = []
    skipped = 0
    for index, case in enumerate(selected, 1):
        if case["case_id"] in done:
            continue
        entry = probe_case(case, conn, key, dry_run)
        if entry is None:
            skipped += 1
            print(f"[{index}/{len(selected)}] {case['case_id']} 跳过（缺 probe 字段）",
                  file=sys.stderr, flush=True)
            continue
        results.append(entry)
        if jsonl_path:
            with open(jsonl_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        hits = "".join(
            "1" if entry["variants"][variant]["hit"] else "0" for variant in VARIANTS
        )
        print(f"[{index}/{len(selected)}] {entry['case_id']} {hits}", file=sys.stderr, flush=True)

    usage = _usage_summary()
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "model": MODEL,
        "dry_run": dry_run,
        "db": Path(db_path).name,
        "case_count": len(results),
        "skipped_missing_probe": skipped,
        "results": results,
        "summary": summarize_probe(results),
        "usage": usage,
        "token_cost": {
            "baseline_prompt_tokens": usage.get("probe_baseline", {}).get("prompt_tokens", 0),
            "summary_prompt_tokens": _SUMMARY_METRICS["prompt_tokens"],
            "summary_calls": _SUMMARY_METRICS["calls"],
            "variants": {
                variant: usage.get(f"probe_{variant}", {}).get("prompt_tokens", 0)
                for variant in VARIANTS
            },
        },
        "notes": (
            "只测上下文里的事实可用性：每变体一问一答、temperature=0、不生成回复、不调 judge；"
            "system 不含 PERSONA，结论用于变体间相对比较。"
        ),
    }


def print_summary(summary: dict[str, Any]) -> None:
    overall = summary["overall"]
    print(f"cases={overall['case_count']}")
    if not overall["case_count"]:
        return
    for variant in VARIANTS:
        delta = overall["delta_pp"].get(variant, 0.0)
        print(f"  {variant:<9} acc={overall['accuracy'][variant]:.3f} delta={delta:+.1f}pp")
    for kind, stats in summary["by_kind"].items():
        if stats["case_count"]:
            print(f"  [{kind}] n={stats['case_count']} "
                  + " ".join(f"{v}={stats['accuracy'][v]:.3f}" for v in VARIANTS))
    thresholds = summary["thresholds"]
    print(f"  raw48 vs baseline: {thresholds['raw48_vs_baseline_pp']:+.1f}pp "
          f"(need ≥{thresholds['raw48_min_pp']:.0f}) -> {'PASS' if thresholds['raw48_pass'] else 'FAIL'}")
    print(f"  layered vs raw48 : {thresholds['layered_vs_raw48_pp']:+.1f}pp "
          f"(need ≥{thresholds['layered_min_pp']:.0f}) -> {'PASS' if thresholds['layered_pass'] else 'FAIL'}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--cases", default="eval/memory_cases.json")
    parser.add_argument("--out", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--jsonl", default="", help="逐条 checkpoint 路径；默认 <out>.jsonl")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)

    raw = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    cases = [*raw.get("cases", []), *raw.get("needles", [])]
    out = Path(args.out) if args.out else (
        Path("eval/out") / f"memory_probe_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    jsonl = args.jsonl or (str(out) + ".jsonl")
    payload = run(cases, args.db, limit=args.limit or None, dry_run=args.dry_run,
                  jsonl_path=jsonl, resume=args.resume)
    if args.resume and not payload["results"]:
        print("checkpoint 里所有 case 都已完成，本次没有新结果；不覆盖 out 文件")
        return 0
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print_summary(payload["summary"])
    print(f"-> {out}")
    print(f"checkpoint -> {jsonl} (resume with --resume)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
