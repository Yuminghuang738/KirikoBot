from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

import requests
from requests.adapters import HTTPAdapter, Retry

logger = logging.getLogger(__name__)


# ── Data models ──────────────────────────────────────────

def _as_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


@dataclass
class ReplyInfo:
    """The message a user is quoting, taken from LLBot's `reply` segment.

    LLBot (as deployed here) sends **only the id**: `{"type": "reply",
    "data": {"id": "75563830"}}` — no text, no sender, no segments. The other
    fields below are kept because some LLBot builds do populate them, but
    nothing may assume they are present: resolve the id against our own
    records (see DatabaseManager.find_quoted) before describing a quote.
    """
    message_seq: int | None = None
    sender_id: str = ""
    sender_name: str = ""
    text: str = ""
    time: int | None = None
    has_images: bool = False
    # Who the quoted message was addressed to, when it was the bot's own.
    # Filled in from our records (see DatabaseManager.find_quoted); LLBot
    # never sends it. Without it the bot cannot tell "B is quoting what I said
    # to A" from "A is quoting what I said to A".
    target_name: str = ""


@dataclass
class IncomingMessage:
    """Parsed incoming OneBot message."""
    raw: dict[str, Any]
    msg_type: str  # "group" or "private"
    user_id: str
    user_name: str
    group_id: str | None = None
    group_name: str | None = None
    user_role: str | None = None
    user_level: str | None = None
    user_title: str | None = None
    text: str = ""
    is_at_bot: bool = False
    message_id: int | None = None
    # The quoted message's **id**, despite the name. Verified against a live
    # LLBot: the reply segment is `{"type": "reply", "data": {"id": "75563830"}}`
    # and that value matches `bot_messages.message_id` / `group_messages.
    # message_id`, not the QQ sequence number. `_extract_reply` therefore
    # prefers `id`; see its docstring.
    message_seq: int | None = None
    reply: ReplyInfo | None = None

    @classmethod
    def from_onebot(cls, data: dict[str, Any], bot_qq: str) -> IncomingMessage:
        msg_type = data.get("message_type", "private")
        user_id = str(data.get("user_id", ""))
        group_id = data.get("group_id")
        if group_id is not None:
            group_id = str(group_id)
        sender = data.get("sender") or {}

        message_raw = data.get("message")
        if not isinstance(message_raw, list):
            message_raw = []

        text = cls._extract_text(message_raw)
        is_at = any(
            seg.get("type") == "at"
            and str(seg.get("data", {}).get("qq", "")) == bot_qq
            for seg in message_raw
        ) if msg_type == "group" else True  # always respond in private

        return cls(
            raw=data,
            msg_type=msg_type,
            user_id=user_id,
            user_name=sender.get("nickname", "unknown"),
            group_id=group_id,
            group_name=data.get("group_name"),
            user_role=sender.get("role"),
            user_level=sender.get("level"),
            user_title=sender.get("title"),
            text=text,
            is_at_bot=is_at,
            message_id=data.get("message_id"),
            message_seq=_as_int(data.get("message_seq")),
            reply=cls._extract_reply(message_raw),
        )

    @staticmethod
    def _extract_reply(segments: list[dict[str, Any]]) -> ReplyInfo | None:
        """Pull the quoted message out of a `reply` segment, if present."""
        for seg in segments:
            if not isinstance(seg, dict) or seg.get("type") != "reply":
                continue
            data = seg.get("data") or {}
            quoted = data.get("segments")
            if not isinstance(quoted, list):
                quoted = []

            # Prefer `id`. Both fields exist in some LLBot builds, but they
            # live in different number spaces: `id` is the message id (what our
            # tables are keyed on), `message_seq` is the QQ sequence number.
            # Taking `message_seq` first silently resolved to nothing.
            seq = data.get("id")
            if seq is None:
                seq = data.get("message_seq")
            try:
                seq = int(seq) if seq is not None else None
            except (TypeError, ValueError):
                seq = None

            # sender_id is a QQ number in some LLBot paths and a UID in others;
            # keep whatever we get and let the caller decide.
            sender_id = data.get("sender_id", data.get("qq", ""))
            return ReplyInfo(
                message_seq=seq,
                sender_id=str(sender_id or ""),
                sender_name=str(data.get("sender_name") or ""),
                text=IncomingMessage._extract_text(quoted) or str(data.get("text") or ""),
                time=data.get("time"),
                has_images=any(
                    isinstance(s, dict) and s.get("type") in ("image", "face", "mface")
                    for s in quoted
                ),
            )
        return None

    @staticmethod
    def _extract_text(segments: list[dict[str, Any]]) -> str:
        parts = []
        for seg in segments:
            try:
                if seg.get("type") == "text":
                    parts.append(seg.get("data", {}).get("text", ""))
            except (AttributeError, TypeError):
                continue
        return "".join(parts)

    @property
    def has_images(self) -> bool:
        """Whether this message contains any image segments."""
        message_raw = self.raw.get("message")
        if not isinstance(message_raw, list):
            return False
        return any(seg.get("type") == "image" for seg in message_raw)

    @property
    def image_urls(self) -> list[str]:
        """All image URLs/file paths in this message."""
        message_raw = self.raw.get("message")
        if not isinstance(message_raw, list):
            return []
        urls: list[str] = []
        for seg in message_raw:
            if seg.get("type") == "image":
                data = seg.get("data", {})
                url = data.get("url") or data.get("file", "")
                if url:
                    urls.append(url)
        return urls


