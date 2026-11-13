from __future__ import annotations

import logging
import os
import re
import tempfile
import time
from typing import Any
from urllib.parse import quote

from config import Config
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


# 产物暂存目录（与 llbot 容器共享）。测试可通过 monkeypatch 替换。
SHARED_FILES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shared_files")

# 密钥/凭据/数据库：既不能从群里发出去，也不能经 /download 直链拿走。
_SENSITIVE_SUBSTRINGS = (
    ".env", "id_rsa", "id_ed25519", ".pem", ".key",
    "password", "secret", "token", "credential", ".git", "robot.db",
)
_SENSITIVE_SUFFIXES = (".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3", ".db.bak")


def is_sensitive_file(path: str) -> bool:
    """判断文件是否属于绝不允许外发的敏感数据（密钥/凭据/数据库）。"""
    name = os.path.basename(str(path or "")).lower()
    if not name:
        return False
    if name.endswith(_SENSITIVE_SUFFIXES) or "robot.db" in name:
        return True
    full = os.path.abspath(str(path)).lower()
    return any(
        p in name or f"/{p}" in full or f"_{p}" in full
        for p in _SENSITIVE_SUBSTRINGS
    )


def _allowed_source_roots() -> list[str]:
    """允许作为 send_file 来源的目录：共享目录、DSH 工作区、系统临时目录。"""
    roots = [os.path.realpath(SHARED_FILES_DIR)]
    for extra in (getattr(Config, "DSH_WORKSPACE_DIR", ""), tempfile.gettempdir()):
        if extra:
            roots.append(os.path.realpath(str(extra)))
    return roots


def _within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([os.path.realpath(path), os.path.realpath(root)]) == os.path.realpath(root)
    except ValueError:
        return False


def _safe_display_name(display_name: str, fallback: str) -> str:
    """只保留文件名本身：模型给的 display_name 可能带 ../ 或绝对路径。"""
    raw = str(display_name or "").strip().replace("\\", "/")
    name = os.path.basename(raw) if raw else ""
    if not name or name in {".", ".."} or "\x00" in name:
        name = os.path.basename(fallback)
    return name


# 这些词开头的句子通常依赖前文，单独发出去会改变语气甚至语义。
_DEPENDENT_SENTENCE_PREFIXES = (
    "但是", "不过", "可是", "然而", "而且", "并且", "所以", "因此", "于是",
    "因为", "如果", "要是", "也就是说", "换句话说", "毕竟", "反而", "其实",
    "同时", "另外", "然后", "或者", "否则", "尤其", "甚至", "只是", "除非",
)

_OPEN_TO_CLOSE = {"（": "）", "(": ")", "[": "]", "【": "】", "“": "”", "‘": "’", "《": "》"}
_CLOSERS = set(_OPEN_TO_CLOSE.values())
_SENTENCE_END = set("。！？!?~…")

# 换行当候选边界时，行尾出现这些标点说明这一行没说完，不能单独成段。
_LINE_SOFT_END = set("，,；;：:、") | set(_OPEN_TO_CLOSE.keys())
_LINE_MIN_LEN = 8
_CLOSER_CHARS = "".join(sorted(_CLOSERS))


def _line_is_boundary(line: str) -> bool:
    """行长够、且（去掉尾部闭引号/括号后）不是以「没说完」的标点收尾。"""
    if len(line) < _LINE_MIN_LEN:
        return False
    tail = line.rstrip(_CLOSER_CHARS)
    return bool(tail) and tail[-1] not in _LINE_SOFT_END


