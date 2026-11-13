"""从只读 DB 副本抽取分层记忆 eval 样本（3b）。

仓库里只存样本索引（case_id / user_id / group_id / target_history_id /
anchor_history_id / category / anchor_fact），**不存聊天原文**；运行时再从
DB 副本按 id 取。anchor_fact 留空，由 Hina 人工标注。

用法：
    python3 eval/memory_cases.py --db <副本> --n 20 --needles 5 \
        --out eval/memory_cases.json
"""
from __future__ import annotations

import argparse
import json
import random
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

RAW_WINDOW = 24          # 目标回合前至少要有这么多条消息
NEEDLE_GAP = (30, 45)    # needle 的锚点距离目标 30–45 条


def connect_ro(db_path: str) -> sqlite3.Connection:
    """只读打开 DB 副本；绝不写生产库。"""
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def load_scope_rows(conn: sqlite3.Connection) -> dict[tuple[str, str | None], list[dict[str, Any]]]:
    """按 (user_id, group_id) 分组，返回按 id 正序的 user/assistant 行。"""
    scopes: dict[tuple[str, str | None], list[dict[str, Any]]] = {}
    rows = conn.execute(
        "SELECT id, user_id, group_id, role FROM history "
        "WHERE role IN ('user', 'assistant') ORDER BY user_id, id"
    )
    for rid, uid, gid, role in rows:
        key = (str(uid), None if gid is None else str(gid))
        scopes.setdefault(key, []).append({"id": int(rid), "role": role})
    return scopes


def _anchor_before(rows: list[dict[str, Any]], idx: int,
                   min_back: int, max_back: int | None = None) -> int | None:
    """目标 assistant 行之前、至少 min_back 条（至多 max_back 条）的最近 user 行。"""
    low = idx - max_back if max_back else 0
    for j in range(idx - min_back, max(low, 0) - 1, -1):
        if 0 <= j < len(rows) and rows[j]["role"] == "user":
            return rows[j]["id"]
    return None


def pick_cases(scopes: dict[tuple[str, str | None], list[dict[str, Any]]],
               n: int = 20, needles: int = 5, seed: int = 7) -> dict[str, list[dict[str, Any]]]:
    """按 10 群 + 10 私聊配额抽样本；数据不够时宁缺毋滥。"""
    rng = random.Random(seed)
    group_target = n // 2
    private_target = n - group_target

    groups: list[dict[str, Any]] = []
    privates: list[dict[str, Any]] = []
    needle_cases: list[dict[str, Any]] = []

    scope_items = list(scopes.items())
    rng.shuffle(scope_items)
    for (uid, gid), rows in scope_items:
        if len(rows) < RAW_WINDOW + 2:
            continue
        bucket = groups if gid is not None else privates
        quota = group_target if gid is not None else private_target
        if len(bucket) >= quota:
            continue

        # 普通样本：目标 assistant 至少在 24 条之后，锚点在 24 条之前。
        candidates = [
            i for i, row in enumerate(rows)
            if row["role"] == "assistant" and i >= RAW_WINDOW
            and _anchor_before(rows, i, RAW_WINDOW) is not None
        ]
        rng.shuffle(candidates)
        for idx in candidates:
            if len(bucket) >= quota:
                break
            anchor = _anchor_before(rows, idx, RAW_WINDOW)
            bucket.append({
                "case_id": f"{'G' if gid is not None else 'P'}{len(bucket) + 1:03d}",
                "user_id": uid,
                "group_id": gid,
                "target_history_id": rows[idx]["id"],
                "anchor_history_id": anchor,
                "anchor_fact": "",
                "category": "group" if gid is not None else "private",
                "kind": "case",
            })

    # needle：锚点在 30–45 条之前，baseline 的 16 条窗口必然看不到。
    for (uid, gid), rows in scope_items:
        if len(needle_cases) >= needles:
            break
        for idx, row in enumerate(rows):
            if len(needle_cases) >= needles:
                break
            if row["role"] != "assistant" or idx < NEEDLE_GAP[1]:
                continue
            anchor = _anchor_before(rows, idx, NEEDLE_GAP[0], NEEDLE_GAP[1])
            if anchor is None:
                continue
            needle_cases.append({
                "case_id": f"N{len(needle_cases) + 1:03d}",
                "user_id": uid,
                "group_id": gid,
                "target_history_id": row["id"],
                "anchor_history_id": anchor,
                "anchor_fact": "",
                "category": "needle",
                "kind": "needle",
            })

    return {"cases": groups + privates, "needles": needle_cases}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="robot.db 的只读副本")
    parser.add_argument("--n", type=int, default=20)
    parser.add_argument("--needles", type=int, default=5)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--out", default="eval/memory_cases.json")
    args = parser.parse_args(argv)

    conn = connect_ro(args.db)
    scopes = load_scope_rows(conn)
    picked = pick_cases(scopes, n=args.n, needles=args.needles, seed=args.seed)
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "db": str(Path(args.db).name),
        "cases": picked["cases"],
        "needles": picked["needles"],
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    groups = sum(1 for c in picked["cases"] if c["group_id"] is not None)
    privates = len(picked["cases"]) - groups
    print(f"cases={len(picked['cases'])} (group={groups} private={privates}) "
          f"needles={len(picked['needles'])} -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
