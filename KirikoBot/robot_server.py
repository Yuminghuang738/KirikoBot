from __future__ import annotations

import logging
import re
import time
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


def split_into_pacing_chunks(text: str, max_chunks: int = 3) -> list[str]:
    """把一段回复拆解为多个自然的短句，模仿正常人类连发多条消息的打字节奏。

    保护规则：
    - Markdown 代码块（```）、列表、表格绝不切分，保持排版与代码完整。
    - 短文本（<= 35 字符）保持单条发完，不画蛇添足。
    - 优先按照自然换行切分段落。
    - 长单句按句末标点（。！？!?~…）切分，合并为最多 max_chunks 段。
    """
    text = (text or "").strip()
    if not text:
        return []
    # 保护结构化内容
    if "```" in text or "\n-" in text or "\n1." in text or "|" in text:
        return [text]

    # 1. 优先按换行符切分（如果是多段/换行回答）
    lines = [p.strip() for p in text.split("\n") if p.strip()]
    if 1 < len(lines) <= max_chunks:
        if all(len(line) >= 3 for line in lines):
            return lines

    # 短文本单行直接一条发完
    if len(text) <= 35:
        return [text]

    # 2. 长单行文本按句子标点拆分
    sentences = re.split(r"(?<=[。！？!?~…])\s*", text)
    sentences = [s.strip() for s in sentences if s.strip()]
    if len(sentences) <= 1:
        return [text]

    chunks: list[str] = []
    curr = ""
    for s in sentences:
        if not curr:
            curr = s
        elif len(curr) + len(s) < 45 and len(chunks) + 1 < max_chunks:
            curr += " " + s
        else:
            chunks.append(curr)
            curr = s
    if curr:
        chunks.append(curr)
    return chunks[:max_chunks]


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
        # 同一回合只要入口通过了一次放行，后续的分段短句与伴随文件/图片全部放行，
        # 绝不让同一轮产生的图文被限流器腰斩或吞掉
        if getattr(self, "_turn_authorized", False):
            return True
        if _rate_limiter is None:
            self._turn_authorized = True
            return True
        try:
            ok = _rate_limiter.allow(self.group_id or "", self.user_id)
            if ok:
                self._turn_authorized = True
            return ok
        except Exception:
            # 限流自己出问题不该把聊天堵死
            logger.debug("限流判定异常，放行", exc_info=True)
            self._turn_authorized = True
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

    def reply_chunked(self, text: str) -> bool:
        """分段式拟人发送：模仿人类打字习惯，将长回复切成 1~3 个短句按顺序发送。

        - 第一条用 reply（在群里带 @引用 明确指向原问题）。
        - 后续短句用 send_text（纯文字紧随其后，不重复 @ 和引用，体验自然）。
        - 单轮判定一次限流额度，避免后续短句被误杀。
        """
        if not self._allowed_to_send():
            return False

        chunks = split_into_pacing_chunks(text)
        if not chunks:
            return False

        # 第一句带引用/@回复
        ok = self.llbot.reply_to(self.incoming, chunks[0])
        if not ok or len(chunks) == 1:
            return ok

        # 随后的句子分批发送，带有自然的打字间隔
        for prev_chunk, next_chunk in zip(chunks[:-1], chunks[1:]):
            delay = min(1.2, max(0.4, len(prev_chunk) * 0.03))
            time.sleep(delay)
            self.llbot.send_text(self.incoming, next_chunk)

        return True

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

    def send_file(self, file_path: str, display_name: str = "") -> bool:
        """发送本地文件到群聊或私聊（带安全黑名单与容器共享目录暂存）。"""
        import os
        import shutil

        if not self._allowed_to_send():
            return False

        if not file_path or not os.path.isfile(file_path):
            logger.warning("send_file 失败：文件不存在 %r", file_path)
            return False

        # 安全沙盒拦截：严禁泄露密钥、配置、密码与凭据
        fname = os.path.basename(file_path).lower()
        full_lower = os.path.abspath(file_path).lower()
        sensitive_patterns = (
            ".env", "id_rsa", "id_ed25519", ".pem", ".key",
            "password", "secret", "token", "credential", ".git",
        )
        if any(p in fname or f"/{p}" in full_lower or f"_{p}" in full_lower for p in sensitive_patterns):
            logger.error("send_file 触发安全拦截：禁止发送敏感文件 %r", file_path)
            return False

        name = display_name or os.path.basename(file_path)

        # 确保文件位于与 llbot 容器共享的目录下
        shared_dir = os.path.abspath(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "shared_files")
        )
        os.makedirs(shared_dir, exist_ok=True)

        target_path = file_path
        # 若当前文件不在 shared_files，拷贝一份过去确保 llbot 容器可见
        if not os.path.abspath(file_path).startswith(shared_dir):
            target_path = os.path.join(shared_dir, name)
            try:
                shutil.copyfile(file_path, target_path)
            except Exception:
                logger.exception("send_file 复制文件到共享目录失败")
                return False

        # Docker 容器中共享挂载的绝对路径是 /app/shared_files/xxx
        container_shared_path = f"/app/shared_files/{name}"
        effective_path = container_shared_path if os.path.exists("/app/shared_files") else target_path

        # 智能区分：图片文件直接发气泡图片，体验远好于冷冰冰的群文件且不被富媒体风控拦截
        img_exts = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
        if fname.endswith(img_exts):
            logger.info("send_file 识别为图片类型，走图片气泡消息发送: %s", effective_path)
            return self.llbot.reply_image(self.incoming, effective_path)

        # 非图片二进制文件：始终发送直链，上传作为后台尝试
        download_url = f"http://100.93.52.78:5000/download/{name}"
        msg_text = f"📎 产物文件「{name}」已生成！\n若客户端未弹出文件下载，可直接在浏览器打开链接下载：\n{download_url}"
        
        # 优先发送文字带直链（保障 100% 可见）
        self.llbot.reply_to(self.incoming, msg_text)
        
        # 后台静默尝试上传，成功即上传，失败不报错，确保对话流程不受风控影响
        try:
            if self.msg_type == "group":
                return self.llbot.upload_group_file(self.group_id or "", effective_path, name)
            else:
                return self.llbot.upload_private_file(self.user_id, effective_path, name)
        except Exception as e:
            logger.info("文件静默上传失败（已提供直链，忽略此错误）: %s", e)
            return True # 已提供直链，依然认为流程成功

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
