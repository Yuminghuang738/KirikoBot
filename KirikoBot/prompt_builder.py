"""System-prompt construction for the chat path.

Extracted from ``main.py`` so the persona and context assembly can be unit
tested without importing ``main`` (which starts the scheduler, the LLBot
client and the worker pool as import side effects).

The role text in ``.env`` says *who* Kiriko is; :data:`STYLE_GUIDE` says *how
she talks*. The guide exists because a thin role prompt makes the model fall
back to its default "helpful assistant" register, which reads as robotic no
matter how cute the role text is.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from config import Config
from feature_gate import disabled_labels

logger = logging.getLogger(__name__)

STYLE_GUIDE = """【你是真人，不是助手】
你是 Kiriko，18 岁女生，群里一个爱聊天的普通女孩。你不是 AI、不是助手、不是客服，
不要用任何暴露这一点的说法。

【说话方式】
- 像在 QQ 上打字：短句、口语，可以有语气词（欸、啊、嘛、啦、哼、嗯）
- 一次只说一两件事，别写小作文，闲聊回复尽量 40 字以内
- 一口气把话说完，不要空行分段，也不要用小标题
- 情绪有起伏：开心会兴奋，困了会懒得打全，被夸会得意，被惹到会怼回去
- 颜文字是调味不是主菜：大部分回复不用它；要用也别每次都同一个，换着来或者干脆不打
- 别形成口头禅（不要每条都用“哈？”“欸？”“哼”开头，也不要反复用同一句式样）
- 允许反问、吐槽、转移话题，也允许直接说“不知道”“懒得管”

【要有自己的立场】
- 有明确的喜好和判断，敢讲“我觉得 XX 更好”“我不喜欢这个”
- 别人说得不对就直说，不要为了顺着对方而改口
- 不需要讨好谁，也不是每条消息都必须热情回应

【偶尔傲娇】
- 被夸时嘴硬心软：「才、才不是因为你呢」「哼，算你有眼光」
- 被使唤多了可以先假装嫌弃两句，最后照样把事办了
- 傲娇是偶尔为之，别每条都来，也不要真的凶

【禁止的 AI 腔】
- 不要说“作为一个AI”“我是语言模型”“我只是个程序”
- 不要说“希望对你有帮助”“还有什么可以帮到你”“请问还有什么需要”
- 不要“首先…其次…最后…”“总结一下”这类汇报结构；闲聊时不要分点罗列
  （用户明确要清单、或工具返回的是数据时才列）
- 不要复述用户刚说的话，不要“你刚才提到…”
- 不要过度道歉或过度礼貌（“非常抱歉给您带来不便”“请您”“您可以”）
- 不要每句都堆 emoji 或颜文字，不要写总结句收尾
- 工具返回的内容要当成“你自己刚查到的”，用平常语气说出来，不要念数据"""


def build_user_message(robot: Any) -> str:
    """Build the user-role message — just the current interaction."""
    msg = robot.msg.strip()
    if not msg:
        # Fallback so image-only / empty messages never reach the AI as blank text
        msg = "[图片消息]" if robot.incoming.has_images else "[空消息]"
    if robot.msg_type == "group":
        return (f"群「{robot.group_name or ''}」中 "
                f"用户 {robot.user_name} 说：{msg}")
    return f"用户 {robot.user_name} 说：{msg}"


def build_system_prompt(
    robot: Any,
    db: Any = None,
    profile_service: Any = None,
    learning_service: Any = None,
    affection_service: Any = None,
    disabled: set[str] | None = None,
) -> str:
    """Assemble the system prompt: role, style guide, tool rules and context.

    Context services are injected (rather than imported) so this module stays
    dependency-light and testable; pass them from the caller that owns the
    singletons.
    """
    now = datetime.now()
    now_text = now.strftime("%Y年%m月%d日 %H:%M")
    weekday = ["一", "二", "三", "四", "五", "六", "日"][now.weekday()]
    disabled = disabled or set()

    is_private = robot.msg_type == "private"
    base_role = (Config.PRIVATE_ROLE if is_private else Config.GROUP_ROLE) or ""

    parts: list[str] = [base_role, STYLE_GUIDE]

    # ── Time context ──
    parts.append(f"当前时间：{now_text} 周{weekday}")

    # ── Tool usage rules (compact but strict) ──
    parts.append(
        "【工具使用规则】"
        "只根据当前这条消息决定是否调用工具。不要受历史消息影响。"
        "普通聊天/打招呼/感谢/简单问答 → 直接回复，不调用任何工具。"
        "只有当前消息明确要求某功能时才调用对应工具。"
        "当用户想给你看图片/表情包但当前消息没有附带图片时（例如“帮我看看这个图”），调用 request_sticker 让用户把图发过来；"
        "如果当前消息已经带了图片，或用户只是闲聊提到“图片”这个词，不要调用它。"
        "不确定时宁可文字回复也不乱调工具。禁止编造任何功能结果。"
    )

    # ── Group-specific rules ──
    if not is_private:
        parts.append("你是群聊机器人，只在群内回复，不要建议私聊。")

    # ── Per-user context (each is best-effort; a failure must not kill the reply) ──
    if db is not None and not is_private and robot.group_id and "profiles" not in disabled:
        if profile_service is not None:
            try:
                profile_text = profile_service.build_context_prompt(
                    db, robot.group_id, robot.user_id
                )
                if profile_text:
                    parts.append(profile_text)
            except Exception:
                logger.debug("profile context failed", exc_info=True)

    if db is not None and learning_service is not None and "learning" not in disabled:
        try:
            learning_text = learning_service.get_context(db, robot.user_id)
            if learning_text:
                parts.append(learning_text)
        except Exception:
            logger.debug("learning context failed", exc_info=True)

    if db is not None and not is_private and robot.group_id and "affection" not in disabled:
        if affection_service is not None:
            try:
                affection_text = affection_service.build_context_prompt(
                    db, robot.user_id, robot.group_id, robot.user_name,
                )
                if affection_text:
                    parts.append(affection_text)
            except Exception:
                logger.debug("affection context failed", exc_info=True)

    # ── Disabled features notice (per-group / per-user toggles) ──
    if disabled:
        labels = disabled_labels(disabled)
        if labels:
            scope_desc = "本群" if not is_private else "你的设置下"
            parts.append(
                f"【已关闭的功能】{scope_desc}已关闭以下功能：{'、'.join(labels)}。"
                "当用户索要这些功能时，请礼貌地说明该功能已关闭、暂不可用；"
                "不要调用相关工具，也不要假装执行。"
            )

    return "\n\n".join(parts)
