from __future__ import annotations

import json
import logging
from typing import Any

import requests

from config import Config

logger = logging.getLogger(__name__)


class ProfileService:
    """Analyzes user messages via DeepSeek to generate personality profiles."""

    MIN_MESSAGES = 20       # minimum messages before profiling
    MAX_SAMPLE_MSGS = 50    # max messages to send for analysis
    REANALYZE_GAP = 100     # re-analyze every N new messages

    SYSTEM_PROMPT = (
        "你是一个用户画像分析助手。根据用户的聊天记录，分析用户特征。"
        "以JSON格式输出，不要有其他内容：\n"
        '{"personality":"性格描述(10字内)","interests":["兴趣1","兴趣2"],'
        '"speaking_style":"说话风格","topics":["常聊话题"],'
        '"mood":"情绪状态","relationship":"与群友关系","note":"备注"}\n'
        "只输出JSON，不要markdown代码块。"
    )

    def __init__(self) -> None:
        self._analyzing: set[str] = set()  # prevent duplicate analysis

    @staticmethod
    def _is_bot(user_id: str) -> bool:
        """Check if a user_id is a known bot account."""
        return user_id in Config.BOT_QQ_LIST

    def should_analyze(self, db: Any, user_id: str, group_id: str) -> bool:
        """Check if user needs profile analysis."""
        if user_id in self._analyzing:
            return False
        if self._is_bot(user_id):
            return False
        existing = db.get_user_profile(user_id)
        if not existing:
            return True
        # Re-analyze if enough new messages accumulated
        total = db.fetch_data(
            "SELECT COUNT(*) FROM group_messages WHERE user_id=? AND group_id=?",
            (user_id, group_id),
        )[0][0]
        old_count = existing.get("message_count", 0)
        return (total - old_count) >= self.REANALYZE_GAP

    def analyze_user(
        self, db: Any, user_id: str, group_id: str, user_name: str,
    ) -> dict[str, Any] | None:
        """Analyze a user's messages and save profile. Non-blocking wrapper."""
        if user_id in self._analyzing:
            return None
        if self._is_bot(user_id):
            return None
        self._analyzing.add(user_id)
        try:
            return self._do_analyze(db, user_id, group_id, user_name)
        finally:
            self._analyzing.discard(user_id)

    def _do_analyze(
        self, db: Any, user_id: str, group_id: str, user_name: str,
    ) -> dict[str, Any] | None:
        messages = db.get_user_messages(user_id, group_id, self.MAX_SAMPLE_MSGS)
        if len(messages) < self.MIN_MESSAGES:
            logger.info(
                "User %s has %d messages (<%d), skipping profile",
                user_name, len(messages), self.MIN_MESSAGES,
            )
            return None

        # Build message sample
        sample = "\n".join(
            f"[{ts}]: {content[:200]}" for content, ts in messages[:self.MAX_SAMPLE_MSGS]
        )

        # Count total messages
        total_count = db.fetch_data(
            "SELECT COUNT(*) FROM group_messages WHERE user_id=? AND group_id=?",
            (user_id, group_id),
        )[0][0]

        try:
            response = requests.post(
                Config.DEEPSEEK_API,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {Config.DEEPSEEK_TOKEN}",
                },
                json={
                    "messages": [
                        {"role": "system", "content": self.SYSTEM_PROMPT},
                        {"role": "user", "content": f"用户 {user_name} 的聊天记录：\n{sample}"},
                    ],
                    "model": "deepseek-v4-flash",
                    "thinking": {"type": "disabled"},
                    "max_tokens": 500,
                    "temperature": 0.3,
                    "response_format": {"type": "text"},
                },
                timeout=30,
            )
            response.raise_for_status()
            result = response.json()["choices"][0]["message"]["content"]
        except Exception:
            logger.exception("Profile analysis API failed for %s", user_name)
            return None

        # Parse JSON from response
        try:
            # Strip markdown code fences if present
            result = result.strip()
            if result.startswith("```"):
                result = result.split("\n", 1)[1].rsplit("\n", 1)[0]
            profile = json.loads(result)
        except (json.JSONDecodeError, ValueError):
            logger.warning("Failed to parse profile JSON for %s: %s", user_name, result[:100])
            profile = {"personality": "未知", "note": result[:200]}

        # Save to database
        try:
            db.save_user_profile(
                user_id, group_id, user_name,
                json.dumps(profile, ensure_ascii=False), total_count,
            )
        except Exception:
            logger.exception("Failed to save profile for %s", user_name)
            return None

        logger.info(
            "Profile saved for %s: %s (%s)",
            user_name, profile.get("personality", "?"), profile.get("interests", []),
        )
        return profile

    def build_context_prompt(
        self, db: Any, group_id: str, current_user_id: str,
    ) -> str:
        """Build a context string about group members for the AI."""
        profiles = db.get_group_profiles(group_id)
        if not profiles:
            return ""

        lines = ["【群友画像】"]
        for p in profiles:
            pf = p["profile"]
            if not pf:
                continue
            # Highlight current speaker
            tag = " ← 当前发言" if p["user_id"] == current_user_id else ""
            lines.append(
                f"- {p['user_name']}: {pf.get('personality', '?')}"
                f" | 兴趣:{','.join(pf.get('interests', ['?']))}"
                f" | {pf.get('speaking_style', '?')}{tag}"
            )
            if pf.get("note"):
                lines.append(f"  备注: {pf['note']}")
        return "\n".join(lines[:20])  # limit to 20 profiles
