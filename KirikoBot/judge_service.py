from __future__ import annotations

import json
import logging
from typing import Any

import requests

from affection_service import AffectionService, LEARNING_SCORE_MAP
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
    """ONE cheap flash call per group @bot message that judges both
    - sentiment of the current message (replaces keyword scoring in affection_service)
    - the previous turn's lesson (replaces learning_service.evaluate_and_learn)
    Fire-and-forget only: never on the critical path, silent on any failure."""

    MODEL = "deepseek-v4-flash"
    TIMEOUT = 15
    MAX_TOKENS = 220
    MAX_DELTA = 2.0        # hard clamp for the combined per-message delta
    MIN_DELTA = 0.05       # below this, no DB write at all
    MIN_NOTE_LEN = 3

    SYSTEM_PROMPT = (
        "你是 Kiriko 的行为评估与情绪分析器。"
        "输出必须是严格 JSON，不要 markdown 代码块，不要任何解释文字。"
    )

    def __init__(self, learning_service: Any) -> None:
        self.learning = learning_service

    # ── Public entry ──────────────────────────────────────

    def judge_turn(
        self, db: Any, robot: Any, prev_turn: dict[str, str] | None = None, *,
        learning_on: bool = True, affection_on: bool = True,
    ) -> dict[str, Any] | None:
        """Judge the current message (sentiment) + the previous turn (learning).
        Returns a summary dict, or None on any failure. Never raises."""
        if not affection_on and not (learning_on and prev_turn):
            return None
        if affection_on and not robot.group_id:
            return None

        record = (
            AffectionService.get_or_create(
                db, robot.user_id, robot.group_id, robot.user_name,
            )
            if affection_on else None
        )
        raw = self._call_api(self._build_prompt(robot, prev_turn if learning_on else None, record))
        if raw is None:
            return None
        data = parse_json_loose(raw)
        if data is None:
            return None

        ltype, note = self._clean_learning(data) if (learning_on and prev_turn) else (None, "")
        if note and ltype:
            note = f"[{ltype}] {note}"   # keeps bootstrap_affection.py substring matching alive
        delta, reason = self._clean_sentiment(data) if affection_on else (0.0, "")
        learning_delta = AffectionService.learning_delta(ltype)
        total = round(delta + learning_delta, 2)

        if note:
            self.learning.save_note(db, robot.user_id, note, prev_turn)

        new_score = None
        if affection_on and abs(total) >= self.MIN_DELTA:
            new_score = AffectionService.update_score(
                db, robot.user_id, robot.group_id, robot.user_name, total,
                positive=total > 0, negative=total < 0,
            )

        logger.info(
            "AI judge %s: sentiment=%+.2f (%s) learning=%s total=%+.2f score=%s",
            robot.user_name, delta, reason or "-", ltype or "-", total,
            f"{new_score:.1f}" if new_score is not None else "n/a",
        )
        return {"sentiment": delta, "learning": ltype, "note": note, "delta": total}

    # ── Prompt ────────────────────────────────────────────

    def _build_prompt(
        self, robot: Any, prev_turn: dict[str, str] | None, record: dict[str, Any] | None,
    ) -> str:
        lines = [f"【待判断消息】\n群「{robot.group_name or ''}」中用户 {robot.user_name} 说：{robot.msg}"]

        if prev_turn:
            lines.append(
                "【上一轮对话】\n"
                f"- 用户原话：{prev_turn.get('user_msg')}\n"
                f"- AI 调用的工具：{prev_turn.get('tool') or '无(直接回复)'}\n"
                f"- AI 回复：{prev_turn.get('ai_text')}"
            )

        if record is not None:
            label, _ = AffectionService.get_relationship(record["affection_score"])
            lines.append(
                f"【当前关系】你与 {robot.user_name} 的好感度 {record['affection_score']:.0f}/100（{label}）"
            )

        lines.append(
            "请判断两项内容：\n\n"
            "1) sentiment：这条消息对机器人表达的情绪倾向。\n"
            "   delta 为 -0.8 ~ +0.8 之间的小数：普通感谢/称赞 +0.2~+0.4；强烈喜爱/表白 +0.6~+0.8；\n"
            "   轻微吐槽/不满 -0.2~-0.4；明显敌意/辱骂 -0.6~-0.8；纯闲聊或无情绪为 0。\n"
            "   不要因为出现某个词就打分，要理解整体语气与意图；不要因为消息短就给 0。\n\n"
            "2) learning：仅当提供了【上一轮对话】时判断上一轮 AI 的表现。\n"
            "   type 只能是 \"工具选择错误\"、\"遗漏工具\"、\"回复不当\"、\"表现良好\" 之一；\n"
            "   note 用一句话（20-50字）说明具体教训或优点。没有上一轮对话时 learning 必须为 null。\n\n"
            "只输出下面的 JSON，不要注释、不要多余字段：\n"
            '{"sentiment":{"delta":0.0,"reason":"20字以内的理由"},"learning":{"type":"表现良好","note":"..."}}\n'
            '没有上一轮对话时输出：{"sentiment":{"delta":0.0,"reason":"..."},"learning":null}'
        )
        return "\n\n".join(lines)

    # ── API ───────────────────────────────────────────────

    def _call_api(self, prompt: str) -> str | None:
        try:
            r = requests.post(
                Config.DEEPSEEK_API,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {Config.DEEPSEEK_TOKEN}",
                },
                json={
                    "messages": [
                        {"role": "system", "content": self.SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    "model": self.MODEL,
                    "thinking": {"type": "disabled"},
                    "response_format": {"type": "text"},
                    "max_tokens": self.MAX_TOKENS,
                    "temperature": 0,
                    "stream": False,
                },
                timeout=self.TIMEOUT,
            )
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        except Exception:
            logger.info("AI judge skipped (API unavailable)")
            return None

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

    @staticmethod
    def _clean_learning(data: dict[str, Any]) -> tuple[str | None, str]:
        obj = data.get("learning")
        if not isinstance(obj, dict):
            return None, ""
        ltype = obj.get("type")
        ltype = ltype if isinstance(ltype, str) and ltype in LEARNING_SCORE_MAP else None
        note = str(obj.get("note") or "").strip()[:200]
        if len(note) < JudgeService.MIN_NOTE_LEN:
            note = ""
        return ltype, note
