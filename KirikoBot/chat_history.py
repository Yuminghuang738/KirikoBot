"""Conversation history for the model: how a turn is stored and replayed.

This lives outside `main.py` on purpose. Both halves of the bug fixed here are
invisible at runtime — they only show up as the model answering the wrong
message — so they need to be directly testable, and `main` starts the
scheduler on import.

The invariant this module exists to protect: **the context must never contain
two user turns in a row.** A turn where the bot replied with nothing at all
used to be stored as a lone user row, which left an unanswered question in the
context forever. The next request then carried two questions, and the model
dutifully answered the stale one alongside the new one — the user sees the bot
"replying to my last message again". Measured on a live database, roughly one
turn in ten used to be stored this way.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

MAX_HISTORY = 8  # fewer turns = less noise, more focus on current message


def load_history(db: Any, uid: str, gid: str | None) -> list[dict[str, Any]]:
    """Recent turns for this user in this scope, oldest first."""
    try:
        rows = db.takeout_chat_history(uid, gid)
    except Exception:
        return []
    history: list[dict[str, Any]] = []
    for role, content, tool_calls, _ in rows:
        if role == "user":
            # Two user turns in a row mean the earlier one never got a reply
            # (see save_turn). Only the newest survives: the older one was
            # either already answered by a tool or is stale by now, and
            # keeping both is what makes the model answer two questions.
            if history and history[-1]["role"] == "user":
                history[-1] = {"role": "user", "content": content or ""}
                continue
            history.append({"role": "user", "content": content or ""})
        elif role == "assistant":
            if content and content.strip():
                history.append({"role": "assistant", "content": content.strip()})
            elif tool_calls:
                # The reply went out from the tool itself, so keep a marker in
                # its place: the turn is closed, but the model can still see
                # that something was handled here.
                history.append({"role": "assistant", "content": "[已调用工具处理]"})

    # The caller appends the message being answered AFTER this list, so a
    # history that ends on a user turn would put two user messages back to
    # back — which is precisely the reported bug. A completed turn is always
    # stored as a pair, so a trailing user turn is by definition an orphan:
    # drop it rather than re-ask a question that is old news by now.
    if history and history[-1]["role"] == "user":
        history.pop()

    return history[-MAX_HISTORY:]


def save_turn(db: Any, uid: str, gid: str | None, user_msg: str, ai_text: str,
              reasoning: str = "", tool_chain: str = "",
              handled: bool = False) -> None:
    """Persist one conversation turn.

    Both halves are written together, or neither is. A lone user row is not a
    harmless omission: it stays in the context as a question nobody ever
    answered.

    Self-contained tools (web_search, tarot, hitokoto…) send their own reply
    and leave `ai_text` empty, which is why `handled` distinguishes "a tool
    already answered this" from "produced nothing at all". The first keeps a
    marker so the turn is visibly closed; the second is dropped entirely,
    because there is no turn worth remembering.

    The assistant row carries this turn's thinking chain and tool chain, so
    the user can later ask "what were you thinking" about this reply (and the
    dashboard's conversation log can show the chain).
    """
    if not (ai_text and ai_text.strip()) and not handled:
        return
    try:
        db.deposit_chat_history("user", uid, gid, user_msg, "", "")
        db.deposit_chat_history("assistant", uid, gid, ai_text or "",
                                tool_chain, "", reasoning)
    except Exception:
        logger.debug("chat_history.save_turn 忽略了异常", exc_info=True)
