from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from flask import Flask, Response, jsonify, render_template, request, send_from_directory

from ai_server import AiServer
from ai_tools import (
    Tarot, Tarot_History,
    WebSearchTool, StickerTool,
    AtMemberTool, ReminderTool, TimeTool,
    BalanceTool, MusicTool,
    ListRemindersTool, DeleteReminderTool,
    AffectionTool, AffectionLeaderboardTool,
    RecallMessageTool, GroupStatsTool, ReadContextTool,
    ExplainSelfTool, VoiceTool, DshAgentTool, SendFileTool,
)
from affection_service import AffectionService
from balance_service import BalanceService
from ai_tools_list import AiTools
from config import Config
import ai_metrics
import dashboard_auth
from chat_history import load_history, save_turn
import memory_service
import webhook_auth
from prompt_builder import (
    build_role_prompt,
    deflection_for,
    filler_for,
    format_group_context,
    leaked_persona,
    resolve_quote,
    build_system_prompt as _build_system_prompt,
    build_user_message as _context,
    describe_reply,
)
from database_manager import DatabaseManager
from feature_gate import (
    FEATURE_DEFS, FeatureGate, VALID_SCOPES,
    disabled_tool_names, disabled_labels,
)
from judge_service import JudgeService
from llbot_client import LLBotClient, MessageBuilder
from llbot_webui import llbot_bp
from msg_package import MsgPackage
from log_stream import sse_handler, setup_sse_logging
from music_service import MusicService
from profile_service import ProfileService, normalize_profile
import robot_server
from robot_server import RobotServer, is_sensitive_file
from rate_limiter import RateLimiter
from scheduler import BotScheduler
from sticker_collector import StickerCollector, STICKER_DIR, STICKER_CATEGORIES
from web_search import WebSearch


def _setup_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    if root.handlers:
        for x in root.handlers: root.removeHandler(x)
    root.addHandler(h)

_setup_logging()
setup_sse_logging()
logger = logging.getLogger(__name__)

# ── App ─────────────────────────────────────────────────
app = Flask(__name__)
# The dashboard shell is edited often; re-read templates from disk instead of
# caching them for the process lifetime (Flask's production default).
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True
# OneBot 事件体实测远小于 1 MB。上限必须在验签之前生效，否则未鉴权的超大
# 请求会先被整个读进内存（waitress 默认放到 1 GB），在 1.6 G 的机器上直接 OOM。
MAX_WEBHOOK_BODY = 1 * 1024 * 1024
app.config["MAX_CONTENT_LENGTH"] = MAX_WEBHOOK_BODY
app.register_blueprint(llbot_bp)
dashboard_auth.init_app(app)

# ── Sticker battle state (must be before services that reference it) ──
_battle_lock = threading.Lock()
BATTLE_TIMEOUT = 60  # seconds before battle auto-ends

# ── Services ────────────────────────────────────────────
llbot = LLBotClient(Config.ONEBOT_API or "http://llbot:3000", Config.ONEBOT_TOKEN or "")
db = DatabaseManager()

# 上个进程留下的 running DSH 行改 interrupted，否则成功率/耗时永远缺这些任务。
try:
    _stale_dsh = db.mark_stale_dsh_tasks_interrupted()
    if _stale_dsh:
        logger.info("启动时将 %d 条遗留 DSH running 任务标记为 interrupted", _stale_dsh)
except Exception:
    logger.debug("标记遗留 DSH 任务失败", exc_info=True)

# 回复频率限制：按群 / 按用户两个维度，配置存在 group_rate_limits 表里，
# 在群设置面板里改。装进 robot_server 之后，所有出站回复都会过它一道。
rate_limiter = RateLimiter(db)
robot_server.install_rate_limiter(rate_limiter)
feature_gate = FeatureGate(db)
pkg = MsgPackage()
tools_def = AiTools()

tarot = Tarot(db, pkg)
tarot_history = Tarot_History(db, pkg)
web_search = WebSearch()
web_search_tool = WebSearchTool(web_search, pkg)
sticker_tool = StickerTool(pkg)
at_member_tool = AtMemberTool(pkg, db, llbot)
reminder_tool = ReminderTool(db, pkg)
list_reminders_tool = ListRemindersTool(db, pkg)
delete_reminder_tool = DeleteReminderTool(db, pkg)
time_tool = TimeTool(pkg)
balance_service = BalanceService()
balance_tool = BalanceTool(balance_service, pkg)
music_service = MusicService()
music_tool = MusicTool(music_service, pkg)

# 群订阅推送已整体移除，调度器现在只管提醒和箱头库爬取
scheduler = BotScheduler(db, llbot, feature_gate)
scheduler.start()
sticker_collector = StickerCollector(db=db)
profile_service = ProfileService()
affection_service = AffectionService()
judge_service = JudgeService()
affection_tool = AffectionTool(pkg, db)
affection_leaderboard_tool = AffectionLeaderboardTool(pkg, db)
recall_tool = RecallMessageTool(db, llbot)
group_stats_tool = GroupStatsTool(db, pkg)
read_context_tool = ReadContextTool(db, pkg)
explain_self_tool = ExplainSelfTool(db, pkg)
voice_tool = VoiceTool(db, pkg, llbot)
dsh_agent_tool = DshAgentTool(db)
send_file_tool = SendFileTool(db)

# Persist the bot's own outgoing messages so transcripts are complete and
# "recall the last thing I said" works across restarts.
llbot.set_recorder(db.record_bot_message)

# Every DeepSeek call (chat / background jobs / vision) is recorded for the
# dashboard's usage page. Never on the critical path — metrics failures are
# swallowed inside ai_metrics.
if Config.AI_METRICS_ENABLED:
    ai_metrics.set_sink(db.record_ai_call)

# Security posture, stated once at boot so a weakened config is never silent.
if not Config.WEBHOOK_TOKEN:
    logger.warning(
        "WEBHOOK_TOKEN / ONEBOT_TOKEN 未配置：/webhook 不校验签名，"
        "任何能访问本端口的人都能伪造消息让机器人发言。"
    )
else:
    logger.info("Webhook 签名校验已启用（x-signature / HMAC-SHA1）")

# Dedicated logger for thinking chains — propagates to root (SSE + stdout)
think_log = logging.getLogger("think")

executor = ThreadPoolExecutor(max_workers=12)
sticker_collector.set_executor(executor)
_seeded_groups: set[str] = set()
_start_time = time.time()

# ── Tool routing ────────────────────────────────────────
ROUTES = {
    "tarot": tarot.tarot_call, "tarot_history": tarot_history.tarot_history_call,
    "web_search": web_search_tool.web_search_call,
    "sticker": sticker_tool.sticker_call,
    "at_member": at_member_tool.at_member_call, "set_reminder": reminder_tool.set_reminder_call,
    "list_reminders": list_reminders_tool.list_reminders_call,
    "delete_reminder": delete_reminder_tool.delete_reminder_call,
    "get_current_time": time_tool.get_current_time_call,
    "check_balance": balance_tool.balance_call,
    "music_search": music_tool.music_search_call,
    "check_affection": affection_tool.check_affection_call,
    "affection_leaderboard": affection_leaderboard_tool.affection_leaderboard_call,
    "recall_message": recall_tool.recall_message_call,
    "group_stats": group_stats_tool.group_stats_call,
    "read_context": read_context_tool.read_context_call,
    "explain_self": explain_self_tool.explain_self_call,
    "send_voice": voice_tool.voice_call,
    "dsh_agent": dsh_agent_tool.dsh_agent_call,
    "send_file": send_file_tool.send_file_call,
}

# Self-contained tools format and send their own reply — no AI follow-up needed
SELF_CONTAINED_TOOLS = {
    "tarot", "sticker", "web_search", "at_member",
    "tarot_history", "music_search",
    # 这几个是纯数据类回答（统计/余额/好感度），直接发原文比让模型复述更准
    "group_stats", "check_balance", "check_affection", "affection_leaderboard",
    # explain_self sends the raw debug dump itself; a follow-up turn would only
    # add the model's paraphrase on top of the text we want verbatim.
    "explain_self",
    # send_voice *is* the reply; a follow-up would add a typed duplicate.
    "send_voice",
    # dsh_agent autonomously executes deep agent tasks and sends its own reply
    "dsh_agent",
    # send_file uploads the file directly via OneBot API
    "send_file",
}

# ── History (only recent context, filtered for clarity) ──
# History storage/replay lives in chat_history.py so it can be unit-tested
# (importing main would start the scheduler). These stay as thin wrappers so
# call sites keep working against the process-wide db.
def _load_history(uid: str, gid: str | None) -> list[dict[str, Any]]:
    return load_history(db, uid, gid)

def _save_turn(uid: str, gid: str | None, user_msg: str, ai_text: str,
               reasoning: str = "", tool_chain: str = "",
               handled: bool = False) -> None:
    save_turn(db, uid, gid, user_msg, ai_text, reasoning, tool_chain, handled)


