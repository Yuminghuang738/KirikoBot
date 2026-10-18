from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Any

import maintenance_service

logger = logging.getLogger(__name__)


class BotScheduler:
    """Background scheduler：提醒 + 每日维护。

    群订阅推送已经整体移除 —— 机器人不再主动往群里推任何东西，
    除了用户自己设的提醒。
    """

    CHECK_INTERVAL = 5  # seconds between checks (supports second-precision reminders)

    def __init__(
        self, db: Any, llbot: Any, feature_gate: Any = None,
    ) -> None:
        self.db = db
        self.llbot = llbot
        self.feature_gate = feature_gate      # 提醒的按群开关要用
        self._running = False
        self._thread: threading.Thread | None = None


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
                # Retention + DB backup, self-guarded to run once per day
                maintenance_service.run_daily(self.db, self.db.db_file)
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