def _safe_sentence_split(text: str) -> list[str]:
    """只在确定的句末边界切分，并保护引号、括号、代码和 URL。

    这是刻意保守的扫描器：边界有疑问就不切。语义完整优先于多发几条的拟人效果。
    """
    sentences: list[str] = []
    start = 0
    stack: list[str] = []
    in_inline_code = False
    i = 0

    while i < len(text):
        ch = text[i]
        if ch == "`":
            in_inline_code = not in_inline_code
            i += 1
            continue
        if in_inline_code:
            i += 1
            continue

        if ch in _OPEN_TO_CLOSE:
            stack.append(_OPEN_TO_CLOSE[ch])
        elif ch in _CLOSERS and stack and ch == stack[-1]:
            stack.pop()

        if ch in _SENTENCE_END and not stack:
            # 省略号连续出现时只在最后一个点后切。
            if ch == "…" and i + 1 < len(text) and text[i + 1] == "…":
                i += 1
                continue
            # URL / 版本号 / 小数里的英文句点不在句末集合中，因此天然不误切。
            end = i + 1
            # 把句末后的闭引号一起留在当前句，避免下一条以孤立引号开头。
            while end < len(text) and text[end] in "”’）》】]）)":
                end += 1
            sentence = text[start:end].strip()
            if sentence:
                sentences.append(sentence)
            start = end
            i = end
            continue
        i += 1

    tail = text[start:].strip()
    if tail:
        sentences.append(tail)
    return sentences


def _join_sentences(sentences: list[str]) -> str:
    """合并完整句子；中文句末直接相邻，其他情况用空格保持可读性。"""
    out = ""
    for sentence in sentences:
        if not out:
            out = sentence
        elif out[-1] in "。！？!?~…”’）》】]）)" or sentence[0] in "“‘《（(【[":
            out += sentence
        else:
            out += " " + sentence
    return out


def _group_complete_sentences(sentences: list[str], max_chunks: int) -> list[str]:
    """把完整句子均衡分组到最多 max_chunks 条，任何内容都不会被丢弃。"""
    if len(sentences) <= max_chunks:
        return sentences

    groups: list[list[str]] = [[s] for s in sentences]

    # 先把依赖前文的句子粘回上一组。
    i = 1
    while i < len(groups):
        first = groups[i][0].lstrip("“‘（(")
        if first.startswith(_DEPENDENT_SENTENCE_PREFIXES):
            groups[i - 1].extend(groups.pop(i))
        else:
            i += 1

    # 仍超限时，每次合并相邻总长度最短的一对；只合并完整句子，不在句内动刀。
    while len(groups) > max_chunks:
        pair_index = min(
            range(len(groups) - 1),
            key=lambda idx: len(_join_sentences(groups[idx])) + len(_join_sentences(groups[idx + 1])),
        )
        groups[pair_index].extend(groups.pop(pair_index + 1))

    return [_join_sentences(group) for group in groups]


def _merge_shortest_lines(lines: list[str], max_chunks: int) -> list[str]:
    """行数超过 max_chunks 时，反复合并总长度最短的相邻两行；内容零丢弃。"""
    chunks = list(lines)
    while len(chunks) > max_chunks:
        index = min(
            range(len(chunks) - 1),
            key=lambda i: len(chunks[i]) + len(chunks[i + 1]),
        )
        chunks[index:index + 2] = [chunks[index] + "\n" + chunks[index + 1]]
    return chunks


def split_into_pacing_chunks(text: str, max_chunks: int = 3) -> list[str]:
    """把回复安全地拆成 1~max_chunks 条；语义完整永远优先于拟人分批。

    原则：
    - 代码、Markdown 结构、表格和 URL 密集文本整段发送；
    - 换行在「行长 ≥8 且行尾不是逗号/分号/冒号/顿号/开括号」时视作候选边界；
      超过 max_chunks 就把最短的相邻行合并，不丢内容；
    - 只按不在引号/括号内的句末标点切分；
    - 绝不按字符数切开完整句子，绝不因 max_chunks 丢弃尾部内容；
    - 「但是/所以/因为……」等依赖前文的句子优先与上一句合并。
    """
    text = (text or "").strip()
    if not text:
        return []
    if max_chunks <= 1:
        return [text]

    # 结构化内容和 URL 密集内容宁可一条发完，避免排版或链接被拆坏。
    if "```" in text or re.search(r"(?m)^\s*(?:[-+*]|\d+[.)])\s+", text) or "|" in text:
        return [text]
    if len(re.findall(r"https?://", text)) >= 2:
        return [text]

    if len(text) <= 35:
        return [text]

    # 模型现在常用换行断句（不一定写句末标点）。行长够、行尾不是「没说完」
    # 的标点，就把换行当成候选边界；超过 max_chunks 时合并最短相邻行，不丢内容。
    lines = [part.strip() for part in text.splitlines() if part.strip()]
    if len(lines) > 1 and all(_line_is_boundary(line) for line in lines):
        return _merge_shortest_lines(lines, max_chunks)

    # 兼容旧行为：每行都带句末标点时按行发送（行可以短）。
    if 1 < len(lines) <= max_chunks and all(line[-1] in _SENTENCE_END | _CLOSERS for line in lines):
        return lines

    # 普通换行可能只是排版；归一为空格后再按可靠句末边界扫描。
    normalized = re.sub(r"\s*\n\s*", " ", text)
    sentences = _safe_sentence_split(normalized)
    if len(sentences) <= 1:
        return [text]
    return _group_complete_sentences(sentences, max_chunks)