def _memory_context(robot: Any) -> tuple[list[dict[str, Any]], str, bool]:
    """本回合的 (history, summary, memory_on)。

    开关判断必须走 is_enabled：``disabled_keys`` 只认识「显式关闭」，
    对 FEATURE_DEFAULTS 里默认关的 memory_layers 会误判为开。
    """
    scope_type, scope_id = feature_gate.scope_of(robot)
    memory_on = bool(
        Config.MEMORY_LAYERS_ENABLED
        and feature_gate.is_enabled(scope_type, scope_id, "memory_layers")
    )
    if not memory_on:
        return _load_history(robot.user_id, robot.group_id), "", False
    try:
        history, summary = memory_service.load_context(
            db, robot.user_id, robot.group_id,
        )
        return history, summary, True
    except Exception:
        logger.exception("memory_service.load_context 失败，本回合退回 24 条原文")
        return _load_history(robot.user_id, robot.group_id), "", False


def _schedule_memory_summary(runner: Any, robot: Any, memory_on: bool) -> None:
    """回复落库后异步摘要；失败绝不影响本轮回复。"""
    try:
        runner.submit(
            memory_service.maybe_summarize,
            db, robot.user_id, robot.group_id, memory_on,
        )
    except Exception:
        logger.debug("main._schedule_memory_summary 忽略了异常", exc_info=True)

# ── Group seeding ───────────────────────────────────────
def _seed_group(gid: str) -> None:
    if gid in _seeded_groups:
        return
    _seeded_groups.add(gid)
    try:
        members = llbot.get_group_member_list(gid)
        if members:
            db.seed_group_members(gid, members)
    except Exception:
        logger.debug("main._seed_group 忽略了异常", exc_info=True)

# ── Core logic ──────────────────────────────────────────

def _enabled_tools(disabled: set[str] | None = None) -> list[dict[str, Any]]:
    """Every AI tool minus those whose feature is disabled for this scope.
    All tool selection now happens in the model via native function calling."""
    banned = disabled_tool_names(disabled or set())
    return [t for t in tools_def.ai_tools() if t["function"]["name"] not in banned]


def _tool_chain_json(tool_calls: Any) -> str:
    """Compact, dashboard-friendly record of this turn's tool calls."""
    if not tool_calls:
        return ""
    items = tool_calls if isinstance(tool_calls, list) else [tool_calls]
    chain = []
    for tc in items:
        fn = tc.get("function", {}) if isinstance(tc, dict) else {}
        chain.append({"name": fn.get("name", ""), "arguments": fn.get("arguments", "")})
    try:
        return json.dumps(chain, ensure_ascii=False)[:2000]
    except (TypeError, ValueError):
        return ""


def _reply_note(robot: RobotServer) -> str:
    """Describe the quoted message when the incoming one is a reply.

    LLBot's reply segment is only `{"id": ...}` — no text, no sender — so the
    quoted message is resolved from our own records (bot_messages /
    group_messages). That lookup is what makes "another user quotes the reply
    the bot just gave someone else" work at all.
    """
    reply = getattr(robot.incoming, "reply", None)
    if reply is None:
        return ""

    try:
        is_own = llbot.is_own_message(reply.message_seq, reply.text)
    except Exception:
        logger.debug("is_own_message failed", exc_info=True)
        is_own = False

    note = resolve_quote(reply, is_own,
                         lambda mid: db.find_quoted(robot.group_id, mid),
                         current_user=robot.user_name or "")
    # Quote awareness is otherwise invisible: if the lookup misses, the bot just
    # answers as though nothing were quoted, and there is no error to notice.
    if note:
        logger.info("引用感知命中（id=%s）：%s", reply.message_seq, note[:100])
    elif reply.message_seq is not None:
        logger.info("引用感知未命中：id=%s 不在库里（无法还原被引用的内容）",
                    reply.message_seq)
    return note

def _mood_signal(robot: RobotServer) -> str:
    """Tell the model how hard this user has been leaning on it.

    The persona's temper is meant to escalate across a *conversation*, but
    every request is independent: the model sees only the last few turns, so
    it cannot tell "first question today" from "the sixth time in five
    minutes". The count is computed here and handed over as a fact, which is
    what makes the escalation actually advance instead of restarting at polite
    every turn.
    """
    if not robot.user_id:
        return ""
    try:
        info = db.get_mood(
            robot.user_id, robot.group_id, text=robot.msg,
            pressure_minutes=Config.PATIENCE_WINDOW_MINUTES,
            cooldown_minutes=Config.MOOD_COOLDOWN_MINUTES,
        )
    except Exception:
        logger.debug("mood signal failed", exc_info=True)
        return ""
    if info["level"] <= 0:
        return ""
    detail = f"最近 {Config.PATIENCE_WINDOW_MINUTES} 分钟这个用户找了你 {info['count']} 次"
    if info["repeats"]:
        detail += f"，其中 {info['repeats']} 次是同一件事"
    # Deliberately advisory. Spelling out "you are now at level 3, act like it"
    # made the bot recite the count back instead of just having a mood, which
    # read as mechanical.
    line = (f"【你现在的状态】{detail}（仅供参考：可能会有点「{info['label']}」）。"
            "这只是让你知道自己被磨了多久，**别刻意照着演，也别把这个次数说出来**。")
    if info.get("cooling"):
        line += f"而且你气已经消得差不多了（大约 {Config.MOOD_COOLDOWN_MINUTES} 分钟回到正常），别翻旧账。"
    return line


def _ambient_group_context(robot: RobotServer, disabled: set[str]) -> str:
    """The recent group transcript attached to every group message.

    Off by default (GROUP_CONTEXT_ENABLED=0). This was flipped on and off more
    than once: on, it costs ~400 chars of every group message; off, the model
    must decide to call read_context itself, and it almost never does
    (57 calls against 3370 replies). Turn it on with GROUP_CONTEXT_ENABLED=1
    and re-measure with eval/chat_replay.py before deciding.

    Reuses the same feature key as the read_context tool, so turning 语境读取
    off in the panel disables both the background and the tool.
    """
    if not Config.GROUP_CONTEXT_ENABLED or robot.msg_type != "group":
        return ""
    if "context_read" in disabled or not robot.group_id:
        return ""
    try:
        rows = db.get_recent_group_context(
            robot.group_id,
            minutes=Config.GROUP_CONTEXT_MINUTES,
            limit=Config.GROUP_CONTEXT_LIMIT,
            # Drop only the message being answered: the author's own earlier
            # lines are context too, and non-@ messages reach the model no
            # other way.
            exclude_message_id=robot.incoming.message_id,
        )
    except Exception:
        logger.debug("ambient group context failed", exc_info=True)
        return ""
    return format_group_context(rows, Config.GROUP_CONTEXT_MINUTES)


def _speaker_context(robot: RobotServer, disabled: set[str]) -> str:
    """当前说话人的详细块：关系 + 画像 + 共同记忆。

    放进 user message 而不是 system prompt —— 它随「谁在说话」变化，
    放系统提示词里等于每换个人就废掉整段前缀缓存。
    """
    group_id = robot.group_id or ""
    affection = None
    parts: list[str] = []
    if "affection" not in disabled:
        try:
            # 读 global（群/私聊加权），不在读路径创建任何行。
            global_score = affection_service.get_global(db, robot.user_id)
            affection = {"affection_score": global_score["score"]}
            block = affection_service.relationship_block(db, robot.user_id, group_id)
            if block:
                parts.append(block)
        except Exception:
            logger.debug("relationship block failed", exc_info=True)

    if "profiles" not in disabled:
        try:
            block = profile_service.build_speaker_context(
                db, robot.user_id, group_id, robot.user_name,
                affection=affection,
            )
            if block:
                parts.append(block)
        except Exception:
            logger.debug("speaker profile failed", exc_info=True)

    return "\n".join(parts)


def _log_thinking(user_name: str, reasoning: str) -> None:
    """Log thinking chain to dedicated logger (visible in logs + frontend)."""
    if not reasoning:
        return
    # Truncate very long chains for readability
    preview = reasoning[:800] + "…" if len(reasoning) > 800 else reasoning
    think_log.info("【%s】%s", user_name, preview)



_VISION_DESCRIBE_PROMPT = (
    "客观描述这张图：画的是什么、什么风格、什么情绪。"
    "画面里有文字就照抄出来。"
    "不要评论，不要扮演任何人，不要加语气词。两句话以内。"
)


