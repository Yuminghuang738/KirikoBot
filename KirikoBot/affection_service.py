"""好感度：**事件制**。

## 为什么改

之前是积分制：每条消息 +0.1（日上限 5）、每次工具调用 +0.05（**无上限**）、
判官按条给 ±2.0（**也无总量上限**）。三个后果：

1. **刷分极容易** —— 判官 ±2 不限总量，25 条消息就能从 50 刷到 100；
   工具加分更是完全无上限，随便点几下就有分。
2. **下半区是死的** —— 掉分封底卡在 50，`普通(20~40)` 和 `冷淡(0~20)` 两档
   在数学上到不了，所以"关系变差"这件事根本不存在。
3. **分布两段化** —— 生产库 70 条记录：6 人顶在 100 分，58 人挤在初始值 50，
   中间和下半区**一个人都没有**。

现在奖励的是「**发生过什么**」，不是「说了多少句」：

    事件               分值      上限
    当天首次出现       +0.3      1/天
    第一次说话         +1.0      一次性
    连续出现           +0.05×天数（封顶 0.5）   1/天
    深夜出现(0-5点)    +0.1      1/天
    发图/表情包        +0.1      1/天
    判官情绪分         ±0.3      每日净额 ±1.0

    每日事件分总计上限 +3.0

刷屏拿不到分，因为「连续出现 N 天」没法在一个下午完成。掉分封底从 50 降到 20，
`普通` / `冷淡` 终于可达 —— 关系可以变差，这才有意义。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

# ── Relationship thresholds ─────────────────────────────
# 从 80/60/40/20 调成 85/65/40/20：顶端收紧一点，让"挚友"真的稀有
RELATIONSHIP_LEVELS = [
    (85, "挚友", "🌟"),
    (65, "亲密", "💕"),
    (40, "友好", "😊"),
    (20, "普通", "👋"),
    (0,  "冷淡", "❄️"),
]

# ── Score bounds ─────────────────────────────────────────
SCORE_MIN = 0.0
SCORE_MAX = 100.0
SCORE_DEFAULT = 50.0

# ── Decay ────────────────────────────────────────────────
DECAY_PER_WEEK = 1.5        # 一周没出现掉 1.5 分（原来是 0.5）
DECAY_FLOOR = 20.0          # 封底从 50 降到 20 —— 普通/冷淡才可达
LONG_ABSENCE_DAYS = 30      # 超过这个天数没出现
LONG_ABSENCE_EXTRA = 3.0    # 额外再掉这么多（一次性，靠日志去重）

# ── Daily caps ───────────────────────────────────────────
DAILY_EVENT_CAP = 3.0       # 每天从「事件」拿到的总分上限

# ── Event values ─────────────────────────────────────────
EVENT_FIRST_TODAY = 0.3     # 当天第一次出现
EVENT_FIRST_EVER = 1.0      # 第一次跟 Kiriko 说话
EVENT_STREAK_PER_DAY = 0.05 # 连续出现，每天多一点
EVENT_STREAK_CAP = 0.5      # 连续加分封顶
EVENT_DEEP_NIGHT = 0.1      # 0-5 点出现
EVENT_MEDIA = 0.1           # 发了图/表情包

# ── Judge sentiment（非对称）──────────────────────────────
# 正面：单条最多 +0.3，每天净额最多 +1.0 —— 好感要慢慢攒。
# 负面：单条最多 -0.6，每天净额最多 -2.0 —— 信任被破坏的速度通常快于建立。
# 之前正负对称（±0.3）：线上 95 条正向 : 27 条负向，37 人有负面计数却仍在友好档以上，
# 负面永远追不上日常的小正分。
SENTIMENT_MAX = 0.3         # 单条正面上限
SENTIMENT_NEG_MAX = 0.6     # 单条负面上限
SENTIMENT_DAILY_NET = 1.0   # 每日正面净额上限
SENTIMENT_DAILY_NEG = 2.0   # 每日负面净额上限

# ── Learning feedback score mapping ──────────────────────
LEARNING_SCORE_MAP: dict[str, float] = {
    "表现良好": 0.5,
    "工具选择错误": -0.2,
    "回复不当": -0.3,
    "遗漏工具": -0.1,
}


def clamp_score(value: float) -> float:
    return max(SCORE_MIN, min(SCORE_MAX, float(value)))


def recalibrated_score(days_seen: int, streak: int) -> float:
    """重算分数：只看「来过多少天」和「连续多久」。

    一次性把历史上按积分制刷出来的分数折算回合理区间。**不看旧分数** ——
    旧分数本身就是错的（判官 ±2 无上限、工具加分无上限），拿它做基准等于把
    刷出来的结果又请回来。

        来过天数  0 → +0     41 天以上 → +35（封顶）
        连续天数  0 → +0     10 天以上 → +5（封顶）

    基准 50（初始值）。所以：
        - 只来一天 → 约 51 分（友好档下沿）
        - 连续来两个月 → 约 90 分（挚友）
    """
    days = max(0, int(days_seen or 0))
    streak = max(0, int(streak or 0))
    from_days = min(35.0, days * 0.85)
    from_streak = min(5.0, streak * 0.5)
    return clamp_score(SCORE_DEFAULT + from_days + from_streak)


class AffectionService:
    """好感度：事件驱动，不是发言量驱动。

    这个服务不做任何 API 调用、不做关键词匹配：事件分是确定性的，
    情绪分从 judge_service 异步过来。
    """

    # ── Relationship level ────────────────────────────────

    @staticmethod
    def get_relationship(score: float) -> tuple[str, str]:
        """Return (label, emoji) for a given score."""
        for threshold, label, emoji in RELATIONSHIP_LEVELS:
            if score >= threshold:
                return label, emoji
        return "冷淡", "❄️"

    @staticmethod
    def is_close(score: float) -> bool:
        """亲密以上 —— 说话可以更随意、可以傲娇、可以用称呼。"""
        return score >= 65

    # ── Decay computation (lazy, applied on read) ────────

    @staticmethod
    def apply_decay(
        score: float, last_interaction_str: str | None,
    ) -> float:
        """按不活跃时长把分数往 DECAY_FLOOR 拉。"""
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
        if days_since >= LONG_ABSENCE_DAYS:
            decay += LONG_ABSENCE_EXTRA
        return max(DECAY_FLOOR, score - decay)

    # ── CRUD helpers ──────────────────────────────────────

    @staticmethod
    def get_or_create(db: Any, user_id: str, group_id: str, user_name: str) -> dict[str, Any]:
        """Fetch affection record, creating with defaults if not present."""
        rows = db.fetch_data(
            "SELECT affection_score, interaction_count, positive_count, negative_count, "
            "last_interaction, relationship, notes, streak_days, last_seen_date "
            "FROM user_affection WHERE user_id=? AND group_id=?",
            (user_id, group_id),
        )
        if not rows:
            db.execute_action(
                "INSERT INTO user_affection (user_id, group_id, user_name, affection_score, "
                "interaction_count, positive_count, negative_count, relationship, notes, "
                "streak_days, last_seen_date) "
                "VALUES (?, ?, ?, ?, 0, 0, 0, 'neutral', '', 0, '')",
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
                "streak_days": 0,
                "last_seen_date": "",
            }

        r = rows[0]
        score = AffectionService.apply_decay(float(r[0]), r[4])
        return {
            "affection_score": score,
            "interaction_count": int(r[1]),
            "positive_count": int(r[2]),
            "negative_count": int(r[3]),
            "last_interaction": r[4],
            "relationship": r[5] or "neutral",
            "notes": r[6] or "",
            "streak_days": int(r[7] or 0),
            "last_seen_date": r[8] or "",
        }

    @staticmethod
    def _apply(
        db: Any, user_id: str, group_id: str, user_name: str,
        delta: float, *, positive: bool = False, negative: bool = False,
    ) -> float:
        """只改分数，不写日志。日志的粒度由调用方决定。"""
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

    @staticmethod
    def update_score(
        db: Any, user_id: str, group_id: str, user_name: str,
        delta: float, *, positive: bool = False, negative: bool = False,
        event: str = "",
    ) -> float:
        """Apply one score delta + log one event row."""
        new_score = AffectionService._apply(
            db, user_id, group_id, user_name, delta,
            positive=positive, negative=negative,
        )
        AffectionService._log(db, user_id, group_id, delta, event or "misc")
        return new_score

    # ── 每日日志（既做流水，也做限额判定） ────────────────

    @staticmethod
    def _today() -> str:
        return datetime.now().strftime("%Y-%m-%d")

    @staticmethod
    def _log(db: Any, user_id: str, group_id: str, delta: float, event: str) -> None:
        try:
            db.execute_action(
                "INSERT INTO user_affection_log (user_id, group_id, date, delta, event) "
                "VALUES (?, ?, ?, ?, ?)",
                (user_id, group_id, AffectionService._today(), round(delta, 3), event),
            )
        except Exception:
            # 日志表可能还不存在；限额会因此偏宽松，但不影响主流程
            logger.debug("affection log write failed", exc_info=True)

    @staticmethod
    def _today_logs(db: Any, user_id: str, group_id: str) -> list[tuple[str, float]]:
        """今天的事件流水：[(event, delta), ...]。一次查询，避免逐条问库。"""
        try:
            rows = db.fetch_data(
                "SELECT event, delta FROM user_affection_log "
                "WHERE user_id=? AND group_id=? AND date=?",
                (user_id, group_id, AffectionService._today()),
            )
        except Exception:
            return []
        return [(r[0] or "misc", float(r[1] or 0)) for r in rows]

    # ── Main scoring entry point ──────────────────────────

    @staticmethod
    def record_interaction(
        db: Any, user_id: str, group_id: str | None, user_name: str,
        has_images: bool = False,
    ) -> float | None:
        """一条消息触发的**事件**计分（不是"发言条数计分"）。

        返回新的分数；私聊不计（好感度是群内关系）。
        """
        if not group_id:
            return None

        record = AffectionService.get_or_create(db, user_id, group_id, user_name)
        logs = AffectionService._today_logs(db, user_id, group_id)
        counts: dict[str, int] = {}
        for event, _delta in logs:
            counts[event] = counts.get(event, 0) + 1

        # 今天已经从「事件」拿到的分（情绪分单独算，不占这个额度）
        event_gain_today = sum(d for e, d in logs if e != "sentiment")
        budget = DAILY_EVENT_CAP - max(0.0, event_gain_today)

        picked: list[tuple[str, float]] = []

        def pick(event: str, value: float) -> None:
            nonlocal budget
            if counts.get(event, 0) >= 1:
                return                      # 这类事件今天已经计过
            if budget <= 0:
                return                      # 今日额度用完
            value = min(value, budget)
            if value <= 0:
                return
            picked.append((event, value))
            counts[event] = 1
            budget -= value

        # ① 第一次跟 Kiriko 说话
        if record["interaction_count"] == 0:
            pick("first_ever", EVENT_FIRST_EVER)

        # ② 当天第一次出现
        pick("first_today", EVENT_FIRST_TODAY)

        # ③ 连续出现（看 streak，不看今天来了几次）
        streak = int(record.get("streak_days") or 0)
        if streak >= 2:
            pick("streak", min(EVENT_STREAK_CAP, EVENT_STREAK_PER_DAY * streak))

        # ④ 深夜出现
        if 0 <= datetime.now().hour < 5:
            pick("deep_night", EVENT_DEEP_NIGHT)

        # ⑤ 发过图/表情包
        if has_images:
            pick("media", EVENT_MEDIA)

        if not picked:
            return record["affection_score"]

        total = round(sum(v for _e, v in picked), 2)
        new_score = AffectionService._apply(
            db, user_id, group_id, user_name, total, positive=True,
        )
        # **每个事件单独一行日志** —— 合成一行（"a+b"）会让"每类每天限一次"
        # 的计数对不上，同一类事件会被重复触发
        for event, value in picked:
            AffectionService._log(db, user_id, group_id, value, event)
        label, emoji = AffectionService.get_relationship(new_score)
        logger.info(
            "Affection %s(%s): %+.2f [%s] → %.1f %s%s",
            user_name, user_id, total, "+".join(e for e, _v in picked),
            new_score, emoji, label,
        )
        return new_score

    # ── Streak bookkeeping ────────────────────────────────

    @staticmethod
    def touch_streak(db: Any, user_id: str, group_id: str | None, user_name: str) -> int:
        """更新连续出现天数（每天第一次调用时才真正变化）。返回新的 streak。"""
        if not group_id:
            return 0
        record = AffectionService.get_or_create(db, user_id, group_id, user_name)
        today = AffectionService._today()
        if record.get("last_seen_date") == today:
            return int(record.get("streak_days") or 0)

        streak = int(record.get("streak_days") or 0)
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        streak = streak + 1 if record.get("last_seen_date") == yesterday else 1

        try:
            db.execute_action(
                "UPDATE user_affection SET streak_days=?, last_seen_date=? "
                "WHERE user_id=? AND group_id=?",
                (streak, today, user_id, group_id),
            )
        except Exception:
            logger.debug("streak update failed", exc_info=True)
        return streak

    # ── Tool usage: 不再加分 ──────────────────────────────

    @staticmethod
    def record_tool_usage(
        db: Any, user_id: str, group_id: str | None, user_name: str,
    ) -> None:
        """**故意什么都不做。**

        这里原来给每次工具调用 +0.05，没有任何上限 —— 是刷分的主要入口
        （随便点几下就有分，且与"关系"无关）。现在工具的"使用"不再加分，
        想知道一个人来不来，看 streak 和 first_today 就够了。
        """
        return None

    # ── Judge sentiment ───────────────────────────────────

    @staticmethod
    def apply_judge_delta(
        db: Any, user_id: str, group_id: str, user_name: str,
        sentiment: float, learning: float = 0.0,
    ) -> float | None:
        """判官给的分。两个分量要分开看：

        - **情绪分**单条最大 ±0.3 —— 它只是微调，不该成为刷分主力
          （原来 ±2.0 且不限总量，25 条消息就能刷满）
        - **学习分**（表现良好 / 回复不当…）反映的是**机器人**的质量，保留原值
        - 两者合计的**每日净额**钳到 ±1.0
        """
        if not group_id:
            return None
        if sentiment > 0:
            sentiment = min(sentiment, SENTIMENT_MAX)
        else:
            sentiment = max(sentiment, -SENTIMENT_NEG_MAX)
        total = round(sentiment + learning, 2)
        if abs(total) < 0.05:
            return None
        logs = AffectionService._today_logs(db, user_id, group_id)
        net_today = sum(d for e, d in logs if e == "sentiment")
        if total > 0:
            total = min(total, SENTIMENT_DAILY_NET - net_today)
        else:
            total = max(total, -SENTIMENT_DAILY_NEG - net_today)
        if abs(total) < 0.05:
            return None
        return AffectionService.update_score(
            db, user_id, group_id, user_name, round(total, 2),
            positive=total > 0, negative=total < 0, event="sentiment",
        )

    @staticmethod
    def learning_delta(lesson_type: str | None) -> float:
        """Score delta for a structured learning verdict type (0.0 if unknown/None)."""
        return LEARNING_SCORE_MAP.get(lesson_type or "", 0.0)

    # ── System prompt context ─────────────────────────────

    @staticmethod
    def relationship_block(db: Any, user_id: str, group_id: str | None) -> str:
        """关系那一块 —— **放进 user message**，不进系统提示词。

        它随"谁在说话"变化，放 system prompt 里等于每换个人就废掉整段前缀缓存。
        """
        if not group_id:
            return ""
        record = AffectionService.get_or_create(db, user_id, group_id, "")
        score = record["affection_score"]
        label, emoji = AffectionService.get_relationship(score)

        # 只给「档位 + 一句可执行的行为」。不放统计数字：实测「聊过 542 次 / 怼过 21」这类
        # 数字会被模型直接拿来说事，去掉后才有可测的正向贡献（见 eval/chat_replay.py）。
        lines = [f"【你和他的关系】{label}"]
        lines.append(AffectionService.behavior_for(score))

        if record.get("notes"):
            lines.append(f"备注：{record['notes']}")
        return "\n".join(lines)

    @staticmethod
    def behavior_for(score: float) -> str:
        """每个关系档位对应的一句具体行为指引（可执行，不是形容词）。"""
        if score >= 85:
            return "很熟：可以接他的梗、吐槽他、直接叫名字，他提过的事你记着，顺口问一句。"
        if score >= 65:
            return "熟：语气放松，记得他之前说过的事，可以顺口提一句。"
        if score >= 40:
            return "一般朋友：正常接话，不翻旧账，不主动问他的私事。"
        if score >= 20:
            return "不太熟：简短接一句，不主动聊他的事。"
        return "印象不好：一两个字能答就别多说，不接梗。"

    # 兼容旧调用名（prompt_builder 之外的调用点还在用）
    build_context_prompt = relationship_block

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