# ── Message builder ──────────────────────────────────────

class MessageBuilder:
    """Fluent builder for OneBot message segments."""

    def __init__(self) -> None:
        self._segments: list[dict[str, Any]] = []

    def text(self, content: str) -> MessageBuilder:
        if content:
            self._segments.append({"type": "text", "data": {"text": content}})
        return self

    def at(self, qq: str) -> MessageBuilder:
        self._segments.append({"type": "at", "data": {"qq": qq}})
        return self

    def image(self, file_path: str) -> MessageBuilder:
        self._segments.append({"type": "image", "data": {"file": file_path}})
        return self

    def reply(self, message_id: int) -> MessageBuilder:
        self._segments.append({"type": "reply", "data": {"id": str(message_id)}})
        return self

    def face(self, face_id: int) -> MessageBuilder:
        self._segments.append({"type": "face", "data": {"id": str(face_id)}})
        return self

    def record(self, file_path: str) -> MessageBuilder:
        """Send a voice/audio message (OneBot record type)."""
        self._segments.append({"type": "record", "data": {"file": file_path}})
        return self

    def music(self, music_type: str, song_id: str) -> MessageBuilder:
        """Send a music share card (OneBot music type). QQ native music share UI."""
        self._segments.append({
            "type": "music",
            "data": {
                "type": music_type,  # "163" for Netease, "qq" for QQ Music
                "id": str(song_id),
            },
        })
        return self

    def build(self) -> list[dict[str, Any]]:
        return self._segments


# ── LLBot Client ─────────────────────────────────────────