def _describe_image_into_message(robot: RobotServer, image_url: str) -> None:
    """把图片内容转成文字描述，塞进消息里，然后让**正常聊天链路**去回。

    ## 为什么不再让视觉模型直接生成回复

    之前这条路是「视觉模型一步到位」：图片 + 一段 role_prompt 一起喂进去，
    由视觉模型直接说出那句回复。有两个硬伤：

    1. **语气不是同一个人** —— 它拿到的是 `GROUP_ROLE`（一个可选的短配置，
       默认就是空的），不是平时用的那份完整人格。同一个群里打字聊天是 Kiriko，
       发张图就换了个人。
    2. **接不上前文** —— 没有历史、没有群语境、没有「当前说话人」的画像，
       模型只看得到这一张图，所以回复是割裂的。

    现在视觉只做它**该做的事**：把图看清楚，描述出来。说话交回平时那条链路，
    人格、历史、群语境、好感度全都照常带上。
    """
    desc = ""
    if Config.VISION_ENABLED:
        try:
            desc = AiServer.vision_analyze(
                image_url, prompt=_VISION_DESCRIBE_PROMPT,
                max_tokens=300, temperature=0, source="vision_desc",
            ) or ""
        except Exception:
            logger.exception("Vision describe failed")

    desc = " ".join(desc.strip().split())[:200] or "一张图片，没看清内容"
    tag = f"[图片：{desc}]"
    text = robot.incoming.text.strip()
    robot.incoming.text = f"{text} {tag}".strip() if text else tag
    logger.info("🖼 图片转文字：%s", desc[:60])

    # 表情包归类仍然在后台做，不影响回复速度
    if Config.VISION_ENABLED:
        try:
            executor.submit(_background_sticker_categorize, image_url)
        except Exception:
            logger.debug("sticker categorize submit failed", exc_info=True)


def _background_sticker_categorize(image_url: str) -> None:
    """Best-effort background sticker categorization via vision API.

    Runs after the end-to-end reply is already sent, so this does not
    block the user-facing response time.

    Stickers are already categorized at collection time
    (StickerCollector._auto_categorize); this only fills the gap for images
    that were not collected, so an already-categorized sticker is skipped
    instead of paying for a second vision call.
    """
    try:
        match = None
        for s in db.get_stickers():
            fn = s.get("filename", "")
            if fn and (fn in image_url or image_url.endswith(fn)):
                match = s
                break
        if match and match.get("category") not in ("", "未分类"):
            return
        vision_data = AiServer.vision_analyze_with_category(image_url)
        if not vision_data or not match:
            return
        db.update_sticker_category(
            match["filename"],
            vision_data.get("category", "未分类"),
            vision_data.get("description", ""),
            vision_data.get("emotion", ""),
        )
    except Exception:
        logger.debug("main._background_sticker_categorize 忽略了异常", exc_info=True)


def _trigger_profile_update(robot: RobotServer, disabled: set[str] | None = None) -> None:
    """Submit background profile work for this message.

    Two independent jobs with different scopes:
    - 共同记忆：私聊和群聊都触发。记忆的素材是 bot 对话，而线上绝大多数对话
      发生在私聊里；之前只挂在群聊分支上，私聊永远不会提取（线上记忆一直停在 53 条）。
    - 画像分析：仍然只在群聊里触发（原有行为）。
    """
    # Skip bot accounts
    if robot.user_id in Config.BOT_QQ_LIST:
        return
    # Respect per-group feature toggle
    if disabled and "profiles" in disabled:
        return
    try:
        # 共同记忆：按对话轮数增量提取（见 ProfileService.MEMORY_GAP）
        if profile_service.should_extract_memory(db, robot.user_id):
            executor.submit(
                profile_service.extract_memories,
                db, robot.user_id, robot.user_name,
            )
        # 画像是「这个人」唯一的一份，私聊也要触发（跨群+私聊计数）。
        if profile_service.should_analyze(db, robot.user_id):
            executor.submit(
                profile_service.analyze_user,
                db, robot.user_id, robot.user_name, robot.group_id or "",
            )
    except Exception:
        logger.debug("main._trigger_profile_update 忽略了异常", exc_info=True)


def _run_ai_judge(robot: RobotServer) -> None:
    """Async AI judge: ONE flash call for the sentiment of this message.
    Silent on failure (no delta) — must never affect the chat path."""
    try:
        judge_service.judge_turn(db, robot, affection_on=True)
    except Exception:
        logger.exception("AI judge failed for %s", robot.user_id)


def main_logic(robot: RobotServer) -> None:
    try:
        # ── Skip bot's own messages (echo prevention) ──────
        if str(robot.user_id) == (Config.ROBOT_QQ or ""):
            return

        # Record group message (include image presence in content)
        if robot.msg_type == "group" and robot.group_id:
            _seed_group(robot.group_id)
            msg_content = robot.msg.strip()
            if not msg_content and robot.incoming.has_images:
                msg_content = "[图片消息]"
            if msg_content:
                reply = getattr(robot.incoming, "reply", None)
                db.record_group_message(
                    robot.group_id, robot.user_id, robot.user_name, msg_content,
                    robot.user_role or "",
                    message_id=robot.incoming.message_id,
                    message_seq=robot.incoming.message_seq,
                    reply_to_seq=reply.message_seq if reply else None,
                )

        # ── Feature gate: effective scope + disabled features ──
        scope_type, scope_id = feature_gate.scope_of(robot)
        disabled = feature_gate.disabled_keys(scope_type, scope_id)
        vision_on = Config.VISION_ENABLED and "vision" not in disabled

        # ── Sticker understanding flow ────────────────────
        now = time.time()
        has_images = robot.incoming.has_images
        first_image_url = robot.incoming.image_urls[0] if robot.incoming.image_urls else ""

        # ── @bot + image → analyze ──────────────────────
        if robot.at_judgement and robot.msg_type == "group":
            if has_images and vision_on:
                logger.info("🎯 图片先描述再走正常回复 (@bot+image) from %s", robot.user_name)
                # 不 return —— 描述会并进消息文本，后面的链路照常跑
                _describe_image_into_message(robot, first_image_url)

            # No image and no text (bare @bot ping / QQ-face-only) → cheap canned
            # greeting. No pending, no AI call.
            if not robot.msg.strip() and not has_images:
                logger.info("🎯 Empty @bot message from %s → canned greeting", robot.user_name)
                robot.reply("我在哦～有什么可以帮你的吗？(｡･ω･｡)")
                return

        # ── Private chat with images — always analyze ──
        if has_images and robot.msg_type == "private" and vision_on:
            logger.info("🎯 图片先描述再走正常回复 (private) from %s", robot.user_name)
            _describe_image_into_message(robot, first_image_url)

        # In groups, respond to @bot or when quoting a message sent by the bot (多轮追问)
        is_reply_to_bot = False
        if robot.msg_type == "group" and getattr(robot.incoming, "reply", None):
            try:
                quoted_seq = robot.incoming.reply.message_seq
                is_reply_to_bot = llbot.is_own_message(quoted_seq, robot.incoming.reply.text)
                if not is_reply_to_bot and quoted_seq is not None:
                    quoted = db.find_quoted(robot.group_id, quoted_seq)
                    if quoted and quoted.get("is_own"):
                        is_reply_to_bot = True
            except Exception:
                logger.debug("check is_reply_to_bot failed", exc_info=True)

        # Only respond to @bot or quoting bot or private (after sticker flow)
        if not robot.at_judgement and not is_reply_to_bot and robot.msg_type != "private":
            return

        # Skip bot accounts (self + other bots like QQ 小冰)
        if robot.user_id in Config.BOT_QQ_LIST:
            return

        # ── Affection: record valid interaction (base points only, sync) ──
        if "affection" not in disabled:
            try:
                # 群/私聊用同一套事件分；私聊写 group_id='' 的 private 分量。
                group_id = robot.group_id or ""
                affection_service.touch_streak(
                    db, robot.user_id, group_id, robot.user_name,
                )
                affection_service.record_interaction(
                    db, robot.user_id, group_id, robot.user_name,
                    has_images=bool(getattr(robot.incoming, "has_images", False)),
                )
            except Exception:
                logger.debug("main.main_logic 忽略了异常", exc_info=True)

        # ── AI judge (async): sentiment of THIS message → affection score ──
        has_text = bool(robot.msg.strip())
        affection_on = "affection" not in disabled and has_text
        if affection_on:
            executor.submit(_run_ai_judge, robot)

        # Trigger profile analysis for group messages (async, non-blocking)
        _trigger_profile_update(robot, disabled)

        history, memory_summary, memory_on = _memory_context(robot)
        is_private = robot.msg_type == "private"
        user_text = _context(robot, _reply_note(robot),
                             _ambient_group_context(robot, disabled),
                             mood=_mood_signal(robot),
                             speaker=_speaker_context(robot, disabled),
                             summary=memory_summary)
        system_prompt = _build_system_prompt(
            robot, db, profile_service, affection_service, disabled,
        )

        # Every enabled tool is offered — the AI picks via native function calling
        active_tools = _enabled_tools(disabled)

        if is_private:
            logger.info("Private chat with %s (%d tools)", robot.user_name, len(active_tools))
        else:
            logger.info("Group chat with %s (%d tools)", robot.user_name, len(active_tools))

        ai = AiServer(system_prompt, user_text, history, active_tools,
                      model_type=Config.DEEPSEEK_MODEL, thinking_type="enabled")
        ai.group_id = robot.group_id or ""   # metrics attribution
        ai.ai_request()

        # Log thinking chain
        _log_thinking(robot.user_name, ai.reasoning_content)

        tool_calls = ai.ai_message.get("tool_calls") if ai.ai_message else None
        final_text = ""
        if tool_calls:
            tc_list = tool_calls if isinstance(tool_calls, list) else [tool_calls]
            follow_up_tcs: list[dict[str, Any]] = []

            for tc in tc_list:
                fn = tc.get("function", {}).get("name", "")
                handler = ROUTES.get(fn)
                if not handler:
                    continue

                # Affection bonus for tool engagement
                if "affection" not in disabled:
                    try:
                        affection_service.record_tool_usage(
                            db, robot.user_id, robot.group_id, robot.user_name,
                        )
                    except Exception:
                        logger.debug("main.main_logic 忽略了异常", exc_info=True)

                if fn in SELF_CONTAINED_TOOLS:
                    handler(robot, ai)
                else:
                    handler(robot, ai)
                    follow_up_tcs.append(tc)
                    tc_id = tc.get("id", "")
                    result_text = getattr(ai, "tool_result_text", "") or ai.user_text or ""
                    ai.add_tool_result(tc_id, result_text)

                # Recorded after the handler so the chain also carries the
                # outcome — this is what explain_self replays back to the user.
                # dsh_agent 自己写 running→终态一行，通用记录跳过避免双行。
                if fn != "dsh_agent":
                    try:
                        db.record_tool_usage(
                            fn, robot.user_id, robot.group_id,
                            arguments=tc.get("function", {}).get("arguments", ""),
                            result=getattr(ai, "tool_result_text", "") or "",
                            reasoning=ai.reasoning_content or "",
                        )
                    except Exception:
                        logger.debug("tool chain record failed", exc_info=True)

            if follow_up_tcs:
                ai.follow_up_request(follow_up_tcs)
                _log_thinking(robot.user_name, ai.reasoning_content)
                final_text = ai.ai_text or ""
        elif ai.ai_text:
            final_text = ai.ai_text

        # Persist BEFORE sending. Delivery can block (slow or failing send),
        # and a user who immediately asks "what were you thinking" must not
        # race an unwritten record.
        # The persona forbids reciting the prompt, but that is just text in a
        # prompt and it failed once under repeated pressure ("好吧好吧，别刷屏
        # 了，贴就贴"). Check the outgoing reply too, and record what was
        # actually sent so the model's own history shows the firm stance.
        if final_text and leaked_persona(final_text):
            logger.warning("Blocked a system-prompt leak: %s", final_text[:150])
            final_text = deflection_for(final_text)

        # Never send nothing. An empty reply reads as "the bot is offline",
        # which is worse than a vague line — and it happens for real: a
        # thinking model can spend its whole budget on reasoning and return no
        # content at all. Self-contained tools have already replied for the
        # turn, so they are exempt.
        handled = bool(tool_calls)
        if not final_text and not handled:
            final_text = filler_for(robot.msg)
            logger.warning("Model returned no text; sending a filler line instead")

        # 自回工具自己发了消息：历史里存它「实际发出去的内容」（如抽到的牌），
        # 而不是空的「已调用工具处理」标记，否则之后的对话不知道刚才发了什么。
        saved_text = final_text or getattr(ai, "handled_summary", "") or ""
        _save_turn(robot.user_id, robot.group_id, robot.msg, saved_text,
                   reasoning=ai.reasoning_content or "",
                   tool_chain=_tool_chain_json(tool_calls),
                   # A self-contained tool replied on its own, so this turn is
                   # answered even though final_text stayed empty.
                   handled=handled)
        _schedule_memory_summary(executor, robot, memory_on)
        if final_text:
            robot.reply_chunked(final_text)

    except Exception:
        logger.exception("Error for user %s", robot.user_id)
        try: robot.reply("抱歉，处理消息时遇到了问题，请稍后再试~")
        except Exception:
            logger.debug("main.main_logic 忽略了异常", exc_info=True)

