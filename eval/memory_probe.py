"""记忆探针 eval：不问 judge 主观打分，直接测「上下文里有没有那条事实」。

对每个 case、每个窗口变体（baseline16 / raw24 / raw48 / layered）各问一次
`probe_question`，temperature=0、只答一题，按 `probe_answer_keywords` 命中判对错。
不生成回复、不调 judge，噪声只剩生成侧；原 `memory_replay.py` 保留作辅助。

数据字段（Hina 的 v4 数据集提供）：
    probe_question         str   —— 只能靠较早上下文回答的问题
    probe_answer_keywords  list  —— 答案必须命中的关键词
    probe_match            str   —— "all"（默认，全部命中）或 "any"（命中一个即可）
    probe_type             str   —— 事实/偏好/约定/人名/未完成（用于分层报告）
    probe_tier             str   —— A/B/C（锚点距离分层）
    probe_scope            str   —— group/private（用于分层报告）

判据：
    raw48 相对 baseline 准确率 ≥ +15pp          → 窗口扩容达标
    layered 相对 raw48  准确率 ≥ +10pp 且 token ≤ +30% → 摘要带来增量
        token 门槛用**下发侧** delivery ratio（layered 请求 prompt / baseline）；
        摘要生成是一次性成本，`with_summary` 只报告、不参与判定（生产摊销另算）。
    同题抽检一致性 ≥ 95%（--repeat N，按 tier 分层抽样，不是整轮准确率差）

用法：
    python3 eval/memory_probe.py --db <副本> --cases eval/memory_cases.json
    python3 eval/memory_probe.py ... --repeat 20   # 顺带做确定性自检
    python3 eval/memory_probe.py ... --resume      # 被杀后续跑
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
import unicodedata
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

PROBE_MAX_TOKENS = 80
RAW48_MIN_GAIN_PP = 15.0
LAYERED_MIN_GAIN_PP = 10.0
LAYERED_MAX_TOKEN_RATIO = 0.30
REPEAT_MIN_CONSISTENCY = 0.95

_CODE_KW = re.compile(r"^[a-z0-9_+\-.]+$")


def _ctx_text(messages: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"{'用户' if m['role'] == 'user' else 'Kiriko'}：{m['content']}"
        for m in messages
    )


def _normalize(text: Any) -> str:
    """NFKC（全角→半角）+ 空白归一 + 小写；中文标点保留。"""
    normalized = unicodedata.normalize("NFKC", str(text or ""))
    return re.sub(r"\s+", " ", normalized).strip().lower()


def _keyword_hit(answer_norm: str, keyword_norm: str) -> bool:
    """代码词按边界匹配（PAC612 不命中 PAC6120），中文按子串（忽略空格差异）。"""
    if not keyword_norm:
        return False
    if _CODE_KW.match(keyword_norm):
        pattern = re.compile(
            r"(?<![a-z0-9_])" + re.escape(keyword_norm) + r"(?![a-z0-9_])",
        )
        return bool(pattern.search(answer_norm))
    if keyword_norm in answer_norm:
        return True
    return keyword_norm.replace(" ", "") in answer_norm.replace(" ", "")


def score_answer(answer: str, keywords: list[str], match: str = "all") -> tuple[bool, list[str]]:
    """关键词命中判定：默认要求全部命中，`match="any"` 时命中一个即可。"""
    answer_norm = _normalize(answer)
    cleaned = [(str(kw).strip(), _normalize(kw)) for kw in (keywords or [])]
    cleaned = [(orig, norm) for orig, norm in cleaned if norm]
    if not cleaned:
        return False, []
    matched = [orig for orig, norm in cleaned if _keyword_hit(answer_norm, norm)]
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


def _pick_repeat_ids(cases: list[dict[str, Any]], n: int) -> set[str]:
    """确定性分层抽样：按 probe_tier（缺省按 kind）轮转，覆盖 A/B/C。"""
    if n <= 0:
        return set()
    pools: dict[str, list[str]] = {}
    for case in cases:
        key = str(case.get("probe_tier") or case.get("kind") or "all")
        pools.setdefault(key, []).append(str(case["case_id"]))
    picked: set[str] = set()
    keys = sorted(pools)
    while len(picked) < n and any(pools[key] for key in keys):
        for key in keys:
            if pools[key] and len(picked) < n:
                picked.add(pools[key].pop(0))
    return picked


def _load_done_entries(jsonl_path: str) -> list[dict[str, Any]]:
    """读 checkpoint 里已完成的条目；resume 时并入结果，避免只剩后半段。"""
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in Path(jsonl_path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        case_id = str(entry.get("case_id") or "")
        if not case_id or case_id in seen:
            continue
        seen.add(case_id)
        entries.append(entry)
    return entries


def probe_case(case: dict[str, Any], conn: Any, key: str, dry_run: bool,
               repeat: bool = False) -> dict[str, Any] | None:
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
        entry: dict[str, Any] = {
            "answer": answer,
            "hit": hit,
            "matched": matched,
            "error": answer.startswith("[ERROR]"),
            "latency_ms": latency,
        }
        if repeat and not dry_run:
            repeat_answer, repeat_latency = probe_call(
                contexts[variant], question, key, tag=f"probe_{variant}",
            )
            repeat_hit, _ = score_answer(repeat_answer, keywords, match)
            entry["repeat_answer"] = repeat_answer
            entry["repeat_hit"] = repeat_hit
            entry["repeat_latency_ms"] = repeat_latency
        variants[variant] = entry

    return {
        "case_id": case["case_id"],
        "kind": case.get("kind", "case"),
        "category": case.get("category"),
        "probe_scope": case.get("probe_scope") or case.get("category"),
        "probe_tier": case.get("probe_tier"),
        "probe_type": case.get("probe_type"),
        "probe_question": question,
        "probe_answer_keywords": keywords,
        "probe_match": match,
        "summary_len": len(summary),
        "variants": variants,
    }


def _wilson(k: int, n: int, z: float = 1.96) -> list[float]:
    if n == 0:
        return [0.0, 0.0]
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return [round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)]


def _mcnemar_exact(b: int, c: int) -> float:
    """配对检验：b=只有变体对，c=只有 baseline 对。返回精确双侧 p 值。"""
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, k) for k in range(0, min(b, c) + 1)) / (2 ** n)
    return round(min(1.0, 2 * tail), 6)


def _accuracy(entries: list[dict[str, Any]]) -> dict[str, Any]:
    if not entries:
        return {"case_count": 0, "accuracy": {}, "ci95": {}, "delta_pp": {}, "mcnemar": {}}
    n = len(entries)
    accuracy: dict[str, float] = {}
    ci95: dict[str, list[float]] = {}
    for variant in VARIANTS:
        k = sum(1 for e in entries if e["variants"][variant]["hit"])
        accuracy[variant] = round(k / n, 4)
        ci95[variant] = _wilson(k, n)
    base = accuracy["baseline"]
    delta = {
        variant: round((accuracy[variant] - base) * 100, 1)
        for variant in VARIANTS[1:]
    }
    mcnemar: dict[str, Any] = {}
    for variant in VARIANTS[1:]:
        b = sum(
            1 for e in entries
            if not e["variants"]["baseline"]["hit"] and e["variants"][variant]["hit"]
        )
        c = sum(
            1 for e in entries
            if e["variants"]["baseline"]["hit"] and not e["variants"][variant]["hit"]
        )
        mcnemar[variant] = {"b": b, "c": c, "p_exact": _mcnemar_exact(b, c)}
    return {
        "case_count": n,
        "accuracy": accuracy,
        "ci95": ci95,
        "delta_pp": delta,
        "mcnemar": mcnemar,
    }


def _group_by(results: list[dict[str, Any]], field: str) -> dict[str, Any]:
    values = sorted({str(r.get(field)) for r in results if r.get(field)})
    return {
        value: _accuracy([r for r in results if str(r.get(field)) == value])
        for value in values
    }


def _repeat_check(results: list[dict[str, Any]]) -> dict[str, Any]:
    compared = [r for r in results if r["variants"]["baseline"].get("repeat_hit") is not None]
    if not compared:
        return {"case_count": 0}
    per_variant: dict[str, Any] = {}
    for variant in VARIANTS:
        same_answer = sum(
            1 for r in compared
            if r["variants"][variant].get("answer") == r["variants"][variant].get("repeat_answer")
        )
        same_hit = sum(
            1 for r in compared
            if r["variants"][variant]["hit"] == r["variants"][variant].get("repeat_hit")
        )
        per_variant[variant] = {
            "answer_identical": same_answer,
            "hit_identical": same_hit,
            "hit_consistency": round(same_hit / len(compared), 4),
        }
    overall_hit = sum(
        1 for r in compared
        for variant in VARIANTS
        if r["variants"][variant]["hit"] == r["variants"][variant].get("repeat_hit")
    )
    total = len(compared) * len(VARIANTS)
    return {
        "case_count": len(compared),
        "hit_consistency": round(overall_hit / total, 4),
        "min_consistency": REPEAT_MIN_CONSISTENCY,
        "pass": overall_hit / total >= REPEAT_MIN_CONSISTENCY,
        "per_variant": per_variant,
    }


def summarize_probe(results: list[dict[str, Any]],
                    token_cost: dict[str, Any] | None = None) -> dict[str, Any]:
    overall = _accuracy(results)
    raw48_gain = overall["delta_pp"].get("raw48", 0.0)
    accuracy = overall["accuracy"]
    layered_gain = round(
        (accuracy.get("layered", 0.0) - accuracy.get("raw48", 0.0)) * 100, 1,
    ) if accuracy else 0.0
    layered_mcnemar = 1.0
    if results:
        b = sum(
            1 for r in results
            if not r["variants"]["raw48"]["hit"] and r["variants"]["layered"]["hit"]
        )
        c = sum(
            1 for r in results
            if r["variants"]["raw48"]["hit"] and not r["variants"]["layered"]["hit"]
        )
        layered_mcnemar = _mcnemar_exact(b, c)

    token_ratio_delivery = None
    token_ratio_with_summary = None
    if token_cost:
        base_tokens = int(token_cost.get("baseline_prompt_tokens") or 0)
        layered_tokens = int((token_cost.get("variants") or {}).get("layered") or 0)
        summary_tokens = int(token_cost.get("summary_prompt_tokens") or 0)
        if base_tokens:
            token_ratio_delivery = round(layered_tokens / base_tokens - 1, 3)
            token_ratio_with_summary = round(
                (layered_tokens + summary_tokens) / base_tokens - 1, 3,
            )

    return {
        "overall": overall,
        "by_kind": _group_by(results, "kind"),
        "by_scope": _group_by(results, "probe_scope"),
        "by_tier": _group_by(results, "probe_tier"),
        "by_type": _group_by(results, "probe_type"),
        "thresholds": {
            "raw48_vs_baseline_pp": raw48_gain,
            "raw48_min_pp": RAW48_MIN_GAIN_PP,
            "raw48_mcnemar_p": overall["mcnemar"].get("raw48", {}).get("p_exact"),
            "raw48_pass": raw48_gain >= RAW48_MIN_GAIN_PP,
            "layered_vs_raw48_pp": layered_gain,
            "layered_min_pp": LAYERED_MIN_GAIN_PP,
            "layered_mcnemar_p": layered_mcnemar,
            "layered_token_ratio_delivery": token_ratio_delivery,
            "layered_token_ratio_with_summary": token_ratio_with_summary,
            "layered_token_max_ratio": LAYERED_MAX_TOKEN_RATIO,
            "layered_pass": (
                layered_gain >= LAYERED_MIN_GAIN_PP
                and (token_ratio_delivery is None or token_ratio_delivery <= LAYERED_MAX_TOKEN_RATIO)
            ),
        },
    }


def run(cases: list[dict[str, Any]], db_path: str, limit: int | None = None,
        dry_run: bool = False, jsonl_path: str | None = None,
        resume: bool = False, repeat: int = 0) -> dict[str, Any]:
    key = "" if dry_run else _read_token()
    _wire_summary_metrics()
    conn = connect_ro(db_path)
    selected = cases[: limit or len(cases)]
    done_entries: list[dict[str, Any]] = []
    if jsonl_path:
        if resume and Path(jsonl_path).exists():
            done_entries = _load_done_entries(jsonl_path)
        else:
            Path(jsonl_path).write_text("", encoding="utf-8")
    done = {str(entry.get("case_id")) for entry in done_entries}

    pending = [case for case in selected if str(case["case_id"]) not in done]
    repeat_ids = _pick_repeat_ids(pending, repeat)
    results: list[dict[str, Any]] = list(done_entries)
    skipped = 0
    for index, case in enumerate(selected, 1):
        if case["case_id"] in done:
            continue
        entry = probe_case(
            case, conn, key, dry_run, repeat=str(case["case_id"]) in repeat_ids,
        )
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
    token_cost = {
        "baseline_prompt_tokens": usage.get("probe_baseline", {}).get("prompt_tokens", 0),
        "summary_prompt_tokens": _SUMMARY_METRICS["prompt_tokens"],
        "summary_calls": _SUMMARY_METRICS["calls"],
        "variants": {
            variant: usage.get(f"probe_{variant}", {}).get("prompt_tokens", 0)
            for variant in VARIANTS
        },
    }
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "model": MODEL,
        "dry_run": dry_run,
        "db": Path(db_path).name,
        "case_count": len(results),
        "skipped_missing_probe": skipped,
        "results": results,
        "summary": summarize_probe(results, token_cost),
        "repeat_check": _repeat_check(results),
        "usage": usage,
        "token_cost": token_cost,
        "notes": (
            "只测上下文里的事实可用性：每变体一问一答、temperature=0、不生成回复、不调 judge；"
            "system 不含 PERSONA；答案是生成侧结果，判据用关键词命中而不是主观分。"
            + ("resume 时结果含 checkpoint 里的条目，token 只统计本次新跑的 case。" if resume else "")
        ),
    }


def print_summary(summary: dict[str, Any]) -> None:
    overall = summary["overall"]
    print(f"cases={overall['case_count']}")
    if not overall["case_count"]:
        return
    for variant in VARIANTS:
        delta = overall["delta_pp"].get(variant, 0.0)
        lo, hi = overall["ci95"][variant]
        print(f"  {variant:<9} acc={overall['accuracy'][variant]:.3f} "
              f"ci95=[{lo:.3f},{hi:.3f}] delta={delta:+.1f}pp")
    for label, groups in (
        ("kind", summary["by_kind"]),
        ("scope", summary["by_scope"]),
        ("tier", summary["by_tier"]),
        ("type", summary["by_type"]),
    ):
        for value, stats in groups.items():
            if stats["case_count"]:
                print(f"  [{label}:{value}] n={stats['case_count']} "
                      + " ".join(f"{v}={stats['accuracy'][v]:.3f}" for v in VARIANTS))
    thresholds = summary["thresholds"]
    print(f"  raw48 vs baseline: {thresholds['raw48_vs_baseline_pp']:+.1f}pp "
          f"(need ≥{thresholds['raw48_min_pp']:.0f}, McNemar p={thresholds['raw48_mcnemar_p']}) "
          f"-> {'PASS' if thresholds['raw48_pass'] else 'FAIL'}")
    print(f"  layered vs raw48 : {thresholds['layered_vs_raw48_pp']:+.1f}pp "
          f"(need ≥{thresholds['layered_min_pp']:.0f}, p={thresholds['layered_mcnemar_p']}) "
          f"token_delivery={thresholds['layered_token_ratio_delivery']} "
          f"-> {'PASS' if thresholds['layered_pass'] else 'FAIL'}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--cases", default="eval/memory_cases.json")
    parser.add_argument("--out", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--jsonl", default="", help="逐条 checkpoint 路径；默认 <out>.jsonl")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--repeat", type=int, default=0,
                        help="按 tier 分层抽 N 条，每变体再问一次，做一致性自检")
    args = parser.parse_args(argv)

    raw = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    cases = [*raw.get("cases", []), *raw.get("needles", [])]
    out = Path(args.out) if args.out else (
        Path("eval/out") / f"memory_probe_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    jsonl = args.jsonl or (str(out) + ".jsonl")
    payload = run(cases, args.db, limit=args.limit or None, dry_run=args.dry_run,
                  jsonl_path=jsonl, resume=args.resume, repeat=args.repeat)
    if args.resume and not payload["results"]:
        if payload["skipped_missing_probe"]:
            print(f"没有可跑的 case：{payload['skipped_missing_probe']} 条缺 probe 字段；不覆盖 out 文件")
        else:
            print("checkpoint 里所有 case 都已完成，本次没有新结果；不覆盖 out 文件")
        return 0
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print_summary(payload["summary"])
    print(f"repeat_check: {payload['repeat_check']}")
    print(f"-> {out}")
    print(f"checkpoint -> {jsonl} (resume with --resume)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
