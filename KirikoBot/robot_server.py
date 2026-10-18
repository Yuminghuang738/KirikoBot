from __future__ import annotations

import logging
from typing import Any

from llbot_client import IncomingMessage, LLBotClient, MessageBuilder

logger = logging.getLogger(__name__)

# 回复频率限制器。由 main.py 在启动时装进来（见 install_rate_limiter）；
# 没装就是「不限流」，测试和脚本里不需要关心它。
_rate_limiter: Any = None


def install_rate_limiter(limiter: Any) -> None:
    """启动时装一次。"""
    global _rate_limiter
    _rate_limiter = limiter


def get_rate_limiter() -> Any:
    return _rate_limiter


class RobotServer:
    """Thin wrapper: parses incoming messages and delegates sending to LLBotClient."""

    def __init__(self, msg_data: dict[str, Any], llbot: LLBotClient, bot_qq: str) -> None:
        self.llbot = llbot
        self.incoming = IncomingMessage.from_onebot(msg_data, bot_qq)
        self.text: str = ""
        self.image_path: str = ""

        logger.info(
            "Message: type=%s user=%s(%s) group=%s",
            self.incoming.msg_type, self.incoming.user_name,
            self.incoming.user_id, self.incoming.group_id,
        )

    # Proxy common attributes from IncomingMessage
    @property
    def msg_type(self) -> str:
        return self.incoming.msg_type

    @property
    def user_id(self) -> str:
        return self.incoming.user_id

    @property
    def group_id(self) -> str | None:
        return self.incoming.group_id

    @property
    def group_name(self) -> str | None:
        return self.incoming.group_name

    @property
    def user_name(self) -> str:
        return self.incoming.user_name

    @property
    def user_role(self) -> str | None:
        return self.incoming.user_role

    @property
    def user_level(self) -> str | None:
        return self.incoming.user_level

    @property
    def user_title(self) -> str | None:
        return self.incoming.user_title

    @property
    def msg(self) -> str:
        return self.incoming.text

    @property
    def at_judgement(self) -> bool:
        return self.incoming.is_at_bot

    # ── Sending ──────────────────────────────────────────
    #
    # 所有出站消息都从这里过一道频率限制（群聊 / 单用户两种模式，见
    # rate_limiter.py）。被限住时返回 False 且**不发任何东西** —— 静默丢弃，
    # 因为「你太频繁了」那条提示本身也会占额度、还会把屏幕刷得更乱。
    #
    # 注意：定时推送（scheduler.py）直接走 llbot，不经过这里 —— 那不是「回复」。

    def _allowed_to_send(self) -> bool:
        if _rate_limiter is None:
            return True
        try:
            return _rate_limiter.allow(self.group_id or "", self.user_id)
        except Exception:
            # 限流自己出问题不该把聊天堵死
            logger.debug("限流判定异常，放行", exc_info=True)
            return True

    def send(self, message: list[dict[str, Any]]) -> bool:
        """发一条**已经构建好**的消息（群或私聊按 msg_type 自动判断）。

        工具里原来分散着十几处
            if robot.msg_type == "group": robot.llbot.send_group_msg(...)
            else:                          robot.llbot.send_private_msg(...)
        统一收到这里，既少一层重复，也保证都能过限流。
        """
        if not self._allowed_to_send():
            return False
        if self.msg_type == "group":
            return self.llbot.send_group_msg(self.group_id or "", message)
        return self.llbot.send_private_msg(self.user_id, message)

    def reply(self, text: str) -> bool:
        """Reply to incoming message. Groups: reply+@user+text. Private: reply+text."""
        if not self._allowed_to_send():
            return False
        return self.llbot.reply_to(self.incoming, text)

    def send_text(self, text: str) -> bool:
        """Send plain text without @ prefix."""
        if not self._allowed_to_send():
            return False
        return self.llbot.send_text(self.incoming, text)

    def reply_image(self, path: str) -> bool:
        """Send image as reply."""
        if not self._allowed_to_send():
            return False
        return self.llbot.reply_image(self.incoming, path)

    def send_group(self) -> bool:
        """Compat: send via msg_list attr (used by old MsgPackage flow)."""
        if not hasattr(self, "msg_list") or not self.msg_list:
            return False
        if self.msg_type == "group":
            return self.llbot.send_group_msg(self.group_id or "", self.msg_list)
        else:
            return self.llbot.send_private_msg(self.user_id, self.msg_list)

    def send_private(self) -> bool:
        """Compat: send private via msg_list."""
        if not hasattr(self, "msg_list") or not self.msg_list:
            return False
        return self.llbot.send_private_msg(self.user_id, self.msg_list)