# ── HTTP routes ─────────────────────────────────────────
@app.route("/healthz")
def healthz():
    """Unauthenticated liveness probe for the container healthcheck."""
    return jsonify({"ok": True})


_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def _asset_version() -> str:
    """Cache-buster derived from the dashboard asset mtimes."""
    latest = 0.0
    for rel in ("css/app.css", "js/app.js"):
        try:
            latest = max(latest, os.path.getmtime(os.path.join(_STATIC_DIR, rel)))
        except OSError:
            logger.debug("main._asset_version 忽略了异常", exc_info=True)
    return str(int(latest)) or "1"


def _llbot_public_url() -> str:
    """Where the browser loads the LLBot WebUI from (embedded WebQQ tab)."""
    if Config.LLBOT_WEBUI_PUBLIC_URL:
        return Config.LLBOT_WEBUI_PUBLIC_URL.rstrip("/")
    host = (request.host or "").split(":")[0] or "localhost"
    return f"{request.scheme}://{host}:3080"


@app.route("/", methods=["GET"])
def dashboard():
    return render_template("dashboard.html", asset_v=_asset_version(),
                           llbot_public_url=_llbot_public_url())

@app.route("/status")
def status():
    # Count stickers
    sticker_count = 0
    try:
        sticker_count = len([f for f in os.listdir(STICKER_DIR) if os.path.isfile(os.path.join(STICKER_DIR, f))])
    except Exception:
        logger.debug("main.status 忽略了异常", exc_info=True)
    uptime_sec = int(time.time() - _start_time)
    return jsonify({"ok": True, "model": Config.DEEPSEEK_MODEL, "thinking": "enabled",
                    "tools": len(tools_def.ai_tools()), "groups": len(_seeded_groups),
                    "uptime": uptime_sec,
                    "scheduler": scheduler._running, "stickers": sticker_count})

@app.route("/stream")
def stream():
    def gen():
        while True:
            entries = sse_handler.read()
            if entries: yield f"data: {json.dumps(entries, ensure_ascii=False)}\n\n"
            else: yield ": keepalive\n\n"
            time.sleep(0.5)
    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.route("/api/stats")
def api_stats():
    return jsonify({"totals": db.get_total_stats(),
                    "tools": [{"name": r[0], "count": r[1]} for r in db.get_tool_stats()]})

@app.route("/api/tarot")
def api_tarot(): return jsonify({"records": db.get_all_tarot_history(50)})

@app.route("/api/history")
def api_history(): return jsonify({"records": db.get_all_history(50)})

@app.route("/api/profiles")
def api_profiles():
    """用户画像列表。

    画像在 2026-10 改成了两层结构（`stable` / `recent`）。这里**统一 normalize
    一次**：面板就不必同时兼容新旧两种结构，也不会因为库里还有老格式而渲染出
    一批只有名字、正文全空的卡片。
    """
    rows = db.get_all_profiles()
    for row in rows:
        row["profile"] = normalize_profile(row.get("profile") or {})
        try:
            row["memories"] = [
                m["content"] for m in db.get_profile_memories(row["user_id"], limit=3)
            ]
        except Exception:
            row["memories"] = []
    return jsonify({"profiles": rows})

# ── Affection API ─────────────────────────────────

@app.route("/api/affection")
def api_affection():
    """一人一个总分（global）；带 group_id 时返回该群分量并附 global_score。"""
    group_id = request.args.get("group_id", "")
    if group_id:
        rows = db.fetch_data(
            "SELECT user_id, group_id, user_name, affection_score, interaction_count, "
            "positive_count, negative_count, last_interaction, relationship, notes "
            "FROM user_affection WHERE group_id=? ORDER BY affection_score DESC LIMIT 200",
            (group_id,),
        )
        records = []
        for r in rows:
            label, emoji = AffectionService.get_relationship(float(r[3]))
            records.append({
                "user_id": r[0], "group_id": r[1], "user_name": r[2],
                "affection_score": round(float(r[3]), 1),
                "interaction_count": int(r[4]),
                "positive_count": int(r[5]), "negative_count": int(r[6]),
                "last_interaction": r[7], "relationship": r[8] or label,
                "emoji": emoji, "notes": r[9] or "",
                "global_score": AffectionService.get_global(db, r[0])["score"],
            })
        return jsonify({"affection_records": records, "total": len(records),
                        "group_id": group_id})

    user_ids = [r[0] for r in db.fetch_data(
        "SELECT DISTINCT user_id FROM user_affection",
    )]
    records = []
    for user_id in user_ids:
        global_score = AffectionService.get_global(db, user_id)
        name_rows = db.fetch_data(
            "SELECT user_name FROM user_affection WHERE user_id=? "
            "ORDER BY updated_at DESC LIMIT 1",
            (user_id,),
        )
        records.append({
            "user_id": user_id,
            "user_name": name_rows[0][0] if name_rows else user_id,
            "affection_score": global_score["score"],
            "interaction_count": global_score["interaction_count"],
            "positive_count": global_score["positive_count"],
            "negative_count": global_score["negative_count"],
            "relationship": global_score["relationship"],
            "emoji": global_score["emoji"],
            "components": global_score["components"],
        })
    records.sort(key=lambda row: row["affection_score"], reverse=True)
    return jsonify({"affection_records": records[:200], "total": len(records)})


