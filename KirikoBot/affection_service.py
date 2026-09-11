from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

# ── Relationship thresholds ─────────────────────────────
RELATIONSHIP_LEVELS = [
    (80, "挚友", "🌟"),
    (60, "亲密", "💕"),
    (40, "友好", "😊"),
    (20, "普通", "👋"),
    (0,  "冷淡", "❄️"),
]

# ── Score bounds ─────────────────────────────────────────
SCORE_MIN = 0.0
SCORE_MAX = 100.0
SCORE_DEFAULT = 50.0

# ── Decay ────────────────────────────────────────────────
DECAY_PER_WEEK = 0.5       # lose 0.5 point per 7 days of inactivity
DECAY_FLOOR = 50.0         # never decay below 50 (neutral)

# ── Daily caps ───────────────────────────────────────────
MAX_INTERACTION_BONUS_PER_DAY = 5   # max from base interaction points
INTERACTION_POINT = 0.1

# ── Learning feedback score mapping ──────────────────────
LEARNING_SCORE_MAP: dict[str, float] = {
    "表现良好": 0.5,
    "工具选择错误": -0.2,
    "回复不当": -0.3,
    "遗漏工具": -0.1,
}


class AffectionService:
    """Manages user affection scores based on interaction patterns.

    This service performs no API calls and no keyword matching: base
    interaction points are deterministic, while sentiment and learning
    deltas arrive from judge_service (AI-judged) asynchronously.
    """

    # ── Relationship level ────────────────────────────────

    @staticmethod
    def get_relationship(score: float) -> tuple[str, str]:
        """Return (label, emoji) for a given score."""
        for threshold, label, emoji in RELATIONSHIP_LEVELS:
            if score >= threshold:
                return label, emoji
        return "冷淡", "❄️"

    # ── Decay computation (lazy, applied on read) ────────

    @staticmethod
    def apply_decay(
        score: float, last_interaction_str: str | None,
    ) -> float:
        """Reduce score toward DECAY_FLOOR based on inactivity duration."""
        if not last_interaction_str or score <= DECAY_FLOOR:
            return score
        try:
            last = datetime.strptime(last_interaction_str, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return score
        days_since = (datetime.now() - last).days
        if days_since < 7:
            return score
        weeks = days_since // 7
        decay = weeks * DECAY_PER_WEEK
        return max(DECAY_FLOOR, score - decay)

    # ── CRUD helpers ──────────────────────────────────────

    @staticmethod
    def get_or_create(db: Any, user_id: str, group_id: str, user_name: str) -> dict[str, Any]:
        """Fetch affection record, creating with defaults if not present.
        Applies decay lazily on read."""
        rows = db.fetch_data(
            "SELECT affection_score, interaction_count, positive_count, negative_count, "
            "last_interaction, relationship, notes "
            "FROM user_affection WHERE user_id=? AND group_id=?",
            (user_id, group_id),
        )
        if not rows:
            # Insert default record
            db.execute_action(
                "INSERT INTO user_affection (user_id, group_id, user_name, affection_score, "
                "interaction_count, positive_count, negative_count, relationship, notes) "
                "VALUES (?, ?, ?, ?, 0, 0, 0, 'neutral', '')",
                (user_id, group_id, user_name, SCORE_DEFAULT),
            )
            return {
                "affection_score": SCORE_DEFAULT,
                "interaction_count": 0,
                "positive_count": 0,
                "negative_count": 0,
                "last_interaction": None,
                "relationship": "neutral",
                "notes": "",
            }

        r = rows[0]
        score = float(r[0])
        last_int = r[4]
        # Apply decay
        score = AffectionService.apply_decay(score, last_int)
        return {
            "affection_score": score,
            "interaction_count": int(r[1]),
            "positive_count": int(r[2]),
            "negative_count": int(r[3]),
            "last_interaction": last_int,
            "relationship": r[5] or "neutral",
            "notes": r[6] or "",
        }

    @staticmethod
    def update_score(
        db: Any, user_id: str, group_id: str, user_name: str,
        delta: float, *, positive: bool = False, negative: bool = False,
    ) -> float:
        """Apply a score delta, clamp, update DB. Returns new score."""
        record = AffectionService.get_or_create(db, user_id, group_id, user_name)
        new_score = max(SCORE_MIN, min(SCORE_MAX, record["affection_score"] + delta))
        pos_count = record["positive_count"] + (1 if positive and delta > 0 else 0)
        neg_count = record["negative_count"] + (1 if negative and delta < 0 else 0)
        relationship, _ = AffectionService.get_relationship(new_score)

        db.execute_action(
            "UPDATE user_affection SET affection_score=?, interaction_count=?, "
            "positive_count=?, negative_count=?, last_interaction=datetime('now','localtime'), "
            "relationship=?, user_name=?, updated_at=datetime('now','localtime') "
            "WHERE user_id=? AND group_id=?",
            (new_score, record["interaction_count"] + 1, pos_count, neg_count,
             relationship, user_name, user_id, group_id),
        )
        return new_score

    # ── Main scoring entry point ──────────────────────────

    @staticmethod
    def record_interaction(
        db: Any, user_id: str, group_id: str | None, user_name: str,
    ) -> float | None:
        """Record a valid interaction (base point + first-interaction bonus).
        Sentiment / learning deltas are applied asynchronously by JudgeService.
        Returns None for private chats (no group_id)."""
        if not group_id:
            return None

        # 1. Base interaction point
        record = AffectionService.get_or_create(db, user_id, group_id, user_name)
        already = record["interaction_count"]

        # Daily cap check: count today's interactions
        today = datetime.now().strftime("%Y-%m-%d")
        today_count_rows = db.fetch_data(
            "SELECT COUNT(*) FROM user_affection_log WHERE user_id=? AND group_id=? AND date=?",
            (user_id, group_id, today),
        )
        today_count = today_count_rows[0][0] if today_count_rows else 0

        delta = 0.0

        # Base interaction (capped daily)
        if today_count < MAX_INTERACTION_BONUS_PER_DAY:
            delta += INTERACTION_POINT

        # 2. Daily first-interaction bonus
        if already == 0 or not record.get("last_interaction"):
            delta += 0.2
        elif record.get("last_interaction"):
            try:
                last_date = datetime.strptime(
                    str(record["last_interaction"]), "%Y-%m-%d %H:%M:%S"
                ).date()
                if last_date < datetime.now().date():
                    delta += 0.2  # first interaction today
            except (ValueError, TypeError):
                pass

        # Apply
        if delta != 0:
            new_score = AffectionService.update_score(
                db, user_id, group_id, user_name, delta,
            )

            # Log for daily cap tracking
            try:
                db.execute_action(
                    "INSERT INTO user_affection_log (user_id, group_id, date, delta) "
                    "VALUES (?, ?, ?, ?)",
                    (user_id, group_id, today, round(delta, 2)),
                )
            except Exception:
                pass  # log table may not exist yet, non-critical

            label, emoji = AffectionService.get_relationship(new_score)
            logger.info(
                "Affection %s(%s): %+.1f → %.1f %s%s",
                user_name, user_id, delta, new_score, emoji, label,
            )
            return new_score

        return record["affection_score"]

    # ── Tool usage bonus ──────────────────────────────────

    @staticmethod
    def record_tool_usage(
        db: Any, user_id: str, group_id: str | None, user_name: str,
    ) -> None:
        """Minimal bonus when user triggers a tool — just using features."""
        if not group_id:
            return
        AffectionService.update_score(
            db, user_id, group_id, user_name, 0.05,
        )

    # ── Learning feedback integration ─────────────────────

    @staticmethod
    def learning_delta(lesson_type: str | None) -> float:
        """Score delta for a structured learning verdict type (0.0 if unknown/None)."""
        return LEARNING_SCORE_MAP.get(lesson_type or "", 0.0)

    # ── System prompt context ─────────────────────────────

    @staticmethod
    def build_context_prompt(
        db: Any, user_id: str, group_id: str | None, user_name: str,
    ) -> str:
        """Build affection context string for injection into system prompt."""
        if not group_id:
            return ""

        record = AffectionService.get_or_create(db, user_id, group_id, user_name)
        score = record["affection_score"]
        label, emoji = AffectionService.get_relationship(score)
        notes = record.get("notes", "")

        lines = [
            "【你和这个人的关系】（心里有数就行，别把好感度念出来）",
            f"{user_name}：好感度 {score:.0f}/100（{emoji}{label}），"
            f"聊过 {record['interaction_count']} 次，"
            f"被 TA 夸过 {record['positive_count']} 次、怼过 {record['negative_count']} 次。",
        ]

        # Tone guidance — phrased as Kiriko's own feeling, not as a rule to obey
        if score >= 80:
            lines.append("你对 TA 很亲近，说话随意点，可以撒娇，也可以吐槽对方。")
        elif score >= 60:
            lines.append("你跟 TA 挺熟，语气自然亲切，可以主动关心两句。")
        elif score >= 40:
            lines.append("普通朋友，正常聊天就行。")
        elif score >= 20:
            lines.append("还不太熟，话少一点，也不用刻意热络。")
        else:
            lines.append("你对 TA 印象一般，别硬凑近乎，正常回就行。")

        if notes:
            lines.append(f"关系备注：{notes}")

        return "\n".join(lines)

    # ── Leaderboard ───────────────────────────────────────

    @staticmethod
    def get_leaderboard(
        db: Any, group_id: str | None = None, limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Return top-N users by affection score, optionally filtered by group."""
        if group_id:
            rows = db.fetch_data(
                "SELECT user_id, user_name, affection_score, interaction_count, "
                "positive_count, negative_count, relationship "
                "FROM user_affection WHERE group_id=? "
                "ORDER BY affection_score DESC LIMIT ?",
                (group_id, limit),
            )
        else:
            rows = db.fetch_data(
                "SELECT user_id, user_name, affection_score, interaction_count, "
                "positive_count, negative_count, relationship, group_id "
                "FROM user_affection "
                "ORDER BY affection_score DESC LIMIT ?",
                (limit,),
            )

        return [
            {
                "user_id": r[0],
                "user_name": r[1],
                "affection_score": round(float(r[2]), 1),
                "interaction_count": int(r[3]),
                "positive_count": int(r[4]),
                "negative_count": int(r[5]),
                "relationship": r[6],
                "emoji": AffectionService.get_relationship(float(r[2]))[1],
            }
            for r in rows
        ]

    # ── Manual adjustment (for dashboard) ─────────────────

    @staticmethod
    def manual_adjust(
        db: Any, user_id: str, group_id: str, delta: float, note: str = "",
    ) -> dict[str, Any]:
        """Manually adjust a user's affection score via dashboard."""
        record = AffectionService.get_or_create(db, user_id, group_id, "unknown")
        new_score = max(SCORE_MIN, min(SCORE_MAX, record["affection_score"] + delta))
        relationship, emoji = AffectionService.get_relationship(new_score)
        notes = record.get("notes", "")
        if note:
            notes = (notes + "; " + note).strip("; ")

        db.execute_action(
            "UPDATE user_affection SET affection_score=?, relationship=?, notes=?, "
            "updated_at=datetime('now','localtime') WHERE user_id=? AND group_id=?",
            (new_score, relationship, notes, user_id, group_id),
        )
        return {
            "user_id": user_id,
            "group_id": group_id,
            "affection_score": new_score,
            "relationship": relationship,
            "emoji": emoji,
            "notes": notes,
        }
