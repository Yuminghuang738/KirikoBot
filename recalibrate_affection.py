#!/usr/bin/env python3
"""把好感度按新的事件制重算一遍。

## 为什么需要

旧模型是积分制：每条消息 +0.1、每次工具调用 +0.05（**无上限**）、判官按条
±2.0（**也无总量上限**）。生产库实测出来的分布很说明问题：

    挚友(≥80)   6 人   均值 98.3   ← 3 个顶在 100
    亲密(≥60)   6 人   均值 66.7
    友好(≥40)  58 人   均值 51.7   ← 全是初始值
    普通/冷淡    0 人              ← 数学上到不了（掉分封底卡在 50）

新规则改成事件制（见 affection_service.py 顶部注释）。这个脚本把历史上按旧
规则刷出来的分数折算回合理区间。

**换算不看旧分数** —— 旧分数本身就是错的，拿它做基准等于把刷出来的结果请回来。
只看两个客观事实：来过多少天、连续多久。

## 用法

    python3 recalibrate_affection.py            # 只看，不改（默认）
    python3 recalibrate_affection.py --apply    # 真的写进去

顺带会把 v1 结构的画像统一成 v2（纯字段映射，不重跑模型）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(_PROJECT_DIR, "KirikoBot")
DB_PATH = os.getenv("KIRIKO_DB") or os.path.join(APP_DIR, "robot.db")

sys.path.insert(0, APP_DIR)

from affection_service import (           # noqa: E402
    AffectionService,
    recalibrated_score,
)
from profile_service import normalize_profile   # noqa: E402


def _days_seen(conn, user_id: str, group_id: str) -> int:
    """这个人在这个群出现过多少**天**（不是多少条）。

    用 group_messages 的日期去重，比 affection_log 准 —— 后者只在
    delta != 0 时才写，按天封顶后就漏了。
    """
    row = conn.execute(
        "SELECT COUNT(DISTINCT date(timestamp)) FROM group_messages "
        "WHERE user_id=? AND group_id=?",
        (user_id, group_id),
    ).fetchone()
    return int(row[0] or 0) if row else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="按事件制重算好感度")
    parser.add_argument("--apply", action="store_true", help="真的写进数据库")
    parser.add_argument("--db", default=DB_PATH, help=f"数据库路径（默认 {DB_PATH}）")
    args = parser.parse_args()

    if not os.path.isfile(args.db):
        print(f"数据库不存在：{args.db}", file=sys.stderr)
        return 2

    import sqlite3

    # 先把 schema 拉齐（DatabaseManager 会跑 ALTER TABLE 补列；幂等，可重复执行）
    from database_manager import DatabaseManager
    DatabaseManager(args.db)

    conn = sqlite3.connect(args.db)
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(user_affection)")]
        streak_col = "streak_days" if "streak_days" in cols else "0 AS streak_days"
        rows = conn.execute(
            f"SELECT user_id, group_id, user_name, affection_score, {streak_col} "
            "FROM user_affection ORDER BY affection_score DESC"
        ).fetchall()

        print(f"{'用户':<14}{'群':<12}{'旧分':>6}{'来过':>6}{'连续':>6}{'新分':>7}  {'档位'}")
        print("-" * 60)
        changed = 0
        updates = []
        for user_id, group_id, user_name, old_score, streak in rows:
            days = _days_seen(conn, user_id, group_id)
            new_score = round(recalibrated_score(days, streak), 1)
            label, _emoji = AffectionService.get_relationship(new_score)
            flag = "  ← 变" if abs(new_score - float(old_score)) >= 0.1 else ""
            print(f"{(user_name or '')[:12]:<14}{str(group_id)[:10]:<12}"
                  f"{float(old_score):>6.1f}{days:>6}{streak or 0:>6}{new_score:>7.1f}  {label}{flag}")
            if abs(new_score - float(old_score)) >= 0.1:
                changed += 1
                updates.append((new_score, label, user_id, group_id))

        print("-" * 60)
        print(f"共 {len(rows)} 条记录，其中 {changed} 条会变")

        if not args.apply:
            print("\n这是**预演**，什么都没改。确认无误后加 --apply 真的写进去。")
            return 0

        for new_score, label, user_id, group_id in updates:
            conn.execute(
                "UPDATE user_affection SET affection_score=?, relationship=?, "
                "updated_at=datetime('now','localtime') WHERE user_id=? AND group_id=?",
                (new_score, label, user_id, group_id),
            )
        conn.commit()
        print(f"\n✓ 已写入 {len(updates)} 条")

        # ── 顺带把画像统一成 v2（纯字段映射，不重跑模型）──
        prows = conn.execute("SELECT user_id, profile_json FROM user_profiles").fetchall()
        migrated = 0
        for user_id, pj in prows:
            try:
                raw = json.loads(pj or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(raw.get("stable"), dict):
                continue                       # 已经是 v2
            normalised = normalize_profile(raw)
            conn.execute(
                "UPDATE user_profiles SET profile_json=? WHERE user_id=?",
                (json.dumps(normalised, ensure_ascii=False), user_id),
            )
            migrated += 1
        conn.commit()
        print(f"✓ 画像统一成 v2：{migrated} 条")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