@app.route("/api/affection/leaderboard")
def api_affection_leaderboard():
    """Get affection leaderboard, optionally filtered by group."""
    group_id = request.args.get("group_id", "")
    limit = request.args.get("limit", 20, type=int)
    board = AffectionService.get_leaderboard(db, group_id=group_id or None, limit=limit)
    return jsonify({"leaderboard": board, "group_id": group_id or None})


@app.route("/api/affection/<user_id>")
def api_affection_user(user_id: str):
    """用户详情：默认返回 global + 各分量；带 group_id 时返回该分量并附 global_score。"""
    group_id = request.args.get("group_id", "")
    global_score = AffectionService.get_global(db, user_id)
    component = None
    if group_id:
        component = next(
            (c for c in global_score["components"] if c["group_id"] == group_id), None,
        )
    score = component["affection_score"] if component else global_score["score"]
    label, emoji = AffectionService.get_relationship(score)
    return jsonify({
        "user_id": user_id,
        "group_id": group_id or None,
        "affection_score": round(score, 1),
        "interaction_count": component["interaction_count"] if component else global_score["interaction_count"],
        "positive_count": global_score["positive_count"],
        "negative_count": global_score["negative_count"],
        "relationship": label,
        "emoji": emoji,
        "global_score": global_score["score"],
        "components": global_score["components"],
    })


@app.route("/api/affection/adjust", methods=["POST"])
def api_affection_adjust():
    """Manually adjust affection score (dashboard use)."""
    data = request.get_json(silent=True) or {}
    user_id = (data.get("user_id") or "").strip()
    group_id = (data.get("group_id") or "").strip()
    scope = (data.get("scope") or "").strip()
    if scope not in ("group", "private"):
        scope = AffectionService.scope_for(group_id)
    delta = float(data.get("delta", 0))
    note = (data.get("note") or "").strip()
    if not user_id:
        return jsonify({"ok": False, "error": "user_id is required"}), 400
    if delta == 0:
        return jsonify({"ok": False, "error": "delta must be non-zero"}), 400
    try:
        result = AffectionService.manual_adjust(db, user_id, group_id, delta, note)
        return jsonify({"ok": True, "adjusted": result})
    except Exception:
        logger.exception("Failed to adjust affection for %s/%s", user_id, group_id)
        return jsonify({"ok": False, "error": "数据库更新失败"}), 500


@app.route("/api/messages")
def api_messages():
    rows = db.get_recent_group_messages("", 100)
    return jsonify({"records": [{"user_id": r[0], "user_name": r[1], "content": r[2][:100], "time": r[3]} for r in rows]})

@app.route("/api/reminders")
def api_reminders():
    rows = db.fetch_data("SELECT id, user_id, group_id, user_name, content, remind_time, fired, repeat_daily FROM reminders ORDER BY remind_time")
    return jsonify({"reminders": [{"id": r[0], "user_id": r[1], "group_id": r[2], "user_name": r[3],
                                   "content": r[4], "remind_time": r[5], "fired": bool(r[6]),
                                   "repeat_daily": bool(r[7])} for r in rows]})

@app.route("/api/reminders/<int:reminder_id>", methods=["DELETE"])
def api_reminders_delete(reminder_id: int):
    try:
        db.execute_action("DELETE FROM reminders WHERE id = ?", (reminder_id,))
        return jsonify({"ok": True, "deleted": reminder_id})
    except Exception:
        logger.exception("Failed to delete reminder #%d", reminder_id)
        return jsonify({"ok": False, "error": "Database delete failed"}), 500

@app.route("/api/reminders", methods=["POST"])
def api_reminders_create():
    data = request.get_json(silent=True) or {}
    content = (data.get("content") or "").strip()
    remind_time = (data.get("remind_time") or "").strip()
    repeat_daily = int(data.get("repeat_daily", False) or False)
    user_name = data.get("user_name", "dashboard")
    user_id = data.get("user_id", "admin")
    group_id = data.get("group_id") or None
    if not content or not remind_time:
        return jsonify({"ok": False, "error": "content and remind_time are required"}), 400
    from datetime import datetime
    try:
        rt = datetime.strptime(remind_time, "%Y-%m-%d %H:%M:%S")
        if rt <= datetime.now():
            return jsonify({"ok": False, "error": "提醒时间不能是过去的时间"}), 400
    except ValueError:
        return jsonify({"ok": False, "error": "时间格式错误，请使用 YYYY-MM-DD HH:MM:SS"}), 400
    try:
        db.deposit(
            "reminders",
            "(user_id, group_id, user_name, content, remind_time, repeat_daily)",
            "(?, ?, ?, ?, ?, ?)",
            (user_id, group_id, user_name, content, remind_time, repeat_daily),
        )
        return jsonify({"ok": True, "created": {"content": content, "remind_time": remind_time, "repeat_daily": bool(repeat_daily)}})
    except Exception:
        logger.exception("Failed to create reminder")
        return jsonify({"ok": False, "error": "Database insert failed"}), 500

@app.route("/api/balance")
def api_balance():
    result = balance_service.get_balance()
    return jsonify(result)

@app.route("/api/scheduler")
def api_scheduler():
    """调度器状态。

    这里原来调的是 `scheduler._get_active_groups()` —— 那个方法只服务于群订阅
    推送，推送整体移除时被一并删掉了，但接口忘了改，于是每次都 500。
    现在直接查库，不再依赖调度器提供这个方法。
    """
    try:
        rows = db.fetch_data(
            "SELECT DISTINCT group_id FROM group_messages WHERE group_id IS NOT NULL"
        )
        active_groups = [r[0] for r in rows if r[0]]
    except Exception:
        logger.debug("api_scheduler active_groups query failed", exc_info=True)
        active_groups = []

    return jsonify({
        "running": scheduler._running,
        "check_interval": scheduler.CHECK_INTERVAL,
        "active_groups": active_groups,
    })

@app.route("/api/stickers")
def api_stickers():
    category = request.args.get("category", "")
    stickers = []
    try:
        stickers = db.get_stickers(category)
    except Exception:
        logger.debug("main.api_stickers 忽略了异常", exc_info=True)
    # If no DB records, fall back to file scan
    if not stickers:
        try:
            for f in sorted(os.listdir(STICKER_DIR)):
                fpath = os.path.join(STICKER_DIR, f)
                if os.path.isfile(fpath):
                    size = os.path.getsize(fpath)
                    stickers.append({
                        "filename": f, "file_hash": "", "category": "未分类",
                        "content_desc": "", "emotion": "", "file_size": size,
                        "collected_at": "", "url": f"/stickers/{f}",
                    })
        except Exception:
            logger.debug("main.api_stickers 忽略了异常", exc_info=True)
    # Add URL to each sticker
    for s in stickers:
        s["url"] = f"/stickers/{s.get('filename', '')}"
    return jsonify({"stickers": stickers, "total": len(stickers)})

@app.route("/api/stickers/categories")
def api_stickers_categories():
    try:
        counts = db.count_stickers_by_category()
        return jsonify({"categories": [{"name": r[0], "count": r[1]} for r in counts]})
    except Exception:
        return jsonify({"categories": []})

@app.route("/api/stickers/<filename>/category", methods=["PATCH"])
def api_stickers_update_category(filename: str):
    data = request.get_json(silent=True) or {}
    category = data.get("category", "").strip()
    if not category:
        return jsonify({"ok": False, "error": "Category is required"}), 400
    if category not in STICKER_CATEGORIES:
        return jsonify({"ok": False, "error": f"无效分类。可选: {', '.join(sorted(STICKER_CATEGORIES))}"}), 400
    content_desc = data.get("content_desc", "")
    emotion = data.get("emotion", "")
    try:
        db.update_sticker_category(filename, category, content_desc, emotion)
        logger.info("Sticker %s category updated to %s", filename, category)
        return jsonify({"ok": True, "updated": {"filename": filename, "category": category}})
    except Exception:
        logger.exception("Failed to update sticker category: %s", filename)
        return jsonify({"ok": False, "error": "数据库更新失败"}), 500

@app.route("/api/stickers/<filename>", methods=["DELETE"])
def api_stickers_delete(filename: str):
    """Delete a sticker file and its DB entry."""
    fpath = os.path.join(STICKER_DIR, filename)
    if not os.path.isfile(fpath):
        return jsonify({"ok": False, "error": "文件不存在"}), 404
    try:
        os.remove(fpath)
        # Remove from DB
        try:
            db.execute_action("DELETE FROM stickers WHERE filename = ?", (filename,))
        except Exception:
            logger.debug("main.api_stickers_delete 忽略了异常", exc_info=True)
        # Invalidate sticker collector caches
        sticker_collector._hashes = None
        sticker_collector._phashes = None
        logger.info("Sticker deleted: %s", filename)
        return jsonify({"ok": True, "deleted": filename})
    except Exception:
        logger.exception("Failed to delete sticker: %s", filename)
        return jsonify({"ok": False, "error": "文件删除失败"}), 500