class RobotServer:
    """Thin wrapper: parses incoming messages and delegates sending to LLBotClient."""

    def __init__(self, msg_data: dict[str, Any], llbot: LLBotClient, bot_qq: str) -> None:
        self.llbot = llbot
        self.incoming = IncomingMessage.from_onebot(msg_data, bot_qq)
        self.text: str = ""
        self.image_path: str = ""
        # send_file 的直链基地址：构造函数在 webhook 请求上下文里被调用，
        # 此时缓存 Host 供后台线程使用（worker 线程里没有 Flask 请求上下文）。
        self.public_base_url = ""
        try:
            from flask import has_request_context, request as flask_request
            if has_request_context():
                self.public_base_url = flask_request.host_url.rstrip("/")
        except Exception:
            pass

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
        """发送本地文件到群聊或私聊（白名单目录 + 敏感文件黑名单 + 共享目录暂存）。"""
        import shutil

        if not self._allowed_to_send():
            return False

        if not file_path or not os.path.isfile(file_path):
            logger.warning("send_file 失败：文件不存在 %r", file_path)
            return False

        # 1) 敏感文件（密钥/凭据/数据库）一律拒绝，源路径和显示名都查。
        if is_sensitive_file(file_path):
            logger.error("send_file 触发安全拦截：禁止发送敏感文件 %r", file_path)
            return False

        # 2) 来源必须在白名单目录里：模型可以编造任意 file_path，
        #    不能让它把 /app 下的文件复制进共享目录再发直链。
        src = os.path.realpath(file_path)
        if not any(_within(src, root) for root in _allowed_source_roots()):
            logger.error("send_file 拒绝白名单目录外的文件：%r", file_path)
            return False

        # 3) display_name 只取 basename，落点必须仍在 shared_files 内。
        name = _safe_display_name(display_name, file_path)
        if is_sensitive_file(name):
            logger.error("send_file 触发安全拦截：禁止使用敏感文件名 %r", name)
            return False

        shared_dir = os.path.realpath(SHARED_FILES_DIR)
        os.makedirs(shared_dir, exist_ok=True)
        target_path = os.path.realpath(os.path.join(shared_dir, name))
        if os.path.dirname(target_path) != shared_dir:
            logger.error("send_file 拒绝越界的落点：%r", target_path)
            return False

        if src != target_path:
            try:
                shutil.copyfile(src, target_path)
            except Exception:
                logger.exception("send_file 复制文件到共享目录失败")
                return False

        # Docker 容器中共享挂载的绝对路径是 /app/shared_files/xxx
        container_shared_path = f"/app/shared_files/{name}"
        effective_path = container_shared_path if os.path.exists("/app/shared_files") else target_path

        # 4) 图片文件直接发气泡图片，体验远好于冷冰冰的群文件且不被富媒体风控拦截
        img_exts = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
        if name.lower().endswith(img_exts):
            logger.info("send_file 识别为图片类型，走图片气泡消息发送: %s", effective_path)
            return self.llbot.reply_image(self.incoming, effective_path)

        # 5) 非图片：直链 host 来自配置/请求，文件名必须 URL 编码。
        base_url = (
            Config.PUBLIC_BASE_URL
            or getattr(self, "public_base_url", "")
            or "http://localhost:5000"
        ).rstrip("/")
        download_url = f"{base_url}/download/{quote(name)}"
        msg_text = (
            f"📎 产物文件「{name}」已生成！\n"
            f"若客户端未弹出文件下载，可直接在浏览器打开链接下载：\n{download_url}"
        )

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
            return True  # 已提供直链，依然认为流程成功

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
