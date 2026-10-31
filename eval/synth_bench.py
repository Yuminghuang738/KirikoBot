#!/usr/bin/env python3
"""联网搜索的综合回答：思考模式开/关对延迟与回答质量的影响。

搜索本身只要 ~0.5 秒（见 search_bench.py），用户感觉到的慢主要来自综合回答那次 LLM 调用：
运行时它开着思考模式（thinking=enabled），输入又带着几千字的资料。这里单独测它。

    python3 eval/synth_bench.py
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "eval"))

import requests  # noqa: E402

from search_bench import CASES, VARIANTS, _key, search, context_for_model, API  # noqa: E402

SYSTEM_TMPL = (
    "你是Kiriko，请根据以下搜索结果回答用户问题。"
    f"当前日期是{datetime.now().strftime('%Y年%m月%d日')}，你的知识截止于2025年，请以当前日期和搜索结果为准。"
    "用可爱的语气，简洁明了，不要长篇大论，控制在300字以内。"
    "禁止使用表格，用自然的段落文字回复。"
    "\n\n用户问题：{q}\n\n搜索结果：\n{ctx}"
)

JUDGE_SYSTEM = (
    "你是回答质量裁判。给你用户问题、搜索资料、和一个回答。只输出 JSON："
    "{\"score\": 1-5}。5=回答准确、切题、基于资料、简洁；3=基本切题但有空话或轻微偏差；"
    "1=答非所问或编造资料里没有的内容。"
)


def ask(ori: str, ctx: str, thinking: bool, key: str) -> tuple[str, float, int]:
    payload = {
        "model": "deepseek-flash",
        "messages": [{"role": "system", "content": SYSTEM_TMPL.format(q=ori, ctx=ctx)},
                     {"role": "user", "content": f"请根据以上搜索结果回答：{ori}"}],
        "max_tokens": 6000, "stream": False,
        "thinking": {"type": "enabled" if thinking else "disabled"},
    }
    if thinking:
        payload["reasoning_effort"] = "low"
    t0 = time.perf_counter()
    r = requests.post(f"{API}/chat/completions", json=payload, timeout=120,
                      headers={"Authorization": f"Bearer {key}"})
    dt = time.perf_counter() - t0
    if r.status_code != 200:
        return "", dt, r.status_code
    return (r.json()["choices"][0]["message"].get("content") or "").strip(), dt, 200


def judge(ori: str, ctx: str, answer: str, key: str) -> int:
    payload = {"model": "deepseek-flash", "temperature": 0, "max_tokens": 60, "stream": False,
               "thinking": {"type": "disabled"},
               "messages": [{"role": "system", "content": JUDGE_SYSTEM},
                            {"role": "user", "content": f"【用户问题】{ori}\n\n【搜索结果】\n{ctx}\n\n【回答】\n{answer}"}]}
    try:
        r = requests.post(f"{API}/chat/completions", json=payload, timeout=60,
                          headers={"Authorization": f"Bearer {key}"})
        import re
        m = re.search(r"\{.*\}", r.json()["choices"][0]["message"]["content"], re.S)
        return int(json.loads(m.group(0)).get("score", 0)) if m else 0
    except Exception:  # noqa: BLE001
        return 0


def main() -> int:
    key = _key()
    v = VARIANTS["C"]   # 资料用 summary 版（与运行时的小体积方案对齐）
    rows = []
    for query, ori in CASES:
        results, _, st = search(query, ori, v, key)
        if st != 200 or not results:
            continue
        ctx = context_for_model(results, v)
        for think in (True, False):
            ans, dt, st2 = ask(ori, ctx, think, key)
            if st2 != 200 or not ans:
                continue
            rows.append({"q": ori, "think": think, "lat": dt, "score": judge(ori, ctx, ans, key), "len": len(ans)})
            time.sleep(0.2)
    for label, flag in (("思考开", True), ("思考关", False)):
        sub = [r for r in rows if r["think"] == flag]
        if not sub:
            continue
        lat = [r["lat"] for r in sub]
        print(f"{label}：n={len(sub)}  延迟 中位 {statistics.median(lat):.2f}s / P90 "
              f"{sorted(lat)[int(len(lat)*0.9)]:.2f}s  回答质量 均分 {statistics.mean(r['score'] for r in sub):.2f}"
              f"（≥4 占 {sum(r['score'] >= 4 for r in sub)/len(sub):.2f}）  回答中位 {statistics.median(r['len'] for r in sub):.0f} 字")
    out = ROOT / "eval" / "out"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"synth_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