@app.route("/api/stickers/orphans/cleanup", methods=["POST"])
def api_stickers_orphans_cleanup():
    """Remove DB entries for stickers whose files no longer exist."""
    try:
        count = db.cleanup_orphan_stickers(STICKER_DIR)
        return jsonify({"ok": True, "cleaned": count})
    except Exception:
        logger.exception("Failed to clean orphan stickers")
        return jsonify({"ok": False, "error": "清理失败"}), 500

# ── Batch sticker organize ────────────────────────────

_sticker_organize_state: dict[str, Any] = {
    "running": False,
    "total": 0,
    "completed": 0,
    "failed": 0,
    "errors": [],
    "started_at": None,
}

@app.route("/api/stickers/organize", methods=["POST"])
def api_stickers_organize():
    """Trigger batch categorization of all uncategorized stickers."""
    if _sticker_organize_state["running"]:
        return jsonify({"ok": False, "error": "批量分类已在运行中"}), 400
    executor.submit(_batch_categorize_stickers)
    return jsonify({"ok": True, "msg": "批量分类已启动"})

@app.route("/api/stickers/organize/progress")
def api_stickers_organize_progress():
    """Return batch categorization progress."""
    return jsonify(_sticker_organize_state)

@app.route("/api/stickers/organize", methods=["DELETE"])
def api_stickers_organize_cancel():
    _sticker_organize_state["running"] = False
    return jsonify({"ok": True, "msg": "分类已取消"})

def _batch_categorize_stickers():
    """Background task: categorize all uncategorized stickers using vision API.

    Calls vision_analyze_with_category() for each uncategorized sticker
    and updates the DB with description, emotion, and category.
    Supports cancellation via _sticker_organize_state["running"].
    """
    _sticker_organize_state["running"] = True
    _sticker_organize_state["completed"] = 0
    _sticker_organize_state["failed"] = 0
    _sticker_organize_state["errors"] = []
    _sticker_organize_state["started_at"] = time.time()

    try:
        # Get all uncategorized stickers from DB
        uncategorized = db.get_uncategorized_stickers()
        _sticker_organize_state["total"] = len(uncategorized)
        logger.info("Batch categorize: %d uncategorized stickers found", len(uncategorized))

        for fname, file_hash in uncategorized:
            if not _sticker_organize_state["running"]:
                break

            fpath = os.path.join(STICKER_DIR, fname)
            if not os.path.isfile(fpath):
                _sticker_organize_state["failed"] += 1
                _sticker_organize_state["errors"].append(f"{fname}: file not found")
                continue

            try:
                vision_data = AiServer.vision_analyze_with_category(fpath)
                if vision_data:
                    db.update_sticker_category(
                        fname,
                        vision_data.get("category", "其他"),
                        vision_data.get("description", ""),
                        vision_data.get("emotion", ""),
                    )
                    _sticker_organize_state["completed"] += 1
                else:
                    # Vision API returned None — leave as uncategorized
                    db.update_sticker_category(fname, "未分类", "", "")
                    _sticker_organize_state["completed"] += 1
            except Exception as e:
                _sticker_organize_state["failed"] += 1
                _sticker_organize_state["errors"].append(f"{fname}: {str(e)[:80]}")

            # Rate limit: 0.5s between vision API calls
            time.sleep(0.5)

            done = _sticker_organize_state["completed"] + _sticker_organize_state["failed"]
            if done % 5 == 0:
                logger.info("Batch categorize: %d/%d (failed: %d)",
                             _sticker_organize_state["completed"],
                             _sticker_organize_state["total"],
                             _sticker_organize_state["failed"])

        duration = int(time.time() - (_sticker_organize_state["started_at"] or time.time()))
        logger.info("Batch categorize finished: %d categorized, %d failed in %ds",
                     _sticker_organize_state["completed"],
                     _sticker_organize_state["failed"], duration)
    finally:
        _sticker_organize_state["running"] = False

# ── Sticker dedup endpoints ──────────────────────────

_sticker_dedup_state: dict[str, Any] = {
    "running": False,
    "scan_result": None,
    "cleanup_result": None,
}

@app.route("/api/stickers/duplicates")
def api_stickers_duplicates():
    """Scan for visually similar duplicate stickers using perceptual hash."""
    force = request.args.get("force", "0") == "1"
    if _sticker_dedup_state["running"] and not force:
        return jsonify({"ok": False, "error": "扫描已在运行中"}), 400

    executor.submit(_scan_duplicates)
    return jsonify({"ok": True, "msg": "重复扫描已启动"})

@app.route("/api/stickers/duplicates/progress")
def api_stickers_duplicates_progress():
    """Return duplicate scan results."""
    return jsonify({
        "running": _sticker_dedup_state["running"],
        "scan_result": _sticker_dedup_state["scan_result"],
        "cleanup_result": _sticker_dedup_state["cleanup_result"],
    })

@app.route("/api/stickers/duplicates/cleanup", methods=["POST"])
def api_stickers_duplicates_cleanup():
    """Remove duplicate stickers, keeping the best quality one from each group."""
    if _sticker_dedup_state["running"]:
        return jsonify({"ok": False, "error": "请等待当前操作完成"}), 400

    dry_run = request.args.get("dry_run", "1") == "1"
    executor.submit(_cleanup_duplicates, dry_run)
    return jsonify({"ok": True, "msg": f"去重清理已启动（{'预览模式' if dry_run else '执行模式'}）"})

def _scan_duplicates():
    """Background task: scan for visually similar stickers."""
    _sticker_dedup_state["running"] = True
    _sticker_dedup_state["scan_result"] = None
    try:
        groups = sticker_collector.find_duplicates()
        result = []
        for group in groups:
            group_info = []
            for f in group:
                group_info.append({
                    "filename": f["filename"],
                    "file_size": f["file_size"],
                    "phash": f.get("phash", ""),
                })
            result.append(group_info)

        total_dups = sum(len(g) - 1 for g in groups)
        waste_bytes = sum(
            sum(f["file_size"] for f in g[1:]) for g in groups
        )
        _sticker_dedup_state["scan_result"] = {
            "total_stickers": len(sticker_collector.hashes),
            "groups": len(groups),
            "duplicate_files": total_dups,
            "waste_bytes": waste_bytes,
            "details": result,
        }
        logger.info("Duplicate scan complete: %d groups, %d duplicates, %d bytes wasted",
                     len(groups), total_dups, waste_bytes)
    except Exception:
        logger.exception("Duplicate scan failed")
        _sticker_dedup_state["scan_result"] = {"error": "扫描失败"}
    finally:
        _sticker_dedup_state["running"] = False

def _cleanup_duplicates(dry_run: bool):
    """Background task: remove duplicate stickers."""
    _sticker_dedup_state["running"] = True
    _sticker_dedup_state["cleanup_result"] = None
    try:
        result = sticker_collector.cleanup_duplicates(dry_run=dry_run)
        _sticker_dedup_state["cleanup_result"] = result
        logger.info(
            "Duplicate cleanup (%s): %d groups, %d removed, %d kept, %d bytes freed",
            "dry_run" if dry_run else "executed",
            result["groups_cleaned"], result["files_removed"],
            result["files_kept"], result["total_waste_bytes"],
        )
    except Exception:
        logger.exception("Duplicate cleanup failed")
        _sticker_dedup_state["cleanup_result"] = {"error": "清理失败"}
    finally:
        _sticker_dedup_state["running"] = False

def _list_groups() -> list[dict]:
    """All groups that ever spoke, with message stats (shared by /api/groups and settings)."""
    groups = []
    try:
        rows = db.fetch_data(
            "SELECT group_id, COUNT(*), MAX(timestamp) FROM group_messages "
            "WHERE group_id IS NOT NULL GROUP BY group_id"
        )
    except Exception:
        rows = []
    for gid, msg_count, last_active in rows:
        # Get group name from cache or API
        gname = ""
        try:
            info = llbot.get_group_info(gid)
            gname = info.get("group_name", "") if info else ""
        except Exception:
            logger.debug("main._list_groups 忽略了异常", exc_info=True)
        groups.append({"group_id": gid, "group_name": gname or gid,
                       "msg_count": msg_count, "last_active": last_active or ""})
    groups.sort(key=lambda g: g["msg_count"], reverse=True)
    return groups

@app.route("/api/groups")
def api_groups():
    groups = _list_groups()
    return jsonify({"groups": groups, "total": len(groups)})


@app.route("/api/groups/<group_id>/purge-preview")
def api_group_purge_preview(group_id: str):
    """What deleting this group would remove — shown in the confirm dialog."""
    return jsonify({"ok": True, "group_id": group_id,
                    "counts": db.group_purge_preview(group_id)})


