"""DSH 智能体自治交付工具 (DSH Autonomous Deliverer).

供 DSH (DeepSeek Harness) 智能体在完成任务、生成文件或分阶段汇报时自主调用，
将最终文字说明与产物文件直接推送到指定的 QQ 群或私聊会话中。

用法示例：
    python3 /app/dsh_deliver.py --channel group:734954188 --text "这是分析报告" --file /tmp/report.pdf
    python3 /app/dsh_deliver.py --channel private:3234979817 --file /tmp/cover.jpg
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import re
from typing import Any
from database_manager import DatabaseManager
from llbot_client import IncomingMessage, LLBotClient
from robot_server import RobotServer, split_into_pacing_chunks
from config import Config

logger = logging.getLogger("dsh_deliver")

# Markdown 语法符号：加粗/斜体/代码/标题/引用/链接括号。
# QQ 气泡不渲染 Markdown，这些字符留着只会让阅读变脏。
_MD_CHARS = re.compile(r"[*#_`~>\[\]]")
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
# 行首的列表/标题符号：`- `、`+ `、`* `、`1. `、`1) `。
_MD_LINE_PREFIX = re.compile(r"^[ \t]*(?:[-+*]|\d+[.)])[ \t]+", re.MULTILINE)


def clean_markdown(text: str) -> str:
    """把 Markdown 片段压成适合 QQ 气泡显示的纯文本。

    顺序有讲究：先去掉图片（连文字一起丢），链接降级成它的文字，
    再剥行首列表符号和剩下的语法符号，最后把换行与连续空白折成单空格 ——
    分段发送时换行已经被 split_into_pacing_chunks 消费掉了。
    """
    out = _MD_IMAGE.sub("", text or "")
    out = _MD_LINK.sub(r"\1", out)
    out = _MD_LINE_PREFIX.sub("", out)
    out = _MD_CHARS.sub("", out)
    out = re.sub(r"\s*\n\s*", " ", out)
    return re.sub(r"[ \t]{2,}", " ", out).strip()


def deliver(
    channel: str,
    text: str = "",
    file_path: str = "",
    display_name: str = "",
    db: Any = None,
    llbot: Any = None,
) -> bool:
    """由 DSH 智能体主动调用的交付函数。"""
    channel = (channel or "").strip()
    print(f"[dsh_deliver] DEBUG: 接收到的 Channel 参数为: {channel!r}", file=sys.stderr)
    if not channel or ":" not in channel:
        print("[dsh_deliver] 错误：未指定合法的 --channel (格式如 group:123456 或 private:123456)", file=sys.stderr)
        return False

    msg_type, target_id = channel.split(":", 1)
    if msg_type not in ("group", "private") or not target_id:
        print(f"[dsh_deliver] 错误：不支持的渠道类型或目标ID为空: {channel}", file=sys.stderr)
        return False

    db = db or DatabaseManager()
    llbot = llbot or LLBotClient(Config.ONEBOT_API or "http://llbot:3000", Config.ONEBOT_TOKEN or "")

    # 构造虚拟的接收上下文以复用成熟的 RobotServer 发送逻辑
    raw_incoming = {
        "message_type": msg_type,
        "user_id": target_id if msg_type == "private" else 0,
        "group_id": target_id if msg_type == "group" else None,
        "sender": {"nickname": "DSH_Agent"},
        "message": [],
    }
    robot = RobotServer(raw_incoming, llbot, Config.ROBOT_QQ or "0")

    success = True

    # 1. 发送文字结果（带拟人分段）
    text = (text or "").strip()
    if text:
        chunks = split_into_pacing_chunks(text)
        for chunk in chunks:
            clean_chunk = clean_markdown(chunk)
            if not clean_chunk:
                continue
            # 群聊使用 send_group_msg，私聊使用 send_private_msg
            if msg_type == "group":
                llbot.send_group_msg(target_id, [{"type": "text", "data": {"text": clean_chunk}}])
            else:
                llbot.send_private_msg(target_id, [{"type": "text", "data": {"text": clean_chunk}}])

    # 2. 发送产物文件（智能分流：图片走气泡，非图片走直链+文件上传）
    file_path = (file_path or "").strip()
    if file_path:
        if not os.path.isfile(file_path):
            print(f"[dsh_deliver] 警告：指定的文件不存在: {file_path}", file=sys.stderr)
            success = False
        else:
            ok = robot.send_file(file_path, display_name=display_name)
            if ok:
                # 记录到会话与用户的最近产物，支持后续重发
                try:
                    db.set_state(f"last_artifact:{channel}", file_path)
                    if msg_type == "private":
                        db.set_state(f"last_artifact:user:{target_id}", file_path)
                except Exception:
                    pass
            else:
                success = False

    # 3. 标记本次自主交付已完成（防止外层重复发送）
    if success:
        try:
            import time
            db.set_state(f"dsh_delivered:{channel}", str(time.time()))
        except Exception:
            pass

    return success


def main() -> int:
    parser = argparse.ArgumentParser(description="DSH Autonomous Deliverer")
    parser.add_argument("--channel", required=True, help="目标渠道，格式如 group:734954188 或 private:3234979817")
    parser.add_argument("--text", default="", help="交付的文字说明与结果文本")
    parser.add_argument("--file", default="", help="交付的目标产物文件绝对路径")
    parser.add_argument("--name", default="", help="文件显示名称（可选）")

    args = parser.parse_args()
    ok = deliver(
        channel=args.channel,
        text=args.text,
        file_path=args.file,
        display_name=args.name,
    )
    if ok:
        print("[dsh_deliver] 交付成功！")
        return 0
    else:
        print("[dsh_deliver] 交付失败！", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