class LLBotClient:
    """Unified client for LLBot / OneBot HTTP API.

    Extensible: add new API methods by calling _post(endpoint, payload).
    """

    def __init__(
        self,
        api_url: str,
        token: str,
        timeout: int = 15,
        max_retries: int = 2,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._session = self._create_session(max_retries)
        # Last few messages this bot sent. Needed to recognise "someone quoted
        # me" (LLBot's reply segment carries a seq/uid that isn't always the QQ
        # number) and as the basis for recalling our own messages.
        self._recent_sent: deque[dict[str, Any]] = deque(maxlen=200)
        # Optional persistence hook, wired to the DB by main.py. Kept as a
        # callback so this module stays free of database concerns.
        self._recorder: Callable[[str, int, str], None] | None = None

    def set_recorder(self, fn: Callable[[str, int, str], None] | None) -> None:
        """Register a sink for sent messages: fn(group_id, message_id, text)."""
        self._recorder = fn

    def recall(self, message_id: Any) -> bool:
        """Recall a message (OneBot delete_msg).

        QQ only allows recalling your own message for about two minutes, and
        needs group-admin rights to touch anyone else's — callers should check
        the age before getting here.
        """
        try:
            mid = int(message_id)
        except (TypeError, ValueError):
            return False
        return self._post("delete_msg", {"message_id": mid})

    def _create_session(self, max_retries: int) -> requests.Session:
        s = requests.Session()
        retry = Retry(
            total=max_retries,
            backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods={"POST", "GET"},
        )
        adapter = HTTPAdapter(max_retries=retry)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        s.headers.update({"Authorization": f"Bearer {self.token}"})
        return s

    def _post(self, endpoint: str, payload: dict[str, Any]) -> bool:
        """Low-level POST. Returns True on success."""
        url = f"{self.api_url}/{endpoint}"
        try:
            r = self._session.post(url, json=payload, timeout=self.timeout)
            r.raise_for_status()
            self._remember_sent(endpoint, payload, r)
            logger.debug("LLBot %s OK", endpoint)
            return True
        except requests.exceptions.Timeout:
            logger.error("LLBot %s timeout", endpoint)
        except requests.exceptions.ConnectionError:
            logger.exception("LLBot %s connection error", endpoint)
        except requests.exceptions.HTTPError:
            logger.error("LLBot %s HTTP %s: %s", endpoint,
                         r.status_code if 'r' in dir() else '?',
                         (r.text[:200] if 'r' in dir() and r.text else ''))
        except Exception:
            logger.exception("LLBot %s unexpected error", endpoint)
        return False

    def _remember_sent(self, endpoint: str, payload: dict[str, Any], response: Any) -> None:
        """Track the message_id of what we just sent.

        `_post` used to discard the response body, so the bot had no idea which
        messages were its own — which is what makes "someone quoted me" hard to
        detect and recall impossible.
        """
        if "send" not in endpoint:
            return
        try:
            data = response.json().get("data") or {}
        except (ValueError, AttributeError):
            return
        message_id = data.get("message_id")
        # `send_group_ai_record` always reports 0 while still delivering, so a
        # falsy id means "no usable id" — recording it would make recall try to
        # delete message 0 and make quotes of that message unresolvable.
        if not message_id:
            return

        text = ""
        target_user_id = ""
        for seg in payload.get("message") or []:
            if not isinstance(seg, dict):
                continue
            kind = seg.get("type")
            data = seg.get("data") or {}
            if kind == "text":
                text += str(data.get("text") or "")
            elif kind == "at":
                # Group replies carry `reply` + `at(user)` + text, so the `at`
                # segment is *who this reply was addressed to*. Recording it is
                # what lets the bot notice later that a different person is now
                # quoting a message it said to someone else.
                target_user_id = str(data.get("qq") or data.get("user_id") or "")
        text = text.strip()
        group_id = str(payload.get("group_id") or "")
        self._recent_sent.append({
            "message_id": message_id,
            "group_id": group_id,
            "user_id": str(payload.get("user_id") or ""),
            "text": text,
            "ts": time.time(),
        })
        if self._recorder and group_id:
            try:
                self._recorder(group_id, int(message_id), text, target_user_id)
            except Exception:
                logger.debug("sent-message recorder failed", exc_info=True)

    # Short replies like "好的"/"嗯" are not distinctive enough to identify a
    # quoted message by text — matching them would claim other people's
    # messages as our own.
    _MIN_TEXT_MATCH = 6

    def is_own_message(self, message_seq: Any = None, text: str | None = None) -> bool:
        """Whether a quoted message was sent by this bot.

        Matches on message id first, then falls back to an exact text match —
        LLBot reports the reply's sender as a UID in some code paths and a QQ
        number in others, so neither signal is dependable on its own. A wrong
        "not mine" is harmless (the quote is still described, just attributed
        to a member); a wrong "mine" is not, hence the strictness.
        """
        try:
            seq = int(message_seq) if message_seq is not None else None
        except (TypeError, ValueError):
            seq = None
        needle = " ".join((text or "").split())

        for item in self._recent_sent:
            if seq is not None and item["message_id"] == seq:
                return True
            sent = " ".join(item["text"].split())
            if len(needle) >= self._MIN_TEXT_MATCH and sent == needle:
                return True
        return False

    # ── Message sending ──────────────────────────────────

    def send_group_msg(
        self,
        group_id: str,
        message: list[dict[str, Any]] | MessageBuilder,
    ) -> bool:
        if isinstance(message, MessageBuilder):
            message = message.build()
        return self._post("send_group_msg", {
            "group_id": group_id,
            "message": message,
        })

    # ── AI voice (LLOneBot / NapCat extension) ───────────
    # QQ's own AI voice synthesis: the text is spoken by a chosen 音色. The
    # character ids are validated against get_ai_characters before use, because
    # the API accepts an unknown id and simply fails to deliver.
    VOICE_ENDPOINT = "send_group_ai_record"

    def get_ai_characters(self) -> list[dict[str, Any]]:
        """Available voices, flattened to [{id, name, category}]."""
        try:
            r = self._session.post(
                f"{self.api_url}/get_ai_characters", json={}, timeout=self.timeout)
            r.raise_for_status()
            data = (r.json() or {}).get("data")
        except Exception:
            logger.debug("get_ai_characters failed", exc_info=True)
            return []

        groups = data if isinstance(data, list) else (data or {}).get("characters") or []
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for group in groups:
            if not isinstance(group, dict):
                continue
            category = str(group.get("type") or group.get("name") or "")
            for c in group.get("characters") or []:
                if not isinstance(c, dict):
                    continue
                cid = str(c.get("character_id") or "")
                if not cid or cid in seen:
                    continue
                seen.add(cid)
                out.append({"id": cid, "name": str(c.get("character_name") or ""),
                            "category": category})
        return out

    def send_ai_voice(self, group_id: str, character: str, text: str) -> bool:
        """Speak `text` in the group using QQ's AI voice. Groups only."""
        if not text.strip():
            return False
        return self._post(self.VOICE_ENDPOINT, {
            "group_id": group_id,
            "character": character,
            "text": text,
        })

    def send_private_msg(
        self,
        user_id: str,
        message: list[dict[str, Any]] | MessageBuilder,
    ) -> bool:
        if isinstance(message, MessageBuilder):
            message = message.build()
        return self._post("send_private_msg", {
            "user_id": user_id,
            "message": message,
        })

    # ── Convenience methods ──────────────────────────────

    def reply_to(
        self,
        msg: IncomingMessage,
        text: str,
    ) -> bool:
        """Reply with text. In groups: @user + text. In private: just text."""
        builder = MessageBuilder()
        if msg.msg_type == "group":
            if msg.message_id:
                builder.reply(msg.message_id)
            builder.at(msg.user_id).text(f" {text}")
            return self.send_group_msg(msg.group_id or "", builder)
        else:
            if msg.message_id:
                builder.reply(msg.message_id)
            builder.text(text)
            return self.send_private_msg(msg.user_id, builder)

    def reply_image(self, msg: IncomingMessage, image_path: str) -> bool:
        """Send an image as reply."""
        builder = MessageBuilder()
        if msg.msg_type == "group":
            if msg.message_id:
                builder.reply(msg.message_id)
            builder.image(image_path)
            return self.send_group_msg(msg.group_id or "", builder)
        else:
            builder.image(image_path)
            return self.send_private_msg(msg.user_id, builder)

    def send_text(self, msg: IncomingMessage, text: str) -> bool:
        """Send text without reply/at prefix. Useful for second messages."""
        builder = MessageBuilder().text(text)
        if msg.msg_type == "group":
            return self.send_group_msg(msg.group_id or "", builder)
        else:
            return self.send_private_msg(msg.user_id, builder)

    # ── Group operations ─────────────────────────────────

    def get_group_info(self, group_id: str) -> dict[str, Any] | None:
        try:
            r = self._session.post(
                f"{self.api_url}/get_group_info",
                json={"group_id": group_id},
                timeout=self.timeout,
            )
            return r.json().get("data", {})
        except Exception:
            logger.exception("get_group_info failed")
            return None

    def get_group_member_info(
        self, group_id: str, user_id: str
    ) -> dict[str, Any] | None:
        try:
            r = self._session.post(
                f"{self.api_url}/get_group_member_info",
                json={"group_id": group_id, "user_id": user_id},
                timeout=self.timeout,
            )
            return r.json().get("data", {})
        except Exception:
            logger.exception("get_group_member_info failed")
            return None

    # ── Group member list ───────────────────────────────

    def get_group_member_list(self, group_id: str) -> list[dict[str, Any]]:
        """Get all members of a group. Returns list of {user_id, nickname, role, ...}."""
        try:
            r = self._session.post(
                f"{self.api_url}/get_group_member_list",
                json={"group_id": group_id},
                timeout=self.timeout,
            )
            r.raise_for_status()
            data = r.json().get("data", [])
            return data if isinstance(data, list) else []
        except Exception:
            logger.exception("get_group_member_list failed for %s", group_id)
            return []

    # ── Extensibility ────────────────────────────────────

    def call(self, endpoint: str, payload: dict[str, Any]) -> bool:
        """Call any OneBot API endpoint. Use for future extensions."""
        return self._post(endpoint, payload)