@app.route("/api/groups/<group_id>", methods=["DELETE"])
def api_group_delete(group_id: str):
    """Remove a group: purge all of its data, optionally make the bot leave.

    Body: {"leave": true} also calls OneBot set_group_leave so the bot exits
    the QQ group. Leaving is not done implicitly — it cannot be undone from
    here, the bot must be re-invited.
    """
    body = request.get_json(silent=True) or {}
    leave = bool(body.get("leave"))
    counts = db.purge_group(group_id)
    left = False
    leave_error = ""
    if leave:
        try:
            left = bool(llbot.call("set_group_leave",
                                   {"group_id": str(group_id), "is_dismiss": False}))
            if not left:
                leave_error = "LLBot 调用失败"
        except Exception as exc:
            leave_error = str(exc)
            logger.exception("Failed to leave group %s", group_id)
    logger.info("Group %s deleted (leave=%s, left=%s)", group_id, leave, left)
    return jsonify({"ok": True, "group_id": group_id, "deleted": counts,
                    "left": left, "leave_error": leave_error})


# ── Group activity & transcript (dashboard) ─────────────

@app.route("/api/groups/<group_id>/stats")
def api_group_stats(group_id: str):
    """One day of activity for a group. ?date=YYYY-MM-DD (default today)."""
    day = request.args.get("date") or None
    return jsonify({"ok": True, "group_id": group_id,
                    "stats": db.get_daily_group_stats(group_id, day)})


@app.route("/api/groups/<group_id>/threads")
def api_group_threads(group_id: str):
    """Transcript clustered into topic threads. ?date=&gap= (minutes)."""
    try:
        gap = int(request.args.get("gap", 10))
    except ValueError:
        gap = 10
    threads = db.get_group_threads(group_id, day=request.args.get("date") or None,
                                   max_gap_minutes=gap)
    return jsonify({"ok": True, "group_id": group_id, "threads": threads})


@app.route("/api/groups/<group_id>/days")
def api_group_days(group_id: str):
    """Days that have messages, for the review page's date picker."""
    return jsonify({"ok": True, "days": db.get_group_days(group_id)})


@app.route("/api/groups/<group_id>/messages")
def api_group_messages(group_id: str):
    """Paginated group transcript. Includes the bot's own lines."""
    try:
        page = int(request.args.get("page", 1))
    except ValueError:
        page = 1
    try:
        size = int(request.args.get("size", 100))
    except ValueError:
        size = 100
    result = db.get_group_message_page(
        group_id,
        day=request.args.get("date") or None,
        keyword=(request.args.get("q") or "").strip(),
        user_name=(request.args.get("user") or "").strip(),
        page=page,
        size=size,
    )
    return jsonify({"ok": True, "group_id": group_id, **result})


@app.route("/api/ai/metrics")
def api_ai_metrics():
    """AI usage over the last N hours: volume, latency, tokens, cost, errors."""
    try:
        hours = int(request.args.get("hours", 24))
    except ValueError:
        hours = 24
    return jsonify({"ok": True, **db.get_ai_metrics(hours)})


# ── Feature settings (per-group / per-user toggles) ─────

@app.route("/api/settings/groups")
def api_settings_groups():
    groups = []
    for g in _list_groups():
        groups.append({**g, "disabled": sorted(feature_gate.disabled_keys("group", g["group_id"]))})
    return jsonify({"ok": True, "features": FEATURE_DEFS, "groups": groups})

@app.route("/api/settings/<scope_type>/<scope_id>")
def api_settings_get(scope_type: str, scope_id: str):
    if scope_type not in VALID_SCOPES:
        return jsonify({"ok": False, "error": "invalid scope_type"}), 400
    return jsonify({"ok": True, "settings": feature_gate.effective_map(scope_type, scope_id)})

@app.route("/api/settings/<scope_type>/<scope_id>", methods=["PATCH"])
def api_settings_patch(scope_type: str, scope_id: str):
    if scope_type not in VALID_SCOPES:
        return jsonify({"ok": False, "error": "invalid scope_type"}), 400
    data = request.json or {}
    key = str(data.get("key", ""))
    enabled = bool(data.get("enabled", True))
    try:
        settings = feature_gate.set_enabled(scope_type, scope_id, key, enabled)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception:
        logger.exception("Failed to update feature settings %s:%s", scope_type, scope_id)
        return jsonify({"ok": False, "error": "failed to save"}), 500
    return jsonify({"ok": True, "settings": settings})

@app.route("/api/settings/<scope_type>/<scope_id>", methods=["DELETE"])
def api_settings_delete(scope_type: str, scope_id: str):
    if scope_type not in VALID_SCOPES:
        return jsonify({"ok": False, "error": "invalid scope_type"}), 400
    try:
        settings = feature_gate.reset(scope_type, scope_id)
    except Exception:
        logger.exception("Failed to reset feature settings %s:%s", scope_type, scope_id)
        return jsonify({"ok": False, "error": "failed to reset"}), 500
    return jsonify({"ok": True, "settings": settings})


# ── Reply rate limits (per group) ───────────────────────
#
# 两种模式各自独立开关、可以同时开：
#   单用户模式 —— N 分钟内对同一个用户最多回 Y 条（对所有用户一视同仁）
#   群聊模式   —— N 分钟内在这个群最多发 Y 条

RATE_LIMIT_FIELDS = ("user_enabled", "user_window_min", "user_max",
                     "group_enabled", "group_window_min", "group_max")


@app.route("/api/rate-limits")
def api_rate_limits():
    """所有群的限流配置 + 群列表（面板用它渲染）。"""
    configured = {r["group_id"]: r for r in db.all_rate_limits()}
    groups = []
    for g in _list_groups():
        gid = str(g.get("group_id") or "")
        groups.append({
            **g,
            # 没配过的群给一份默认值，前端不用再判空
            "limit": configured.get(gid) or {"group_id": gid, **{
                "user_enabled": False, "user_window_min": 10, "user_max": 20,
                "group_enabled": False, "group_window_min": 10, "group_max": 30,
            }},
        })
    return jsonify({"ok": True, "groups": groups, "usage": rate_limiter.snapshot()})


@app.route("/api/rate-limits/<group_id>", methods=["POST"])
def api_rate_limits_save(group_id: str):
    data = request.json or {}
    try:
        saved = db.set_rate_limit(group_id, **{
            k: data.get(k) for k in RATE_LIMIT_FIELDS if k in data
        })
    except (ValueError, TypeError) as e:
        return jsonify({"ok": False, "error": f"invalid value: {e}"}), 400
    except Exception:
        logger.exception("Failed to save rate limit for %s", group_id)
        return jsonify({"ok": False, "error": "failed to save"}), 500

    # 立刻生效，别等 RateLimiter 的配置缓存过期
    rate_limiter.invalidate(group_id)
    logger.info("限流配置已更新：%s → %s", group_id, saved)
    return jsonify({"ok": True, "limit": saved})


@app.route("/api/rate-limits/<group_id>", methods=["DELETE"])
def api_rate_limits_reset(group_id: str):
    try:
        db.execute_action("DELETE FROM group_rate_limits WHERE group_id = ?", (str(group_id),))
    except Exception:
        logger.exception("Failed to reset rate limit for %s", group_id)
        return jsonify({"ok": False, "error": "failed to reset"}), 500
    rate_limiter.invalidate(group_id)
    return jsonify({"ok": True})

@app.route("/stickers/<path:filename>")
def serve_sticker(filename: str):
    return send_from_directory(STICKER_DIR, filename)

@app.route("/download/<path:filename>")
def download_shared_file(filename: str):
    """供群友或用户直接下载生成的产物（绕过腾讯 NTQQ 文件传输风控拦截）。"""
    shared_dir = os.path.abspath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "shared_files")
    )
    # 与 send_file 共用同一份黑名单：数据库/密钥/凭据一律不许经免鉴权直链外泄。
    if is_sensitive_file(filename):
        return jsonify({"ok": False, "error": "禁止访问敏感文件"}), 403
    return send_from_directory(shared_dir, filename, as_attachment=True)

# ── DSH (DeepSeek Harness) Console APIs ───────────────
@app.route("/api/dsh/status")
def api_dsh_status():
    """获取 DSH 运行时状态、版本、配置与工作区概览。"""
    available = dsh_agent_tool.client.is_available()
    bin_path = dsh_agent_tool.client._dsh_bin or ""
    version = ""
    if available and bin_path:
        try:
            r = subprocess.run([bin_path, "--version"], capture_output=True, text=True, timeout=5)
            version = r.stdout.strip()
        except Exception:
            version = "unknown"

    workspace = os.path.abspath(Config.DSH_WORKSPACE_DIR)
    patch_file = os.path.expanduser(Config.DSH_PATCH_PATH)

    # 读取活跃会话映射
    sessions = []
    try:
        rows = db.fetch_data("SELECT key, value FROM app_state WHERE key LIKE 'dsh_session:%' ORDER BY key")
        for k, v in rows:
            sessions.append({
                "target": k.replace("dsh_session:", ""),
                "session_id": v,
            })
    except Exception:
        pass

    return jsonify({
        "available": available,
        "dsh_bin": bin_path,
        "version": version,
        "workspace": workspace,
        "patch_file": patch_file,
        "timeout": Config.DSH_TIMEOUT,
        "max_concurrency": Config.DSH_MAX_CONCURRENCY,
        "sessions": sessions,
        # 3085 的官方 WebUI 需要浏览器侧 token 交换 + 287MB RSS，1.6G 机器上不划算；
        # 面板改用同源的原生状态页，这里显式说明，不再让前端拼 iframe。
        "web_ui": "native",
    })


