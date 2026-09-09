from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)


class BotScheduler:
    """Background scheduler for reminders + morning greetings."""

    CHECK_INTERVAL = 5  # seconds between checks (supports second-precision reminders)

    def __init__(
        self, db: Any, llbot: Any, political_news: Any, news_crawler: Any,
        hitokoto_service: Any = None, feature_gate: Any = None,
    ) -> None:
        self.db = db
        self.llbot = llbot
        self.political_news = political_news
        self.news_crawler = news_crawler
        self.hitokoto_service = hitokoto_service
        self.feature_gate = feature_gate
        self._running = False
        self._thread: threading.Thread | None = None
        self._last_morning: str = ""

    def _get_active_groups(self) -> list[str]:
        """Get all distinct group IDs from recorded messages."""
        try:
            rows = self.db.fetch_data(
                "SELECT DISTINCT group_id FROM group_messages WHERE group_id IS NOT NULL"
            )
            return [r[0] for r in rows if r[0]]
        except Exception:
            return []

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="scheduler")
        self._thread.start()
        logger.info("Scheduler started")

    def stop(self) -> None:
        self._running = False

    def _loop(self) -> None:
        while self._running:
            try:
                self._check_reminders()
                self._check_greetings()
            except Exception:
                logger.exception("Scheduler loop error")
            time.sleep(self.CHECK_INTERVAL)

    # ── Reminders ──────────────────────────────────────

    def _check_reminders(self) -> None:
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            rows = self.db.fetch_data(
                "SELECT id, user_id, group_id, user_name, content, "
                "remind_time, repeat_daily FROM reminders "
                "WHERE remind_time <= ? AND fired = 0 ORDER BY remind_time LIMIT 5",
                (now,),
            )
            for rid, uid, gid, uname, content, remind_time, repeat_daily in rows:
                self._fire_reminder(rid, uid, gid, uname, content, remind_time, repeat_daily)
        except Exception:
            logger.exception("Reminder check failed")

    def _fire_reminder(
        self, rid: int, uid: str, gid: str | None, uname: str, content: str,
        remind_time: str = "", repeat_daily: int = 0,
    ) -> None:
        # Respect per-group / per-user feature toggles: skip delivery, then
        # finalize like a normal fire (one-shot → fired, daily → tomorrow) so
        # the due row doesn't retry every scheduler tick.
        if self.feature_gate:
            if gid and not self.feature_gate.is_enabled("group", str(gid), "reminder"):
                logger.info("Reminder #%d skipped: reminder disabled in group %s", rid, gid)
                self._finalize_reminder(rid, remind_time, repeat_daily)
                return
            if not gid and not self.feature_gate.is_enabled("user", str(uid), "reminder"):
                logger.info("Reminder #%d skipped: reminder disabled for user %s", rid, uid)
                self._finalize_reminder(rid, remind_time, repeat_daily)
                return

        from llbot_client import MessageBuilder
        builder = MessageBuilder()
        if gid:
            builder.at(uid).text(f" ⏰ 提醒：{content}")
            self.llbot.send_group_msg(gid, builder.build())
        else:
            builder.text(f"⏰ 提醒：{content}")
            self.llbot.send_private_msg(uid, builder.build())

        self._finalize_reminder(rid, remind_time, repeat_daily)
        logger.info("Fired reminder #%d for %s: %s", rid, uname, content[:40])

    def _finalize_reminder(self, rid: int, remind_time: str, repeat_daily: int) -> None:
        """Mark a reminder as handled: reschedule daily ones to tomorrow, fire one-shots."""
        if repeat_daily:
            # Reschedule to same time tomorrow
            try:
                next_time = datetime.strptime(remind_time, "%Y-%m-%d %H:%M:%S") + timedelta(days=1)
                next_str = next_time.strftime("%Y-%m-%d %H:%M:%S")
                self.db.execute_action(
                    "UPDATE reminders SET remind_time=?, fired=0 WHERE id=?",
                    (next_str, rid),
                )
                logger.info("Daily reminder #%d rescheduled to %s", rid, next_str)
            except Exception:
                logger.exception("Failed to reschedule daily reminder #%d", rid)
                self.db.execute_action(
                    "UPDATE reminders SET fired=1 WHERE id=?", (rid,),
                )
        else:
            self.db.execute_action(
                "UPDATE reminders SET fired=1 WHERE id=?", (rid,),
            )

    # ── Morning greeting ────────────────────────────────

    def _check_greetings(self) -> None:
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")

        # Morning: 7:00-7:05
        if now.hour == 7 and now.minute < 5 and self._last_morning != today:
            self._last_morning = today
            threading.Thread(target=self._morning_greeting, daemon=True).start()

    def _morning_greeting(self) -> None:
        groups = self._get_active_groups()
        # Respect per-group feature toggles: skip groups with morning_news off
        if self.feature_gate:
            groups = [
                g for g in groups
                if self.feature_gate.is_enabled("group", g, "morning_news")
            ]
        if not groups:
            return

        # Fetch political news (once for all groups)
        news_items: list[dict[str, str]] = []
        try:
            news_items = self.political_news.translate_news(
                self.political_news.fetch_for_greeting()
            )
        except Exception:
            logger.exception("Morning political news fetch failed")

        gaming_items: list[dict[str, str]] = []
        try:
            gaming_items = self.news_crawler.fetch_gaming_news()
        except Exception:
            pass

        for gid in groups:
            lines = ["☀️ 早上好！新的一天开始啦～ (◕‿◕✿)", ""]

            if news_items:
                lines.append("📰 今日时政要闻：")
                for i, n in enumerate(news_items, 1):
                    src = f" [{n['source']}]" if n.get("source") else ""
                    lines.append(f"  {i}. {n['title']}{src}")
                lines.append("")

            if gaming_items:
                lines.append("🎮 游戏速递：")
                for i, n in enumerate(gaming_items[:3], 1):
                    lines.append(f"  {i}. {n['title']}")
                lines.append("")

            # Daily quote (hitokoto)
            if self.hitokoto_service:
                try:
                    quote = self.hitokoto_service.get_quote()
                    if quote and quote.get("text"):
                        lines.append("💬 每日一言：")
                        lines.append(f"  {quote['text']}")
                        credit_parts = []
                        if quote.get("source"):
                            credit_parts.append(quote["source"])
                            if quote.get("author"):
                                credit_parts.append(quote["author"])
                        if credit_parts:
                            lines.append(f"  —— {' '.join(credit_parts)}")
                        lines.append("")
                except Exception:
                    logger.exception("Hitokoto fetch in morning greeting failed")

            lines.append("祝大家今天元气满满！💪✨")

            from llbot_client import MessageBuilder
            builder = MessageBuilder()
            builder.text("\n".join(lines))
            self.llbot.send_group_msg(gid, builder.build())
            logger.info("Morning greeting sent to %s", gid)


