#!/usr/bin/env python3
"""联网搜索基准：延迟 + 结果可用性（裁判打分）。

为什么需要：搜索「慢」「不准」都是感觉，要分清是哪一段慢、哪一步不准。
对同一组查询跑几种配置，分别记录：
  · 延迟：单次搜索请求的耗时（串行跑，避免并发干扰）
  · 资料体积：交给模型的资料有多少字
  · 可用性：LLM 裁判看「交给模型的资料」能否回答用户原问题（1–5 分）
  · 过时：资料里是否出现明显早于当前年份的年份

用法（仓库根目录）：
    python3 eval/search_bench.py --variants A,B,C,D
    python3 eval/search_bench.py --list

API Key 从 KirikoBot/.env 读取（与运行时一致）。约 20 条查询 × 4 个变体 ≈ 0.4 元。
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parent.parent
API = "https://api.deepseek.com"

# 基准查询：(查询词 query, 用户原问题 ori_question)。查询词模拟模型改写后的样子。
CASES: list[tuple[str, str]] = [
    ("2026年最近发售的游戏", "最近有什么新游戏"),
    ("DeepSeek API 价格 2026", "deepseek 现在多少钱一百万token"),
    ("吉他 效果器 新品 2026", "最近有啥新出的效果器"),
    ("Linux 内核 最新版本", "linux 最新内核出了吗"),
    ("Python 3.14 新特性", "python 3.14 有啥新东西"),
    ("原神 最新版本 更新内容", "原神最新版本更新了啥"),
    ("Fedora 最新版本 发布", "fedora 出新版本了吗"),
    ("Docker 最新版本 2026", "docker 现在最新是哪个版本"),
    ("Steam 秋季特卖 2026", "steam 什么时候打折"),
    ("哔哩哔哩 热门视频 今天", "B站今天什么热门"),
    ("iPhone 18 发布会 时间", "iphone18 什么时候发布"),
    ("国产光刻机 最新进展 2026", "国产光刻机最近咋样了"),
    ("鸣潮 新角色 2026", "鸣潮最近有新角色吗"),
    ("Hollow Knight Silksong 评价", "丝之歌好玩吗"),
    ("ChatGPT 最新模型 发布", "chatgpt 最新出了啥"),
    ("王者荣耀 新赛季 2026", "王者荣耀新赛季什么时候"),
    ("NVIDIA 最新显卡 2026", "英伟达最新的显卡是啥"),
    ("GitHub 访问 速度 2026", "github 现在能直接访问吗"),
    ("Claude 最新模型 发布 2026", "claude 最新版本是什么"),
    ("音乐生成 模型 发布", "有没有能生成音乐的ai"),
]


@dataclass
class Variant:
    name: str
    count: int
    need_content: bool
    use_ori: bool       # 是否传 ori_question
    rerank: bool        # 是否按 rerank_score 过滤排序
    after: bool = False  # 是否在查询后加 after:去年 时效过滤
    note: str = ""


VARIANTS = {
    "A": Variant("A", 10, True, False, False, "现状：count=10 + 正文"),
    "B": Variant("B", 5, True, False, False, "count=5 + 正文"),
    "C": Variant("C", 5, False, False, False, "count=5，不取正文，用 summary"),
    "D": Variant("D", 8, False, True, True, "count=8 + ori_question + rerank 过滤排序 + summary"),
    "E": Variant("E", 5, False, False, False, after=True, note="C + after:去年 时效过滤（压低旧资料）"),
}


def _key() -> str:
    env = ROOT / "KirikoBot" / ".env"
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("DEEPSEEK_TOKEN"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("找不到 DEEPSEEK_TOKEN")


def search(query: str, ori: str, v: Variant, key: str) -> tuple[list[dict], float, int]:
    if v.after:
        query = f"{query} after:{datetime.now().year - 1}"
    body: dict[str, Any] = {"query": query, "count": v.count, "need_content": v.need_content}
    if v.use_ori:
        body["ori_question"] = ori
    t0 = time.perf_counter()
    r = requests.post(f"{API}/v1/tools/search", json=body, timeout=60,
                      headers={"Authorization": f"Bearer {key}"})
    dt = time.perf_counter() - t0
    if r.status_code != 200:
        return [], dt, r.status_code
    return r.json().get("results") or [], dt, 200


def context_for_model(results: list[dict], v: Variant) -> str:
    """复刻运行时 WebSearch.search_and_fetch 的拼装，换成本变体的规则。"""
    items = results
    if v.rerank:
        items = sorted(results, key=lambda x: x.get("rerank_score") or 0, reverse=True)
        good = [x for x in items if (x.get("rerank_score") or 0) >= 1.5]
        items = good or items[:3]
    chunks = []
    for r in items[:5]:
        title = (r.get("title") or "").strip() or "(无标题)"
        if v.need_content:
            body = (r.get("content") or "").strip() or (r.get("summary") or "").strip()
        else:
            body = (r.get("summary") or "").strip() or (r.get("snippet") or "").strip()
        body = body[:2000]
        if body:
            chunks.append(f"【来源：{title}】\n{body}")
    return "\n\n---\n\n".join(chunks)


JUDGE_SYSTEM = (
    "你是搜索结果质量裁判。给你一个用户原问题和一段交给模型的搜索资料。"
    "只输出 JSON：{\"score\": 1-5, \"stale\": true|false}。"
    "score：资料能否回答这个问题（5=直接能答且是最新的，3=能答但有无关内容，1=答不了）。"
    "stale：资料明显是过时的（讲的是几年前的事却当成现在）则为 true。"
)


def judge(ori: str, context: str, key: str) -> dict:
    payload = {"model": "deepseek-flash", "temperature": 0, "max_tokens": 80, "stream": False,
               "thinking": {"type": "disabled"},
               "messages": [{"role": "system", "content": JUDGE_SYSTEM},
                            {"role": "user", "content": f"【用户原问题】{ori}\n\n【资料】\n{context or '（空）'}"}]}
    try:
        r = requests.post(f"{API}/chat/completions", json=payload, timeout=60,
                          headers={"Authorization": f"Bearer {key}"})
        raw = r.json()["choices"][0]["message"]["content"]
        m = re.search(r"\{.*\}", raw, re.S)
        return json.loads(m.group(0)) if m else {"score": 0, "stale": False}
    except Exception:  # noqa: BLE001
        return {"score": 0, "stale": False}


def old_years(text: str) -> bool:
    """资料里是否出现明显早于去年的年份（过时的粗略信号）。"""
    now = datetime.now().year
    return any(int(y) < now - 1 for y in re.findall(r"20[12]\d", text))


def run(names: list[str]) -> int:
    key = _key()
    out: dict[str, Any] = {}
    for name in names:
        v = VARIANTS[name]
        lat, sizes, scores, stales, empties = [], [], [], [], 0
        for query, ori in CASES:
            results, dt, status = search(query, ori, v, key)
            lat.append(dt)
            if status != 200:
                print(f"  [{name}] {query} → HTTP {status}")
                continue
            if not results:
                empties += 1
            ctx = context_for_model(results, v)
            sizes.append(len(ctx))
            j = judge(ori, ctx, key)
            scores.append(int(j.get("score", 0) or 0))
            stales.append(bool(j.get("stale")) or old_years(ctx))
            time.sleep(0.2)
        out[name] = {
            "note": v.note,
            "latency_median": round(statistics.median(lat), 2),
            "latency_p90": round(sorted(lat)[int(len(lat) * 0.9)], 2),
            "context_chars_median": int(statistics.median(sizes)) if sizes else 0,
            "score_mean": round(statistics.mean(scores), 2) if scores else 0,
            "good_rate": round(sum(s >= 4 for s in scores) / len(scores), 2) if scores else 0,
            "stale_rate": round(sum(stales) / len(stales), 2) if stales else 0,
            "empty": empties,
        }
        o = out[name]
        print(f"[{name}] {v.note}\n"
              f"   延迟 中位 {o['latency_median']}s / P90 {o['latency_p90']}s · 资料中位 {o['context_chars_median']} 字\n"
              f"   可用分 {o['score_mean']}（≥4 占 {o['good_rate']}）· 过时率 {o['stale_rate']} · 空结果 {empties}")

    outdir = ROOT / "eval" / "out"
    outdir.mkdir(parents=True, exist_ok=True)
    path = outdir / f"search_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果：{path.relative_to(ROOT)}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--variants", default="A,B,C,D")
    p.add_argument("--list", action="store_true")
    a = p.parse_args()
    if a.list:
        for v in VARIANTS.values():
            print(f"{v.name}  {v.note}")
        return 0
    names = [x.strip() for x in a.variants.split(",") if x.strip()]
    for n in names:
        if n not in VARIANTS:
            print(f"未知变体 {n}", file=sys.stderr)
            return 2
    return run(names)


if __name__ == "__main__":
    raise SystemExit(main())