def _percentile(sorted_values: list[int], q: float) -> int | None:
    """线性插值百分位（和 Hina 的离线口径一致，部署后才可比）。"""
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = (len(sorted_values) - 1) * q
    lower = int(pos)
    upper = min(lower + 1, len(sorted_values) - 1)
    frac = pos - lower
    return int(round(sorted_values[lower] * (1 - frac) + sorted_values[upper] * frac))


@app.route("/api/dsh/tasks")
def api_dsh_tasks():
    """最近 DSH 任务与指标：p50/p90（只算 ok）、成功率、恢复率。"""
    limit = request.args.get("limit", 20, type=int)
    try:
        rows = db.fetch_data(
            "SELECT id, timestamp, status, duration_ms, session_key, "
            "session_recovered, queue_wait_ms, error "
            "FROM tool_usage WHERE tool_name='dsh_agent' "
            "AND status IS NOT NULL AND status <> '' "
            "ORDER BY id DESC LIMIT ?",
            (max(1, min(limit, 100)),),
        )
    except Exception:
        rows = []
    tasks = [{
        "id": r[0], "timestamp": r[1], "status": r[2] or "",
        "duration_ms": int(r[3] or 0), "session_key": r[4] or "",
        "session_recovered": int(r[5] if r[5] is not None else -1),
        "queue_wait_ms": int(r[6] or 0),
        "error": r[7] or "",
    } for r in rows]

    try:
        durations = sorted(
            int(r[3] or 0) for r in rows if (r[2] or "") == "ok" and int(r[3] or 0) > 0
        )
        terminal = [r for r in rows if (r[2] or "") in
                    ("ok", "empty", "timeout", "error", "interrupted")]
        ok_count = sum(1 for r in terminal if (r[2] or "") == "ok")
        rejected = [r for r in rows if (r[2] or "") == "rejected"]
        # 恢复率只看有存量 session 的任务（-1 = 本来就没有，不进分母）
        resumed = [r for r in rows if int(r[5] if r[5] is not None else -1) in (0, 1)]
        recovered = sum(1 for r in resumed if int(r[5]) == 1)
        stats = {
            "n": len(terminal),
            "p50_ms": _percentile(durations, 0.5),
            "p90_ms": _percentile(durations, 0.9),
            "success_rate": round(ok_count / len(terminal), 3) if terminal else None,
            "rejected": len(rejected),
            # 没有终态任务时拒绝率没有意义（旧行 status 为空也不算），回 None。
            "reject_rate": round(len(rejected) / len(rows), 3) if terminal and rows else None,
            "session_tasks": len(resumed),
            "recovery_rate": round(recovered / len(resumed), 3) if resumed else None,
        }
    except Exception:
        stats = {}
    return jsonify({"tasks": tasks, "stats": stats})


@app.route("/api/dsh/plugins")
def api_dsh_plugins():
    """读取 DSH 的 profile 插件列表与依赖。"""
    profile_dir = os.path.expanduser("~/.dsh/profiles/headless")
    pkg_file = os.path.join(profile_dir, "package.json")
    dsh_profile_file = os.path.join(profile_dir, "dsh.profile")

    bundles = []
    dependencies = {}
    if os.path.exists(pkg_file):
        try:
            with open(pkg_file, encoding="utf-8") as f:
                data = json.load(f)
                dependencies = data.get("dependencies", {})
                bundles = data.get("dsh", {}).get("profile", {}).get("bundles", [])
        except Exception as e:
            logger.debug("读取 package.json 失败: %s", e)

    if not bundles and os.path.exists(dsh_profile_file):
        try:
            with open(dsh_profile_file, encoding="utf-8") as f:
                d = json.load(f)
                bundles = d.get("bundles", [])
        except Exception:
            pass

    return jsonify({
        "bundles": bundles,
        "dependencies": dependencies,
        "profile_dir": profile_dir,
    })


@app.route("/api/dsh/execute", methods=["POST"])
def api_dsh_execute():
    """从 Web 控制台手动向 DSH 委派执行任务并返回结果。"""
    data = request.get_json(silent=True) or {}
    task = str(data.get("task") or "").strip()
    session_key = str(data.get("session_key") or "web_console").strip()
    if not re.fullmatch(r"[A-Za-z0-9_:.\-]{1,80}", session_key):
        session_key = "web_console"
    # 前端传来的 timeout 不能超过服务端配置，避免一个请求占住线程远超预算。
    try:
        timeout = int(data.get("timeout") or 120)
    except (TypeError, ValueError):
        timeout = 120
    timeout = max(1, min(timeout, int(Config.DSH_TIMEOUT or 600)))

    if not task:
        return jsonify({"ok": False, "error": "任务内容不能为空"}), 400

    task_id = 0
    t0 = time.monotonic()
    try:
        task_id = db.start_dsh_task("web_console", None, session_key)
    except Exception:
        logger.debug("web console dsh row insert failed", exc_info=True)

    meta: dict[str, Any] = {}
    try:
        result = dsh_agent_tool.client.run_task(
            task, session_key=session_key, timeout=timeout, meta=meta,
        )
    except Exception as e:
        logger.exception("Web 控制台触发 DSH 任务失败")
        meta.setdefault("status", "error")
        meta.setdefault("error", f"{type(e).__name__}: {e}")
        result = f"执行失败：{e}"

    status = str(meta.get("status") or "error")
    if status == "unavailable":
        status = "error"
    if task_id:
        try:
            db.finish_dsh_task(
                task_id, status=status,
                duration_ms=int((time.monotonic() - t0) * 1000),
                session_recovered=int(meta.get("session_recovered", -1)),
                error=str(meta.get("error") or ""),
                arguments=task, result=str(result or "")[:500],
            )
        except Exception:
            logger.debug("web console dsh row update failed", exc_info=True)

    return jsonify({
        "ok": status in ("ok", "empty"),
        "task": task,
        "session_key": session_key,
        "timeout": timeout,
        "status": status,
        "result": result,
    })


def _webhook_signature_ok(raw_body: bytes) -> bool:
    """Verify LLBot's `x-signature` on an incoming event.

    The signing scheme lives in webhook_auth (dependency-free, unit tested);
    here we only bind it to the request. Returns True when no token is
    configured — an unauthenticated webhook must not silently break a running
    bot, but it is called out loudly at startup.
    """
    return webhook_auth.signature_ok(
        Config.WEBHOOK_TOKEN, raw_body, request.headers.get("X-Signature")
    )


@app.route("/webhook", methods=["POST"])
@app.route("/", methods=["POST"])
def receive():
    # 验签前先看 Content-Length：未鉴权的超大请求不能先被读进内存。
    declared = request.content_length
    if declared is not None and declared > MAX_WEBHOOK_BODY:
        logger.warning(
            "Rejected oversized webhook: %s declared bytes from %s (limit %s)",
            declared, request.remote_addr, MAX_WEBHOOK_BODY,
        )
        return jsonify({"status": "too_large"}), 413

    # Read the raw body first: the signature is over the exact bytes sent.
    raw_body = request.get_data(cache=True)
    if len(raw_body) > MAX_WEBHOOK_BODY:
        # 没有 Content-Length（chunked）时靠这里兜底。
        logger.warning(
            "Rejected oversized webhook: %s bytes from %s (limit %s)",
            len(raw_body), request.remote_addr, MAX_WEBHOOK_BODY,
        )
        return jsonify({"status": "too_large"}), 413
    if not _webhook_signature_ok(raw_body):
        logger.warning(
            "Rejected webhook without a valid signature from %s "
            "(check that LLBot's http-post token matches WEBHOOK_TOKEN/ONEBOT_TOKEN)",
            request.remote_addr,
        )
        return jsonify({"status": "unauthorized"}), 403

    msg_data = request.json
    if not msg_data: return jsonify({"status": "nodata"}), 400

    # ── Only process message events; skip notices (recalls, pokes, etc.) ──
    post_type = msg_data.get("post_type", "message")
    if post_type != "message":
        # Log recall events for debugging but don't process them
        notice_type = msg_data.get("notice_type", "")
        if notice_type:
            logger.info(
                "Ignoring notice event: type=%s user=%s group=%s",
                notice_type, msg_data.get("user_id", ""), msg_data.get("group_id", ""),
            )
        return jsonify({"status": "ignored", "reason": f"post_type={post_type}"}), 200

    if msg_data.get("message_type") == "group":
        gid = str(msg_data.get("group_id") or "")
        if gid and feature_gate.is_enabled("group", gid, "sticker_collect"):
            executor.submit(sticker_collector.collect, msg_data)
    try:
        robot = RobotServer(msg_data, llbot, Config.ROBOT_QQ or "")
    except Exception:
        return jsonify({"status": "error"}), 400
    executor.submit(main_logic, robot)
    return jsonify({"status": "success"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
