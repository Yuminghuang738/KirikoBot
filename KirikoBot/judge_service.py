from __future__ import annotations

import json
import logging
from typing import Any

import requests

from affection_service import AffectionService
from ai_server import quick_chat
from config import Config

logger = logging.getLogger(__name__)


def parse_json_loose(raw: str) -> dict[str, Any] | None:
    """Fence-strip + json.loads + brace-slice fallback. Reusable helper."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("\n", 1)[0].strip()
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        try:
            data = json.loads(text[text.index("{"): text.rindex("}") + 1])
        except Exception:
            logger.warning("Judge returned non-JSON: %s", text[:120])
            return None
    return data if isinstance(data, dict) else None


class JudgeService:
    """ONE cheap flash call per group @bot message that judges the sentiment of
    the current message. Feeds the affection score. Fire-and-forget only: never on
    the critical path, silent on any failure.

    自学习（上一轮的教训记录）已移除：模型侧的决策不靠外挂笔记纠正，记录几乎没有作用还白白占输入。
    """

    MODEL = Config.DEEPSEEK_MODEL
    TIMEOUT = 15
    MAX_TOKENS = 120
    MAX_DELTA = 2.0        # hard clamp for the per-message delta
    MIN_DELTA = 0.05       # below this, no DB write at all

    SYSTEM_PROMPT = (
        "你是 Kiriko 的情绪分析器。"
        "输出必须是严格 JSON，不要 markdown 代码块，不要任何解释文字。"
    )

    # ── Public entry ──────────────────────────────────────

    def judge_turn(self, db: Any, robot: Any, *, affection_on: bool = True) -> dict[str, Any] | None:
        """Judge the sentiment of the current message. Returns a summary dict, or
        None on any failure. Never raises."""
        if not affection_on or not robot.group_id:
            return None

        record = AffectionService.get_or_create(
            db, robot.user_id, robot.group_id, robot.user_name,
        )
        raw = self._call_api(self._build_prompt(robot, record))
        if raw is None:
            return None
        data = parse_json_loose(raw)
        if data is None:
            return None

        delta, reason = self._clean_sentiment(data)
        new_score = None
        if abs(delta) >= self.MIN_DELTA:
            # 情绪分单条 ±0.3、正负分别有每日净额上限（钳制在 service 里）。
            new_score = AffectionService.apply_judge_delta(
                db, robot.user_id, robot.group_id, robot.user_name, sentiment=delta,
            )

        logger.info(
            "AI judge %s: sentiment=%+.2f (%s) score=%s",
            robot.user_name, delta, reason or "-",
            f"{new_score:.1f}" if new_score is not None else "n/a",
        )
        return {"sentiment": delta, "delta": delta}

    # ── Prompt ────────────────────────────────────────────

    def _build_prompt(self, robot: Any, record: dict[str, Any] | None) -> str:
        lines = [f"【待判断消息】\n群「{robot.group_name or ''}」中用户 {robot.user_name} 说：{robot.msg}"]

        if record is not None:
            label, _ = AffectionService.get_relationship(record["affection_score"])
            lines.append(
                f"【当前关系】你与 {robot.user_name} 的好感度 {record['affection_score']:.0f}/100（{label}）"
            )

        lines.append(
            "请判断这条消息对机器人表达的情绪倾向：\n"
            "delta 为 -0.8 ~ +0.8 之间的小数：普通感谢/称赞 +0.2~+0.4；强烈喜爱/表白 +0.6~+0.8；\n"
            "轻微吐槽/不满 -0.2~-0.4；明显敌意/辱骂 -0.6~-0.8；纯闲聊或无情绪为 0。\n"
            "默认是 0：普通的接话、问问题、闲聊都不打分，只有**明显**的情绪才给分。\n"
            "不要因为出现某个词就打分，要理解整体语气与意图。\n\n"
            "只输出下面的 JSON，不要注释、不要多余字段：\n"
            '{"sentiment":{"delta":0.0,"reason":"20字以内的理由"}}'
        )
        return "\n\n".join(lines)

    # ── API ───────────────────────────────────────────────

    def _call_api(self, prompt: str) -> str | None:
        text = quick_chat(
            self.SYSTEM_PROMPT, prompt,
            max_tokens=self.MAX_TOKENS, temperature=0, timeout=self.TIMEOUT,
            model=self.MODEL, source="judge",
        )
        if text is None:
            logger.info("AI judge skipped (API unavailable)")
        return text

    # ── Parsing / validation (pure, unit-testable) ────────

    @staticmethod
    def _clean_sentiment(data: dict[str, Any]) -> tuple[float, str]:
        obj = data.get("sentiment")
        if not isinstance(obj, dict):
            return 0.0, ""
        try:
            delta = float(obj.get("delta", 0) or 0)
        except (TypeError, ValueError):
            delta = 0.0
        delta = max(-JudgeService.MAX_DELTA, min(JudgeService.MAX_DELTA, delta))
        return round(delta, 2), str(obj.get("reason") or "")[:100]
