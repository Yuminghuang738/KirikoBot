#!/usr/bin/env python3
"""Bootstrap affection scores from real @bot interactions only.

Uses history table (role=user entries = actual bot conversations) for scoring.
Keyword sentiment is extracted from the user's message content in history.
Tool usage and learning feedback provide supplementary signals.

Scoring values match the reduced-scale constants in affection_service.py.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from typing import Any

DB_PATH = "/home/bosak/Documents/ClaudeCode_Projects/KirikoBot/KirikoBot/robot.db"

# ── Keyword patterns (reduced scale, matching affection_service.py) ──
POSITIVE_PATTERNS: list[tuple[str, float]] = [
    ("最喜欢", 0.8), ("爱了", 0.8), ("好喜欢你", 0.8), ("真棒", 0.7),
    ("厉害", 0.6), ("好强", 0.6), ("太强了", 0.6),
    ("谢谢", 0.4), ("感谢", 0.4), ("多谢", 0.4),
    ("可爱", 0.6), ("好萌", 0.6), ("贴心", 0.6),
    ("好用", 0.4), ("不错", 0.3),
    ("好有趣", 0.5), ("好棒", 0.5), ("太好了", 0.5), ("完美", 0.5),
    ("好评", 0.4), ("真香", 0.5),
    ("哈哈", 0.2), ("笑死", 0.3),
    ("方便", 0.2), ("牛", 0.4),
]

NEGATIVE_PATTERNS: list[tuple[str, float]] = [
    ("垃圾", -0.6), ("废物", -0.8), ("没用", -0.6), ("真没用", -0.8),
    ("滚", -0.8), ("闭嘴", -0.6), ("别说了", -0.4), ("烦死了", -0.6),
    ("笨", -0.4), ("蠢", -0.5), ("傻逼", -0.8), ("SB", -0.8),
    ("不好用", -0.5), ("什么鬼", -0.3), ("乱说", -0.5),
    ("无语", -0.3), ("失望", -0.5), ("差评", -0.5),
    ("别@我", -0.5), ("别叫我", -0.4),
]

SCORE_MIN = 0.0
SCORE_MAX = 100.0
SCORE_DEFAULT = 50.0

RELATIONSHIP_LEVELS = [
    (80, "挚友", "🌟"),
    (60, "亲密", "💕"),
    (40, "友好", "😊"),
    (20, "普通", "👋"),
    (0,  "冷淡", "❄️"),
]


def get_relationship(score: float) -> tuple[str, str]:
    for threshold, label, emoji in RELATIONSHIP_LEVELS:
        if score >= threshold:
            return label, emoji
    return "冷淡", "❄️"


def score_text(text: str) -> float:
    """Score a single message for keyword sentiment."""
    text_lower = text.lower()
    best_pos = 0.0
    best_neg = 0.0
    for pattern, value in POSITIVE_PATTERNS:
        if pattern.lower() in text_lower and value > best_pos:
            best_pos = value
    for pattern, value in NEGATIVE_PATTERNS:
        if pattern.lower() in text_lower and abs(value) > abs(best_neg):
            best_neg = value
    return best_pos + best_neg


def bootstrap_user(
    db: sqlite3.Connection,
    user_id: str,
    group_id: str,
    user_name: str,
) -> dict[str, Any] | None:
    """Compute initial affection from real @bot interactions in history table."""

    # ── 1. Real @bot interactions from history ──────────────
    # History records user messages formatted as: 群「群名」中 用户 XXX 说：内容
    # We extract the content part after "说：" for keyword analysis
    rows = db.execute(
        "SELECT content, timestamp FROM history "
        "WHERE user_id = ? AND group_id = ? AND role = 'user' ORDER BY id",
        (user_id, group_id),
    ).fetchall()

    interaction_count = len(rows)
    if interaction_count == 0:
        return None  # never actually talked to the bot

    # Get last interaction time
    last_ts = rows[-1][1] if rows else None

    # Extract actual user message text from history content format
    # Format: "群「群名」中 用户 XXX 说：消息内容"
    total_sentiment = 0.0
    pos_hits = 0
    neg_hits = 0
    for content, _ in rows:
        if not content:
            continue
        # Extract the message part after "说："
        if "说：" in content:
            msg = content.split("说：", 1)[-1].strip()
        else:
            msg = content.strip()
        if not msg:
            continue
        s = score_text(msg)
        if s > 0:
            pos_hits += 1
            total_sentiment += s
        elif s < 0:
            neg_hits += 1
            total_sentiment += s

    # Cap sentiment contribution (same scale as reduced keywords)
    sentiment_bonus = max(-5.0, min(10.0, total_sentiment))

    # ── 2. Tool usage (only bot-invoked tools) ──────────────
    tool_count = db.execute(
        "SELECT COUNT(*) FROM tool_usage WHERE user_id = ? AND group_id = ?",
        (user_id, group_id),
    ).fetchone()[0]
    tool_bonus = min(tool_count * 0.05, 5.0)

    # ── 3. Learning feedback ──────────────────────────────
    learning_rows = db.execute(
        "SELECT note FROM learning_log WHERE user_id = ?", (user_id,)
    ).fetchall()
    learning_bonus = 0.0
    for (note,) in learning_rows:
        if not note:
            continue
        if "表现良好" in note:
            learning_bonus += 0.5
        elif "回复不当" in note or "工具选择错误" in note:
            learning_bonus -= 0.3
        elif "遗漏工具" in note:
            learning_bonus -= 0.1
    learning_bonus = max(-3.0, min(5.0, learning_bonus))

    # ── 4. Interaction frequency bonus ────────────────────
    # Based on real @bot interactions, not all group messages
    # 100+ @bot interactions = full activity bonus
    activity_bonus = min(interaction_count / 10.0, 15.0)

    # ── 5. Compute final score ────────────────────────────
    score = SCORE_DEFAULT + activity_bonus + sentiment_bonus + tool_bonus + learning_bonus
    score = max(SCORE_MIN, min(SCORE_MAX, score))
    relationship, emoji = get_relationship(score)

    return {
        "user_id": user_id,
        "group_id": group_id,
        "user_name": user_name,
        "affection_score": round(score, 1),
        "interaction_count": interaction_count,
        "positive_count": pos_hits,
        "negative_count": neg_hits,
        "last_interaction": last_ts,
        "relationship": relationship,
        "emoji": emoji,
        "activity_bonus": round(activity_bonus, 1),
        "sentiment_bonus": round(sentiment_bonus, 1),
        "tool_bonus": round(tool_bonus, 1),
        "learning_bonus": round(learning_bonus, 1),
    }


def main() -> None:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row

    # Get users who have actually interacted with the bot (from history table)
    # Exclude bot accounts (ROBOT_QQ + known bots like QQ 小冰)
    from dotenv import load_dotenv
    import os
    load_dotenv("/home/bosak/Documents/ClaudeCode_Projects/KirikoBot/KirikoBot/.env")
    bot_qq = os.getenv("ROBOT_QQ", "")
    extra_bot_qq = os.getenv("EXTRA_BOT_QQ", "2854196306")  # QQ 小冰
    bot_qqs = {q for q in [bot_qq, extra_bot_qq] if q}
    exclude_placeholders = ",".join("?" for _ in bot_qqs)

    users = db.execute(
        f"SELECT DISTINCT h.user_id, h.group_id, "
        "COALESCE("
        "(SELECT user_name FROM group_messages WHERE user_id=h.user_id AND group_id=h.group_id ORDER BY id DESC LIMIT 1),"
        "h.user_id"
        ") as user_name "
        "FROM history h "
        f"WHERE h.role = 'user' AND h.user_id NOT IN ({exclude_placeholders}) AND h.group_id IS NOT NULL",
        tuple(bot_qqs),
    ).fetchall()

    # Deduplicate by (user_id, group_id)
    all_users: dict[tuple[str, str], tuple[str, str, str]] = {}
    for row in users:
        key = (row["user_id"], row["group_id"])
        if key not in all_users:
            all_users[key] = (row["user_id"], row["group_id"], row["user_name"])

    print(f"Found {len(all_users)} unique users across all groups\n")

    results = []
    for (uid, gid), (_, _, uname) in all_users.items():
        result = bootstrap_user(db, uid, gid, uname)
        if result is None:
            continue
        results.append(result)

    # Sort by score descending
    results.sort(key=lambda r: r["affection_score"], reverse=True)

    # ── Display ────────────────────────────────────────────
    print(f"{'用户':<16} {'分数':>6} {'关系':<6} {'消息':>5} {'活跃':>6} {'情感':>6} {'工具':>5} {'学习':>5}")
    print("-" * 72)
    for r in results:
        print(
            f"{r['user_name'][:14]:<16} "
            f"{r['affection_score']:>5.0f} "
            f"{r['emoji']}{r['relationship']:<4} "
            f"{r['interaction_count']:>5} "
            f"{r['activity_bonus']:>+5.0f} "
            f"{r['sentiment_bonus']:>+5.0f} "
            f"{r['tool_bonus']:>+4.0f} "
            f"{r['learning_bonus']:>+4.0f}"
        )

    print()
    print(f"Score distribution: ", end="")
    ranges = [("挚友", 80), ("亲密", 60), ("友好", 40), ("普通", 20)]
    for label, threshold in ranges:
        count = sum(1 for r in results if r["affection_score"] >= threshold)
        print(f"{label}: {count}  ", end="")
    print()

    # ── Write to database ──────────────────────────────────
    print("\n写入 user_affection 表...")
    inserted = 0
    updated = 0
    for r in results:
        # Check if record exists
        existing = db.execute(
            "SELECT id FROM user_affection WHERE user_id = ? AND group_id = ?",
            (r["user_id"], r["group_id"]),
        ).fetchone()

        if existing:
            db.execute(
                "UPDATE user_affection SET "
                "affection_score = ?, interaction_count = ?, positive_count = ?, "
                "negative_count = ?, last_interaction = ?, relationship = ?, "
                "user_name = ?, updated_at = datetime('now','localtime') "
                "WHERE user_id = ? AND group_id = ?",
                (
                    r["affection_score"], r["interaction_count"],
                    r["positive_count"], r["negative_count"],
                    r["last_interaction"], r["relationship"],
                    r["user_name"], r["user_id"], r["group_id"],
                ),
            )
            updated += 1
        else:
            db.execute(
                "INSERT INTO user_affection "
                "(user_id, group_id, user_name, affection_score, interaction_count, "
                "positive_count, negative_count, last_interaction, relationship) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    r["user_id"], r["group_id"], r["user_name"],
                    r["affection_score"], r["interaction_count"],
                    r["positive_count"], r["negative_count"],
                    r["last_interaction"], r["relationship"],
                ),
            )
            inserted += 1

    db.commit()
    print(f"  ✅ 新增 {inserted} 条，更新 {updated} 条")
    print(f"  📊 总计 {len(results)} 位用户的好感度已初始化")

    db.close()


if __name__ == "__main__":
    main()