# ── Time parsing for reminders ─────────────────────────

def _cn_to_arabic(text: str) -> str:
    """Convert Chinese numerals in text to Arabic digits."""
    cn_map = {"零": "0", "一": "1", "二": "2", "两": "2", "三": "3", "四": "4",
              "五": "5", "六": "6", "七": "7", "八": "8", "九": "9", "十": "10"}
    # Replace "三十" → "30", "五" → "5", etc.
    result = []
    i = 0
    while i < len(text):
        if text[i:i+2] in cn_map:
            result.append(cn_map[text[i:i+2]])
            i += 2
        elif text[i] in cn_map:
            result.append(cn_map[text[i]])
            i += 1
        else:
            result.append(text[i])
            i += 1
    return "".join(result)


def parse_reminder_time(text: str) -> tuple[str | None, str | None, int]:
    """Extract time and content from a reminder request.
    Returns (remind_time_str, content, repeat_daily) or (None, error_msg, 0)."""
    now = datetime.now()

    # Pre-process Chinese numerals → Arabic
    text = _cn_to_arabic(text)

    # ── Daily recurring patterns ──────────────────────────
    daily_patterns = [
        (r"(?:每天|每日)\s*(\d+)\s*点\s*(\d+)\s*分\s*(.+)",
         lambda m: (now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0), m.group(3))),
        (r"(?:每天|每日)\s*(\d+)\s*点半\s*(.+)",
         lambda m: (now.replace(hour=int(m.group(1)), minute=30, second=0), m.group(2))),
        (r"(?:每天|每日)\s*早上\s*(\d+)\s*点\s*(.+)",
         lambda m: (now.replace(hour=int(m.group(1)), minute=0, second=0), m.group(2))),
        (r"(?:每天|每日)\s*下午\s*(\d+)\s*点\s*(.+)",
         lambda m: (now.replace(hour=12 + int(m.group(1)), minute=0, second=0), m.group(2))),
        (r"(?:每天|每日)\s*晚上\s*(\d+)\s*点\s*(.+)",
         lambda m: (now.replace(hour=12 + int(m.group(1)), minute=0, second=0), m.group(2))),
        (r"(?:每天|每日)\s*上午\s*(\d+)\s*点\s*(.+)",
         lambda m: (now.replace(hour=int(m.group(1)), minute=0, second=0), m.group(2))),
        (r"(?:每天|每日)\s*(\d+)\s*点\s*(.+)",
         lambda m: (now.replace(hour=int(m.group(1)), minute=0, second=0), m.group(2))),
    ]

    for pattern, time_fn in daily_patterns:
        match = re.search(pattern, text)
        if match:
            try:
                remind_time, content = time_fn(match)
            except Exception:
                continue
            if remind_time <= now:
                remind_time += timedelta(days=1)
            # Clean up content
            content = content.strip()
            for prefix in ("提醒我", "提醒", "记得", "别忘了", "叫我", "帮我"):
                if content.startswith(prefix):
                    content = content[len(prefix):].strip()
            while content and content[0] in "的去要来把给":
                content = content[1:].strip()
            if not content:
                content = "未指定内容"
            return (remind_time.strftime("%Y-%m-%d %H:%M:%S"), content, 1)

    # ── One-shot patterns ─────────────────────────────────
    patterns = [
        (r"半\s*小?\s*时\s*后\s*(.+)", lambda m: (now + timedelta(minutes=30), m.group(1))),
        (r"(\d+)\s*秒\s*后\s*(.+)", lambda m: (now + timedelta(seconds=int(m.group(1))), m.group(2))),
        (r"(\d+)\s*分钟\s*后\s*(.+)", lambda m: (now + timedelta(minutes=int(m.group(1))), m.group(2))),
        (r"(\d+)\s*小时\s*后\s*(.+)", lambda m: (now + timedelta(hours=int(m.group(1))), m.group(2))),
        (r"(\d+)\s*点\s*(\d+)\s*分\s*(\d+)\s*秒\s*(.+)", lambda m: (now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=int(m.group(3))), m.group(4))),
        (r"(\d+)\s*点\s*(\d+)\s*分\s*(.+)", lambda m: (now.replace(hour=int(m.group(1)), minute=int(m.group(2))), m.group(3))),
        (r"(\d+)\s*点半\s*(.+)", lambda m: (now.replace(hour=int(m.group(1)), minute=30), m.group(2))),
        (r"明天\s*(\d+)\s*点\s*(.+)", lambda m: ((now + timedelta(days=1)).replace(hour=int(m.group(1)), minute=0), m.group(2))),
        (r"今天\s*(\d+)\s*点\s*(.+)", lambda m: (now.replace(hour=int(m.group(1)), minute=0), m.group(2))),
        (r"下午\s*(\d+)\s*点\s*(.+)", lambda m: (now.replace(hour=12 + int(m.group(1)), minute=0), m.group(2))),
        (r"(\d+)\s*点\s*(.+)", lambda m: (now.replace(hour=int(m.group(1)), minute=0), m.group(2))),
    ]

    for pattern, time_fn in patterns:
        match = re.search(pattern, text)
        if match:
            try:
                remind_time, content = time_fn(match)
            except Exception:
                continue
            if remind_time <= now:
                remind_time += timedelta(days=1)
            # Clean up content: strip "提醒我" / "提醒" prefixes
            content = content.strip()
            for prefix in ("提醒我", "提醒", "记得", "别忘了", "叫我", "帮我"):
                if content.startswith(prefix):
                    content = content[len(prefix):].strip()
            # Also strip leading 的/去/要
            while content and content[0] in "的去要来把给":
                content = content[1:].strip()

            if not content:
                content = "未指定内容"

            return (remind_time.strftime("%Y-%m-%d %H:%M:%S"), content, 0)

    return (None, "无法理解时间，请说'X分钟后提醒我XXX'或'明天X点提醒我XXX'或'每天X点提醒我XXX'", 0)
