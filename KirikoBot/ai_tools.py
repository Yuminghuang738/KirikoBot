from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
from typing import Any

from config import Config

logger = logging.getLogger(__name__)

# One long message must not crowd out the rest of the transcript.
_CONTEXT_LINE_LIMIT = 100


# ── Helper ──────────────────────────────────────────────

def _set_tool_meta(ai_server: Any, tool_calls: Any, extra: str = "") -> None:
    if tool_calls:
        ai_server.airesponse_tool_id = tool_calls[0].get("id", "")
        ai_server.airesponse_tool_calls = tool_calls


# ══════════════════════════════════════════════════════════
#  Tarot
# ══════════════════════════════════════════════════════════

class Tarot:
    def __init__(self, database_manager: Any, msg_package: Any) -> None:
        self.database_manager = database_manager
        self.msg_package = msg_package

    def _draw_card(self) -> dict[str, str]:
        try:
            data = self.database_manager.takeout("tarot_content", "card_name, card_text, card_path")
        except Exception:
            logger.exception("tarot_content query failed")
            return {"card_name": "未知", "card_text": "数据库不可用", "card_path": ""}
        if not data:
            return {"card_name": "未知", "card_text": "牌库为空", "card_path": ""}
        card_name, card_text, card_path = random.choice(data)
        return {"card_name": card_name, "card_text": card_text, "card_path": card_path}

    def _lookup_target(self, robot: Any, target_name: str) -> str | None:
        """Find a group member's user_id by name. Returns user_id or None."""
        try:
            rows = self.database_manager.fetch_data(
                "SELECT DISTINCT user_id FROM group_messages "
                "WHERE group_id=? AND user_name LIKE ? ORDER BY id DESC LIMIT 1",
                (robot.group_id, f"%{target_name}%"),
            )
            return rows[0][0] if rows else None
        except Exception:
            return None

    def _resend_today(self, robot: Any, ai: Any, card: dict[str, Any],
                      display_name: str, is_for_self: bool) -> None:
        """今天已经抽过：把同一张牌再发一次，只发这一条。

        不再额外请 AI 解读（之前会发第二条）。「一天一张」「牌好坏都算数」这两句
        直接写进这条消息里，所以模型不需要再开口。
        """
        from llbot_client import MessageBuilder

        builder = MessageBuilder()
        if is_for_self:
            builder.text("🔮 今天已经抽过啦，一天只能抽一张，还是这张哦～\n\n")
        else:
            builder.text(f"🔮 {display_name}的牌今天已经抽过了，一天只能抽一张，还是这张。\n\n")
        if card.get("card_path"):
            builder.image(card["card_path"])
        builder.text(f"\n🎴 {display_name}今天抽到的：{card['card_name']}\n{card.get('card_text') or ''}")
        builder.text("\n（牌好是坏都算数，抽到什么就是什么。）")
        robot.send(builder.build())
        ai.handled_summary = self._summary(card, display_name, resent=True)

    @staticmethod
    def _summary(card: dict[str, Any], display_name: str, resent: bool = False) -> str:
        """这一轮实际发出去的内容的摘要，写进历史，让之后的对话知道抽到了什么。"""
        head = "重新发送了今天的牌" if resent else "抽牌"
        return f"[塔罗牌·{head}] {display_name}：{card['card_name']}，{card.get('card_text') or ''}"

    def tarot_call(self, robot: Any, ai: Any) -> None:
        tool_calls = ai.ai_message.get("tool_calls")
        _set_tool_meta(ai, tool_calls)

        # Parse target_name from tool arguments
        target_name = ""
        if tool_calls:
            try:
                args = json.loads(tool_calls[0]["function"].get("arguments", "{}"))
                target_name = args.get("target_name", "")
            except (json.JSONDecodeError, TypeError):
                logger.debug("ai_tools.tarot_call 忽略了异常", exc_info=True)

        # Determine who the card is for
        is_for_self = not target_name or target_name == robot.user_name
        display_name = robot.user_name if is_for_self else target_name
        target_uid = self._lookup_target(robot, target_name) if not is_for_self else None

        # One card per person per day. The limit is on the REQUESTER, which is
        # also who tarot_history records, so asking on behalf of ten friends
        # doesn't get you ten draws. A repeat re-serves the original card
        # instead of drawing a new one — the point is that it stands, whether
        # it was good or bad.
        try:
            already = self.database_manager.get_today_tarot(robot.user_id)
        except Exception:
            logger.debug("tarot daily check failed", exc_info=True)
            already = None
        if already:
            self._resend_today(robot, ai, already, display_name, is_for_self)
            return

        card = self._draw_card()

        # 只发一条：牌面 + 牌名 + 简短解释（数据库里的关键词，本身就只有十来个字）。
        # 不再额外请 AI 写第二条解读。
        from llbot_client import MessageBuilder
        builder = MessageBuilder()
        if not is_for_self and target_name:
            builder.text(f"🔮 应 {robot.user_name} 的要求，给 {target_name} 抽了一张塔罗牌！\n\n")
        if card["card_path"]:
            builder.image(card["card_path"])
        builder.text(f"\n🎴 {display_name}的塔罗牌：{card['card_name']}\n{card['card_text']}")
        robot.send(builder.build())

        # 历史：主流程会保存这一轮，这里只给它一段「实际发出去的内容」作为助手的回复，
        # 不再自己写 user/assistant/tool 行（那样会和主流程重复，出现连续两条 user）。
        try:
            self.database_manager.deposit_tarot_history(robot.user_id, card["card_name"])
        except Exception:
            logger.exception("Failed to deposit tarot history")
        ai.handled_summary = self._summary(card, display_name)


class Tarot_History:
    def __init__(self, database_manager: Any, msg_package: Any) -> None:
        self.database_manager = database_manager
        self.msg_package = msg_package

    def tarot_history_call(self, robot: Any, ai: Any) -> None:
        try:
            rows = self.database_manager.takeout_tarot_history(robot.user_id)
        except Exception:
            logger.exception("tarot_history query failed")
            robot.reply("抱歉，获取塔罗牌记录时出了点问题~")
            return

        if not rows:
            robot.reply("你还没有抽取过塔罗牌哦～快来抽一张吧！(◕‿◕✿)")
        else:
            lines = ["你的塔罗牌记录："]
            for name, ts in rows:
                lines.append(f"  {ts} · {name}")
            robot.reply("\n".join(lines))

        tool_calls = ai.ai_message.get("tool_calls")
        _set_tool_meta(ai, tool_calls)
        ai.user_text = robot.text if hasattr(robot, 'text') else ""


# ══════════════════════════════════════════════════════════
#  Web Search (RAG-style: search → fetch → feed AI → reply)
# ══════════════════════════════════════════════════════════

class WebSearchTool:
    def __init__(self, web_search: Any, msg_package: Any) -> None:
        self.web_search = web_search
        self.msg_package = msg_package

    def web_search_call(self, robot: Any, ai: Any) -> None:
        tool_calls = ai.ai_message.get("tool_calls")
        if not tool_calls:
            return

        try:
            args = json.loads(tool_calls[0]["function"].get("arguments", "{}"))
        except (json.JSONDecodeError, TypeError):
            args = {}
        query = args.get("query", robot.msg)

        # Fetch search results + page content
        try:
            content = self.web_search.search_and_fetch(query)
        except Exception:
            logger.exception("Search+fetch failed")
            robot.reply("抱歉，联网搜索暂时不可用，请稍后再试~")
            return

        if not content:
            # 失败原因分几种，对用户说的话不一样：额度用完/被限流/配置错了，
            # 换关键词是没用的，得让他知道该等还是该找开发者。文案由
            # WebSearch.failure_message() 统一管，这里只负责发出去。
            reason = getattr(self.web_search, "last_reason", "error")
            message = getattr(self.web_search, "failure_message", None)
            text = message() if callable(message) else "搜索没成，待会儿再试试吧"
            logger.info("web_search 失败：reason=%s → 回复用户「%s」", reason, text)
            robot.reply(text)
            return

        # Feed search results to AI for synthesis
        from datetime import datetime
        current_date = datetime.now().strftime("%Y年%m月%d日")
        # 综合回答关闭思考模式：资料已经给全了，思考只会多花约 1 秒（基准：中位 2.8s → 1.9s，
        # 回答质量 4.8 → 4.7，两边都是 100% 得分 ≥4）。
        ai.model_type = Config.DEEPSEEK_MODEL
        ai.thinking_type = "disabled"
        ai.system_text = (
            "你是Kiriko，请根据以下搜索结果回答用户问题。"
            f"当前日期是{current_date}，你的知识截止于2025年，请以当前日期和搜索结果为准。"
            "用可爱的语气，简洁明了，不要长篇大论，控制在300字以内。"
            "禁止使用表格，用自然的段落文字回复。"
            f"\n\n用户问题：{robot.msg}\n\n搜索结果：\n{content}"
        )
        # Clear history for search context (irrelevant old messages confuse)
        ai.history_list = []
        ai.user_text = f"请根据以上搜索结果回答：{robot.msg}"
        ai.ai_request()

        if ai.ai_text:
            robot.reply(ai.ai_text)
        else:
            robot.reply("抱歉，没能整理出搜索结果，请换个问法试试~")

        _set_tool_meta(ai, tool_calls)
        ai.user_text = f"搜索查询: {query}\n搜索内容: {content[:500]}"



# ══════════════════════════════════════════════════════════
#  Sticker
# ══════════════════════════════════════════════════════════

class StickerTool:
    STICKER_DIR = "/app/stickers"

    def __init__(self, msg_package: Any) -> None:
        self.msg_package = msg_package
        self._cache: list[str] = []

    def _scan(self) -> list[str]:
        if self._cache:
            return self._cache
        import os
        try:
            self._cache = [
                f for f in os.listdir(self.STICKER_DIR)
                if f.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))
            ]
        except Exception:
            logger.exception("Sticker scan failed")
        return self._cache

    def _pick_by_category(self, category: str) -> str | None:
        """Query DB for stickers matching the given category. Returns filename or None."""
        try:
            from database_manager import DatabaseManager
            db = DatabaseManager()
            stickers = db.get_stickers(category)
            if stickers:
                return random.choice(stickers)["filename"]
        except Exception:
            logger.debug("ai_tools._pick_by_category 忽略了异常", exc_info=True)
        return None

    def sticker_call(self, robot: Any, ai: Any) -> None:
        import json
        tool_calls = ai.ai_message.get("tool_calls")
        args: dict[str, Any] = {}
        if tool_calls:
            try:
                args = json.loads(tool_calls[0]["function"].get("arguments", "{}"))
            except (json.JSONDecodeError, TypeError):
                logger.debug("ai_tools.sticker_call 忽略了异常", exc_info=True)
        category = (args.get("category") or "").strip()

        chosen: str | None = None

        # Try DB lookup by category first
        if category and category != "":
            chosen = self._pick_by_category(category)
            if chosen:
                logger.info("Sticker: matched category '%s' → %s", category, chosen)

        # Fall back to random file scan
        if not chosen:
            self._cache = []  # Clear cache to pick up newly collected stickers
            stickers = self._scan()
            if not stickers:
                robot.reply("暂时没有表情包哦～请添加一些 Kiriko 图片吧！(｡•́︿•̀｡)")
                return
            chosen = random.choice(stickers)

        # Send image directly, no reply wrapper
        from llbot_client import MessageBuilder
        builder = MessageBuilder().image(f"{self.STICKER_DIR}/{chosen}")
        robot.send(builder.build())
        _set_tool_meta(ai, tool_calls)
        ai.user_text = f"发送了表情包({category or '随机'}): {chosen}"




# ══════════════════════════════════════════════════════════
#  Proactive @ Member
# ══════════════════════════════════════════════════════════

class AtMemberTool:
    def __init__(self, msg_package: Any, database_manager: Any = None, llbot: Any = None) -> None:
        self.msg_package = msg_package
        self.db = database_manager
        self.llbot = llbot

    def _resolve_target(self, robot: Any, target: str) -> tuple[str | None, str | None]:
        """Resolve target to (qq_number, display_name).
        1. '群主' → get_group_info API
        2. '管理员' → DB role=admin or cached members
        3. names → DB fuzzy match → cached member list"""
        target = target.strip()
        group_id = robot.group_id
        bot_qq = Config.ROBOT_QQ or ""

        # ── 群主 → use LLBot get_group_info API ──
        if target == "群主":
            if self.llbot and group_id:
                info = self.llbot.get_group_info(group_id)
                if info:
                    owner_uid = str(info.get("owner_id", "") or info.get("owner_user_id", ""))
                    if owner_uid:
                        # Guard: never resolve to the bot itself
                        if owner_uid == bot_qq or owner_uid == robot.user_id:
                            logger.debug("Skipping self-resolution (owner is bot)")
                            return (None, None)
                        # Try cached members or DB for owner's name
                        if self.db:
                            cached = self.db._member_cache.get(group_id, [])
                            for m in cached:
                                if m["user_id"] == owner_uid:
                                    return (owner_uid, m["user_name"] or "群主")
                            rows = self.db.fetch_data(
                                "SELECT user_name FROM group_messages WHERE group_id=? AND user_id=? LIMIT 1",
                                (group_id, owner_uid),
                            )
                            if rows:
                                return (owner_uid, rows[0][0])
                        return (owner_uid, "群主")
            # Fallback: cached members with role=owner
            if self.db:
                qq, name = self.db.find_member_by_role(group_id, "owner")
                if qq and qq != bot_qq and qq != robot.user_id:
                    return (qq, name)

        # ── 管理员 → cached members + DB ──
        if target in ("管理员", "群管理", "管理"):
            if self.db:
                qq, name = self.db.find_member_by_role(group_id, "admin")
                if qq and qq != robot.user_id and qq != bot_qq:
                    return (qq, name)

        # ── Name match → DB first, then cached members ──
        if self.db:
            qq, name = self.db.find_member_by_name(group_id, target)
            if qq:
                # Guard: never resolve to the bot itself
                if qq == bot_qq or qq == robot.user_id:
                    logger.debug("Skipping self-resolution (name matched bot)")
                    return (None, None)
                return (qq, name)

        return (None, None)

    def at_member_call(self, robot: Any, ai: Any) -> None:
        tool_calls = ai.ai_message.get("tool_calls")
        if not tool_calls:
            return

        try:
            args = json.loads(tool_calls[0]["function"].get("arguments", "{}"))
        except (json.JSONDecodeError, TypeError):
            args = {}
        target = args.get("target_name", "").strip()
        message = args.get("message", "").strip()

        if not target or not message:
            return

        # Resolve to QQ number + display name
        target_qq, display_name = self._resolve_target(robot, target)
        if not target_qq or not display_name:
            robot.reply(
                f"呜～Kiriko没找到「{target}」呢 (｡•́︿•̀｡)\n"
                "可能ta还没在群里说过话，换个方式试试？"
            )
            _set_tool_meta(ai, tool_calls)
            return

        # Generate the message via AI
        ai.model_type = Config.DEEPSEEK_MODEL
        ai.thinking_type = "disabled"
        ai.system_text = (
            f"你是Kiriko。你要主动@群友{display_name}说一句话。"
            f"意图：{message}。语气可爱自然，20字以内。"
        )
        ai.user_text = f"请对{display_name}说一句话"
        ai.ai_request()

        content = ai.ai_text.strip() if ai.ai_text else message

        # Use proper OneBot at segment with QQ number
        from llbot_client import MessageBuilder
        builder = MessageBuilder()
        builder.at(target_qq)
        builder.text(f" {content}")

        # 私聊里 at 是群专属功能，发纯文本，别塞无效的 @ 段
        if robot.msg_type != "group":
            builder = MessageBuilder()
            builder.text(f"想对 {display_name} 说：{content}")
        robot.send(builder.build())

        _set_tool_meta(ai, tool_calls)
        ai.user_text = f"@了{display_name}({target_qq}): {content}"


# ══════════════════════════════════════════════════════════
#  Reminder
# ══════════════════════════════════════════════════════════

class ReminderTool:
    def __init__(self, database_manager: Any, msg_package: Any) -> None:
        self.db = database_manager
        self.msg_package = msg_package

    def set_reminder_call(self, robot: Any, ai: Any) -> None:
        tool_calls = ai.ai_message.get("tool_calls")
        if not tool_calls:
            return

        try:
            args = json.loads(tool_calls[0]["function"].get("arguments", "{}"))
        except (json.JSONDecodeError, TypeError):
            args = {}
        user_msg = args.get("user_message", "") or robot.msg

        from scheduler import parse_reminder_time
        remind_time, content, repeat_daily = parse_reminder_time(user_msg)

        if not remind_time:
            ai.tool_result_text = f"提醒设置失败：{content}"
            _set_tool_meta(ai, tool_calls)
            ai.user_text = ai.tool_result_text
            return

        from datetime import datetime
        try:
            rt = datetime.strptime(remind_time, "%Y-%m-%d %H:%M:%S")
            if rt <= datetime.now():
                ai.tool_result_text = "提醒时间已过期，无法设置"
                _set_tool_meta(ai, tool_calls)
                ai.user_text = ai.tool_result_text
                return
        except ValueError:
            ai.tool_result_text = "提醒时间计算错误"
            _set_tool_meta(ai, tool_calls)
            ai.user_text = ai.tool_result_text
            return

        try:
            self.db.deposit(
                "reminders",
                "(user_id, group_id, user_name, content, remind_time, repeat_daily)",
                "(?, ?, ?, ?, ?, ?)",
                (robot.user_id, robot.group_id, robot.user_name, content, remind_time, repeat_daily),
            )
        except Exception:
            logger.exception("Failed to save reminder")
            ai.tool_result_text = "提醒保存失败（数据库错误）"
            _set_tool_meta(ai, tool_calls)
            ai.user_text = ai.tool_result_text
            return

        friendly = rt.strftime("%m月%d日 %H:%M:%S")
        if repeat_daily:
            ai.tool_result_text = f"每日提醒已设置：每天 {rt.strftime('%H:%M:%S')} 提醒内容：{content}"
        else:
            ai.tool_result_text = f"提醒已设置：{friendly} 提醒内容：{content}"

        _set_tool_meta(ai, tool_calls)
        ai.user_text = ai.tool_result_text


# ══════════════════════════════════════════════════════════
#  List Reminders
# ══════════════════════════════════════════════════════════

class ListRemindersTool:
    def __init__(self, database_manager: Any, msg_package: Any) -> None:
        self.db = database_manager
        self.msg_package = msg_package

    def list_reminders_call(self, robot: Any, ai: Any) -> None:
        try:
            rows = self.db.fetch_data(
                "SELECT id, content, remind_time, fired, repeat_daily FROM reminders "
                "WHERE user_id = ? ORDER BY remind_time",
                (robot.user_id,),
            )
        except Exception:
            logger.exception("List reminders failed")
            ai.tool_result_text = "获取提醒列表失败"
            _set_tool_meta(ai, ai.ai_message.get("tool_calls"))
            ai.user_text = ai.tool_result_text
            return

        if not rows:
            ai.tool_result_text = "你当前没有设置任何提醒"
        else:
            lines = ["你的提醒列表："]
            for rid, content, rt, fired, repeat in rows:
                tag = "✓" if fired else "⏳"
                rep = " [每日]" if repeat else ""
                lines.append(f"  #{rid} {tag}{rep} {rt} — {content}")
            ai.tool_result_text = "\n".join(lines)

        _set_tool_meta(ai, ai.ai_message.get("tool_calls"))
        ai.user_text = ai.tool_result_text


# ══════════════════════════════════════════════════════════
#  Delete Reminder
# ══════════════════════════════════════════════════════════

class DeleteReminderTool:
    def __init__(self, database_manager: Any, msg_package: Any) -> None:
        self.db = database_manager
        self.msg_package = msg_package

    def delete_reminder_call(self, robot: Any, ai: Any) -> None:
        tool_calls = ai.ai_message.get("tool_calls")
        if not tool_calls:
            return

        try:
            args = json.loads(tool_calls[0]["function"].get("arguments", "{}"))
        except (json.JSONDecodeError, TypeError):
            args = {}
        reminder_id = args.get("reminder_id", 0)
        keyword = args.get("keyword", "").strip()

        if reminder_id:
            # Delete by ID
            rows = self.db.fetch_data(
                "SELECT id, content FROM reminders WHERE id = ? AND user_id = ?",
                (reminder_id, robot.user_id),
            )
            if not rows:
                ai.tool_result_text = f"未找到 #{reminder_id} 提醒，可能不属于你或已被删除"
                _set_tool_meta(ai, tool_calls)
                ai.user_text = ai.tool_result_text
                return
            try:
                self.db.execute_action("DELETE FROM reminders WHERE id = ?", (reminder_id,))
                ai.tool_result_text = f"已删除提醒 #{reminder_id}：{rows[0][1]}"
            except Exception:
                logger.exception("Failed to delete reminder #%d", reminder_id)
                ai.tool_result_text = "删除提醒失败（数据库错误）"
        elif keyword:
            # Delete by content keyword match
            rows = self.db.fetch_data(
                "SELECT id, content FROM reminders WHERE user_id = ? AND content LIKE ? AND fired = 0",
                (robot.user_id, f"%{keyword}%"),
            )
            if not rows:
                ai.tool_result_text = f"未找到包含「{keyword}」的待触发提醒"
                _set_tool_meta(ai, tool_calls)
                ai.user_text = ai.tool_result_text
                return
            if len(rows) > 1:
                lines = [f"找到 {len(rows)} 个匹配的提醒，请指定要删除的编号："]
                for rid, content in rows:
                    lines.append(f"  #{rid} — {content}")
                ai.tool_result_text = "\n".join(lines)
            else:
                try:
                    self.db.execute_action("DELETE FROM reminders WHERE id = ?", (rows[0][0],))
                    ai.tool_result_text = f"已删除提醒 #{rows[0][0]}：{rows[0][1]}"
                except Exception:
                    logger.exception("Failed to delete reminder")
                    ai.tool_result_text = "删除提醒失败（数据库错误）"
        else:
            ai.tool_result_text = "请指定要删除的提醒编号或关键词"

        _set_tool_meta(ai, tool_calls)
        ai.user_text = ai.tool_result_text


# ══════════════════════════════════════════════════════════
#  Current Time
# ══════════════════════════════════════════════════════════

class TimeTool:
    def __init__(self, msg_package: Any) -> None:
        self.msg_package = msg_package

    def get_current_time_call(self, robot: Any, ai: Any) -> None:
        from datetime import datetime
        now = datetime.now()
        result = now.strftime("%Y-%m-%d %H:%M:%S")
        ai.ai_text = f"现在是 {result}"
        ai.tool_result_text = f"当前精确时间：{result}"
        _set_tool_meta(ai, ai.ai_message.get("tool_calls"))
        ai.user_text = ai.tool_result_text


# ══════════════════════════════════════════════════════════
#  Balance Query
# ══════════════════════════════════════════════════════════

class BalanceTool:
    def __init__(self, service: Any, msg_package: Any) -> None:
        self.service = service
        self.msg_package = msg_package

    def balance_call(self, robot: Any, ai: Any) -> None:
        try:
            result = self.service.format_balance()
        except Exception:
            logger.exception("Balance query failed")
            result = "查询DeepSeek余额失败"
        _set_tool_meta(ai, ai.ai_message.get("tool_calls"))
        # 直接发原文 —— 余额是数字，让模型复述没有意义还可能读错
        robot.reply(result)


# ══════════════════════════════════════════════════════════
#  Music Search & Playback (点歌)
# ══════════════════════════════════════════════════════════

class MusicTool:
    def __init__(self, music_service: Any, msg_package: Any) -> None:
        self.service = music_service
        self.msg_package = msg_package
        self._recent_songs: dict[str, float] = {}  # dedup_key → timestamp
        self._recent_keywords: dict[str, str] = {}  # dedup_key → last keyword used

    def music_search_call(self, robot: Any, ai: Any) -> None:
        tool_calls = ai.ai_message.get("tool_calls")
        if not tool_calls:
            return

        try:
            args = json.loads(tool_calls[0]["function"].get("arguments", "{}"))
        except (json.JSONDecodeError, TypeError):
            args = {}
        keyword = args.get("keyword", "").strip()

        if not keyword:
            keyword = robot.msg

        from llbot_client import MessageBuilder
        import time as _time

        # Search for the best matching song
        song_info = self.service.search_best(keyword)

        if not song_info:
            robot.reply(f"抱歉，没有找到「{keyword}」的歌曲呢～换一首试试吧 (｡•́︿•̀｡)")
            _set_tool_meta(ai, tool_calls)
            ai.user_text = f"搜索歌曲: {keyword} - 未找到"
            return

        song_id = song_info.get("id", 0)
        artist = song_info.get("artist", "未知歌手")
        name = song_info.get("name", "未知歌曲")
        album = song_info.get("album", "")
        music_type = song_info.get("music_type", "163")

        now = _time.time()
        dedup_key = f"{robot.group_id or robot.user_id}:{song_id}"

        # ── Dedup: skip same song within 120 seconds per chat ──
        last_sent = self._recent_songs.get(dedup_key, 0)
        if now - last_sent < 120:
            logger.info(
                "Music dedup: skipping '%s - %s' (sent %.0fs ago to %s)",
                name, artist, now - last_sent, dedup_key,
            )
            # Try to find an alternative from search results
            alt_songs = self.service.search(keyword, limit=5)
            for alt in alt_songs:
                alt_id = alt.get("id", 0)
                alt_key = f"{robot.group_id or robot.user_id}:{alt_id}"
                if now - self._recent_songs.get(alt_key, 0) >= 120:
                    song_info = alt
                    song_id = alt_id
                    artist = alt.get("artist", "未知歌手")
                    name = alt.get("name", "未知歌曲")
                    album = alt.get("album", "")
                    music_type = alt.get("music_type", "163")
                    dedup_key = alt_key
                    logger.info("Music dedup: using alternative '%s - %s'", name, artist)
                    break
            else:
                # All alternatives also recently sent
                robot.reply(f"「{name} - {artist}」刚放过哦～等一会儿再点吧 (◕‿◕✿)")
                _set_tool_meta(ai, tool_calls)
                ai.user_text = f"已播放歌曲: {name} - {artist}（去重跳过，无可用替代）"
                return

        self._recent_songs[dedup_key] = now
        self._recent_keywords[dedup_key] = keyword

        # Clean up old entries (>5min)
        self._recent_songs = {
            k: v for k, v in self._recent_songs.items() if now - v < 300
        }
        self._recent_keywords = {
            k: v for k, v in self._recent_keywords.items() if k in self._recent_songs
        }

        # ── 1. Send song info text first ──
        info_lines = [f"🎵 {name}", f"👤 {artist}"]
        if album:
            info_lines.append(f"💿 {album}")
        info_lines.append("")

        # ── 2. Send music share card (QQ native music UI) ──
        info_lines.append(f"🎧 正在播放，点击收听")

        info_builder = MessageBuilder()
        info_builder.text("\n".join(info_lines))
        robot.send(info_builder.build())

        # Send the music share card — this renders as a beautiful playable card in QQ
        music_builder = MessageBuilder()
        music_builder.music(music_type, str(song_id))
        robot.send(music_builder.build())

        logger.info("Music shared: %s - %s (id=%s, type=%s)", name, artist, song_id, music_type)

        # ── 3. Try audio download as bonus (best-effort) ──
        try:
            audio_path = self.service.download_audio(
                song_info.get("audio_url", ""), song_id
            )
            if audio_path:
                record_builder = MessageBuilder()
                record_builder.record(audio_path)
                robot.send(record_builder.build())
                logger.info("Audio voice message also sent for %s - %s", name, artist)
        except Exception:
            logger.debug("ai_tools.music_search_call 忽略了异常", exc_info=True)

        _set_tool_meta(ai, tool_calls)
        ai.user_text = f"播放歌曲: {name} - {artist}"


# ══════════════════════════════════════════════════════════
#  Affection — Check affection score
# ══════════════════════════════════════════════════════════

class AffectionTool:
    """SELF_CONTAINED：直接发原文，不经 AI 复述。"""

    def __init__(self, msg_package: Any, database_manager: Any) -> None:
        self.msg_package = msg_package
        self.db = database_manager

    def check_affection_call(self, robot: Any, ai: Any) -> None:
        from affection_service import AffectionService

        tool_calls = ai.ai_message.get("tool_calls")
        _set_tool_meta(ai, tool_calls)

        # Parse target_name
        target_name = ""
        if tool_calls:
            try:
                args = json.loads(tool_calls[0]["function"].get("arguments", "{}"))
                target_name = (args.get("target_name") or "").strip()
            except (json.JSONDecodeError, TypeError):
                logger.debug("ai_tools.check_affection_call 忽略了异常", exc_info=True)

        # Determine target user
        if not target_name or target_name in ("我", "自己", "我的"):
            target_uid = robot.user_id
            target_display = robot.user_name
        else:
            target_uid, target_display = self.db.find_member_by_name(
                robot.group_id or "", target_name,
            )
            if not target_uid:
                robot.reply(f"找不到群友「{target_name}」哦～可能ta还没说过话？")
                return

        # Fetch affection data
        record = AffectionService.get_or_create(
            self.db, target_uid or "", robot.group_id or "", target_display or target_name,
        )
        score = record["affection_score"]
        label, emoji = AffectionService.get_relationship(score)

        is_self = (target_uid == robot.user_id) or (not target_name)

        # 直接发原文：数字类回答让模型复述反而容易被它加工得不准
        lines = [
            f"💕 {target_display} 的好感度",
            f"{score:.0f}/100 {emoji}{label}",
            f"互动 {record['interaction_count']} 次"
            f"（好评 {record['positive_count']} / 差评 {record['negative_count']}）",
        ]
        if is_self and record.get("notes"):
            lines.append(f"备注：{record['notes']}")
        robot.reply("\n".join(lines))


# ══════════════════════════════════════════════════════════
#  Affection Leaderboard
# ══════════════════════════════════════════════════════════

class AffectionLeaderboardTool:
    """SELF_CONTAINED：直接发原文，不经 AI 复述。"""

    def __init__(self, msg_package: Any, database_manager: Any) -> None:
        self.msg_package = msg_package
        self.db = database_manager

    def affection_leaderboard_call(self, robot: Any, ai: Any) -> None:
        from affection_service import AffectionService

        tool_calls = ai.ai_message.get("tool_calls")
        _set_tool_meta(ai, tool_calls)

        board = AffectionService.get_leaderboard(
            self.db, group_id=robot.group_id, limit=10,
        )

        if not board:
            robot.reply("这个群还没有好感度数据哦～多和我聊天互动吧！(◕‿◕✿)")
            return

        lines = [f"📊 {robot.group_name or '本群'} 好感度排行榜 TOP{len(board)}"]
        for i, entry in enumerate(board, 1):
            medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
            lines.append(
                f"{medal} {entry['user_name']} — "
                f"{entry['affection_score']:.0f}分 {entry['emoji']}{entry['relationship']} "
                f"({entry['interaction_count']}次互动)"
            )

        robot.reply("\n".join(lines))


# ══════════════════════════════════════════════════════════
#  Recall (撤回机器人自己的消息)
# ══════════════════════════════════════════════════════════

class RecallMessageTool:
    """FOLLOW_UP tool: recall the bot's own most recent message in this group.

    QQ only lets a member recall their own message for about two minutes, so
    the lookup is bounded by that window instead of failing at the API.
    """

    RECALL_WINDOW = 110  # seconds, comfortably inside QQ's ~2 minute limit

    def __init__(self, database_manager: Any, llbot: Any) -> None:
        self.db = database_manager
        self.llbot = llbot

    def recall_message_call(self, robot: Any, ai: Any) -> None:
        _set_tool_meta(ai, ai.ai_message.get("tool_calls"))

        if robot.msg_type != "group" or not robot.group_id:
            ai.tool_result_text = "只能在群聊里撤回消息。"
            ai.user_text = ai.tool_result_text
            return

        last = self.db.get_last_bot_message(robot.group_id, self.RECALL_WINDOW)
        if not last:
            ai.tool_result_text = (
                "你最近两分钟内没有在这个群发过消息，或者那条已经撤回了，没有可撤回的内容。"
                "直接告诉对方没有可撤回的消息就行，不要假装撤回了。"
            )
            ai.user_text = ai.tool_result_text
            return

        if self.llbot.recall(last["message_id"]):
            self.db.mark_bot_message_recalled(last["message_id"])
            preview = (last["text"] or "").strip()[:20]
            logger.info("Recalled own message %s in group %s", last["message_id"], robot.group_id)
            ai.tool_result_text = (
                f"已成功撤回你刚才发的那条消息（开头是「{preview}」）。"
                "用很自然的语气应一声就好，比如「好啦撤回啦」。"
            )
        else:
            logger.warning("Recall failed for message %s", last["message_id"])
            ai.tool_result_text = (
                "撤回失败了（可能超过了 QQ 的两分钟限制）。"
                "用轻松的语气说明一下就行，不要反复重试。"
            )
        ai.user_text = ai.tool_result_text


# ══════════════════════════════════════════════════════════
#  Group activity stats (单群单日发言统计)
# ══════════════════════════════════════════════════════════

class GroupStatsTool:
    """SELF_CONTAINED：直接发原文，不经 AI 复述。"""

    def __init__(self, database_manager: Any, msg_package: Any) -> None:
        self.db = database_manager
        self.msg_package = msg_package

    def group_stats_call(self, robot: Any, ai: Any) -> None:
        from datetime import datetime, timedelta

        tool_calls = ai.ai_message.get("tool_calls")
        _set_tool_meta(ai, tool_calls)

        args: dict[str, Any] = {}
        if tool_calls:
            try:
                args = json.loads(tool_calls[0]["function"].get("arguments", "{}"))
            except (json.JSONDecodeError, TypeError):
                args = {}

        raw_day = str(args.get("day") or "today").strip().lower()
        today = datetime.now()
        if raw_day in ("today", "今天", ""):
            day = today.strftime("%Y-%m-%d")
        elif raw_day in ("yesterday", "昨天"):
            day = (today - timedelta(days=1)).strftime("%Y-%m-%d")
        else:
            day = raw_day

        if not robot.group_id:
            robot.reply("这个功能只能在群里用。")
            return

        try:
            stats = self.db.get_daily_group_stats(robot.group_id, day)
        except Exception:
            logger.exception("Group stats failed")
            robot.reply("统计查询失败了，稍后再试吧。")
            return

        if not stats["total"]:
            robot.reply(f"{day} 这个群还没有说话记录，今天挺安静的。")
            return

        # 直接发原文：统计是数字，让模型复述反而容易被加工得不准
        lines = [
            f"📊 {day} 发言统计",
            f"总消息：{stats['total']} 条",
            f"活跃人数：{stats['active_users']} 人",
            f"图片/表情：{stats['images']} 条",
        ]
        if stats["top"]:
            lines.append("发言最多：")
            for i, item in enumerate(stats["top"][:5], 1):
                lines.append(f"  {i}. {item['user_name']} — {item['count']} 条")
        peak = max(range(24), key=lambda h: stats["hourly"][h])
        if stats["hourly"][peak]:
            lines.append(
                f"最热闹的时段：{peak:02d}:00 - {peak + 1:02d}:00"
                f"（{stats['hourly'][peak]} 条）"
            )
        robot.reply("\n".join(lines))


# ══════════════════════════════════════════════════════════
#  Read group context (AI 自决获取整体语境)
# ══════════════════════════════════════════════════════════

# Repeat guard. The prompt asks the model to look only when it is genuinely
# lost, but prompts fail — measured on a live group, read_context fired on
# "收到", "[图片消息]" and "你好，死傲娇". Each call dumps a transcript in
# front of the model and the reply then answers the transcript instead of the
# person, so a second look within a couple of minutes gets a *much* smaller
# slice. Keyed per (group, user) because that is the scope of "did I just look
# at this conversation".
_context_seen: dict[tuple[str, str], float] = {}
_context_lock = threading.Lock()
CONTEXT_REPEAT_MINUTES = 3
CONTEXT_REPEAT_LIMIT = 5


def _just_looked(group_id: str, user_id: str) -> bool:
    """True if this user already pulled the transcript very recently."""
    key = (group_id or "", user_id or "")
    now = time.time()
    with _context_lock:
        last = _context_seen.get(key, 0.0)
        _context_seen[key] = now
        # Keep the dict from growing without bound on a busy bot.
        if len(_context_seen) > 500:
            cutoff = now - CONTEXT_REPEAT_MINUTES * 60
            for k in [k for k, v in _context_seen.items() if v < cutoff]:
                _context_seen.pop(k, None)
    return (now - last) < CONTEXT_REPEAT_MINUTES * 60


class ReadContextTool:
    """FOLLOW_UP tool: pull the recent group transcript when the model asks.

    Deliberately tool-driven rather than always-on: attaching a transcript to
    every message would multiply token cost, and the model usually knows when
    it is missing something.
    """

    def __init__(self, database_manager: Any, msg_package: Any) -> None:
        self.db = database_manager
        self.msg_package = msg_package

    def read_context_call(self, robot: Any, ai: Any) -> None:
        tool_calls = ai.ai_message.get("tool_calls")
        _set_tool_meta(ai, tool_calls)

        args: dict[str, Any] = {}
        if tool_calls:
            try:
                args = json.loads(tool_calls[0]["function"].get("arguments", "{}"))
            except (json.JSONDecodeError, TypeError):
                args = {}

        # Small by default. A big transcript does not just cost tokens — it
        # pushes the actual message out of the model's attention, and the
        # replies then answer the transcript instead of the person.
        minutes = args.get("minutes", 15)
        limit = args.get("limit", 20)

        if not robot.group_id:
            ai.tool_result_text = "只有在群里才需要读群聊记录。"
            ai.user_text = ai.tool_result_text
            return

        repeated = _just_looked(robot.group_id or "", robot.user_id)
        if repeated:
            logger.info("read_context 短时间内重复调用，只给最近几条：%s", robot.user_name)
            limit = min(limit, CONTEXT_REPEAT_LIMIT)

        try:
            rows = self.db.get_recent_group_context(
                robot.group_id, minutes=minutes, limit=limit,
                exclude_message_id=robot.incoming.message_id,
            )
        except Exception:
            logger.exception("read_context failed")
            ai.tool_result_text = "读取群聊记录失败了。"
            ai.user_text = ai.tool_result_text
            return

        if not rows:
            ai.tool_result_text = (
                f"最近 {minutes} 分钟群里没有别的消息（当前这条已经排除）。"
                "也就是说这句话没有可供参考的前文——**直接按字面回答，或者问对方指的是什么**，"
                "不要因为查了记录就硬找话说。"
            )
            ai.user_text = ai.tool_result_text
            return

        # The framing comes FIRST as well as last. Without it the model treats
        # the transcript as the thing to answer — which is exactly the "replied
        # to an old message" bug, caused by the tool rather than by history.
        lines = [
            "【以下只是背景，不是要你回应的话。你唯一要回应的是当前这一条消息。】",
            f"本群最近 {minutes} 分钟的聊天记录（已排除当前这条，按时间正序）：",
        ]
        for r in rows:
            hhmm = str(r.get("timestamp") or "")[11:16]
            who = self.db.BOT_DISPLAY_NAME if r.get("is_bot") else r.get("user_name", "?")
            text = " ".join(str(r.get("content") or "").split())
            if len(text) > _CONTEXT_LINE_LIMIT:
                text = text[:_CONTEXT_LINE_LIMIT] + "…"
            lines.append(f"[{hhmm}] {who}：{text}")
        lines.append(
            "【背景到此结束。】以上内容只用来理解当前那句话在说什么："
            "不要回应背景里的任何一条，不要复述，也不要提「我看了聊天记录」。"
            "如果看完还是不知道对方指什么，就直接问，别猜。"
        )
        if repeated:
            lines.append(
                "（你刚刚已经看过更长的版本了，所以这里只给最近几条。"
                "别再查了，直接回答当前这条。）"
            )

        ai.tool_result_text = "\n".join(lines)
        ai.user_text = ai.tool_result_text


# ══════════════════════════════════════════════════════════
#  Explain self (调用链 + 思维链)
# ══════════════════════════════════════════════════════════

class ExplainSelfTool:
    """DEBUG tool: dump the PREVIOUS reply's raw record straight into the chat.

    The current turn is not saved until after the reply is sent, so the newest
    assistant row in `history` is exactly the message the user is asking about.

    This is a *debugging* aid, so it deliberately bypasses the model: feeding
    the chain back through the AI made it re-tell its own thoughts in its own
    words, which is a lossy second pass over the very text we are trying to
    inspect. Instead the reasoning is reproduced verbatim — newlines kept, no
    summarising, no rewording — and sent directly, with no follow-up turn.
    """

    MAX_TOTAL = 8000      # matches the history.reasoning storage cap
    CHUNK = 1200          # keep each QQ text segment comfortably small

    def __init__(self, database_manager: Any, msg_package: Any) -> None:
        self.db = database_manager
        self.msg_package = msg_package

    def explain_self_call(self, robot: Any, ai: Any) -> None:
        _set_tool_meta(ai, ai.ai_message.get("tool_calls"))

        group_id = robot.group_id if robot.msg_type == "group" else None
        try:
            last = self.db.get_last_bot_turn(robot.user_id, group_id)
        except Exception:
            logger.exception("explain_self lookup failed")
            last = None

        if not last:
            self._send(robot, "【执行回放】还没有上一轮的记录，无从查阅。")
            ai.tool_result_text = "已直接告知用户没有上一轮记录。本轮不要再回复任何内容。"
            ai.user_text = ai.tool_result_text
            return

        self._send(robot, self._render(last))
        # Self-contained: main_logic sends no follow-up, so the model never
        # gets a chance to paraphrase what we just dumped.
        ai.tool_result_text = (
            "已把上一轮的原始记录直接发到对话里（原文照录）。"
            "本轮不要再说任何话，也不要复述其中的内容。"
        )
        ai.user_text = ai.tool_result_text

    def _render(self, last: dict[str, Any]) -> str:
        lines = ["【上一轮原始记录 · 调试输出】"]
        if last.get("timestamp"):
            lines.append(f"时间：{last['timestamp']}")

        reasoning = (last.get("reasoning") or "").strip()
        lines.append("")
        lines.append("── 思维链原文 ──")
        lines.append(reasoning or "（这一轮没有思维链：思考模式可能被关掉了）")

        chain: list[dict[str, Any]] = []
        if last.get("tool_calls"):
            try:
                chain = json.loads(last["tool_calls"])
            except (json.JSONDecodeError, TypeError):
                chain = []
        lines.append("")
        lines.append("── 工具调用 ──")
        if chain:
            for i, c in enumerate(chain, 1):
                name = c.get("name", "?")
                args = (c.get("arguments") or "").strip()
                lines.append(f"{i}. {name}({args})")
        else:
            lines.append("（无，直接回答的）")

        reply = (last.get("content") or "").strip()
        lines.append("")
        lines.append("── 最终回复 ──")
        lines.append(reply or "（空）")

        text = "\n".join(lines)
        if len(text) > self.MAX_TOTAL:
            text = text[:self.MAX_TOTAL] + "\n…（超出 8000 字，已截断）"
        return text

    def _split(self, text: str) -> list[str]:
        """Chunk on line boundaries so raw reasoning stays readable.

        A thinking chain is often one enormous unbroken paragraph, so lines
        longer than CHUNK are hard-wrapped too — otherwise the whole point of
        chunking (QQ rejects oversized text segments) is lost.
        """
        chunks: list[str] = []
        current = ""
        for line in text.split("\n"):
            while len(line) > self.CHUNK:
                if current:
                    chunks.append(current)
                    current = ""
                chunks.append(line[:self.CHUNK])
                line = line[self.CHUNK:]
            candidate = line if not current else current + "\n" + line
            if len(candidate) > self.CHUNK:
                chunks.append(current)
                current = line
            else:
                current = candidate
        if current:
            chunks.append(current)
        return chunks or [text]

    def _send(self, robot: Any, text: str) -> None:
        from llbot_client import MessageBuilder

        chunks = self._split(text)
        total = len(chunks)
        for idx, chunk in enumerate(chunks, 1):
            body = f"({idx}/{total})\n{chunk}" if total > 1 else chunk
            builder = MessageBuilder()
            # Quote the request once so the dump is anchored in a busy group.
            if idx == 1 and robot.incoming.message_id:
                builder.reply(robot.incoming.message_id)
            builder.text(body)
            try:
                robot.send(builder.build())
            except Exception:
                logger.exception("explain_self send failed")
                return


# ══════════════════════════════════════════════════════════
#  Voice (直接说话，而不是打字)
# ══════════════════════════════════════════════════════════

class VoiceTool:
    """SELF-CONTAINED tool: speak the reply with QQ's AI voice.

    The model decides whether speaking fits the moment and which timbre to use.
    It is self-contained because the voice *is* the reply — a follow-up turn
    would only add a typed duplicate on top of it.

    Character ids are validated against `get_ai_characters` rather than trusted:
    the endpoint happily accepts an unknown id and then silently fails to
    deliver, which would look like the bot ignoring people.
    """

    def __init__(self, database_manager: Any, msg_package: Any, llbot: Any = None) -> None:
        self.db = database_manager
        self.msg_package = msg_package
        self.llbot = llbot

    def voice_call(self, robot: Any, ai: Any) -> None:
        tool_calls = ai.ai_message.get("tool_calls")
        _set_tool_meta(ai, tool_calls)

        args: dict[str, Any] = {}
        if tool_calls:
            try:
                args = json.loads(tool_calls[0]["function"].get("arguments", "{}"))
            except (json.JSONDecodeError, TypeError):
                args = {}

        text = str(args.get("text") or "").strip()
        character = str(args.get("voice") or "").strip() or Config.VOICE_DEFAULT_CHARACTER

        if robot.msg_type != "group" or not robot.group_id:
            ai.tool_result_text = (
                "私聊里发不了语音（这个功能只支持群）。"
                "直接用文字把刚才想说的话说出来即可，不要提这件事。"
            )
            ai.user_text = ai.tool_result_text
            return

        if not text:
            ai.tool_result_text = "要说的内容为空，没发出去。用文字回答即可。"
            ai.user_text = ai.tool_result_text
            return

        character = self._validated_character(character)

        try:
            ok = bool(self.llbot and self.llbot.send_ai_voice(
                robot.group_id, character, text))
        except Exception:
            logger.exception("voice send failed")
            ok = False

        if ok:
            logger.info("语音已发送（%s）：%s", character, text[:40])
            ai.tool_result_text = (
                "语音已经发出去了，本轮不要再打字重复一遍，也不要说明你发了语音。"
            )
        else:
            logger.info("语音发送失败，回退文字：%s", text[:40])
            # Falling back to text is the whole point of not trusting the API:
            # a silent failure would look like the bot ignoring the message.
            ai.tool_result_text = (
                "语音没发出去（功能不可用）。请直接用文字把刚才那句话正常说出来，"
                "不要提语音、也不要道歉。"
            )
        ai.user_text = ai.tool_result_text

    def _validated_character(self, wanted: str) -> str:
        """Return `wanted` if QQ offers it, else the configured default."""
        try:
            available = {c["id"] for c in (self.llbot.get_ai_characters() if self.llbot else [])}
        except Exception:
            logger.debug("character list unavailable", exc_info=True)
            return wanted or Config.VOICE_DEFAULT_CHARACTER
        if not available:
            return wanted or Config.VOICE_DEFAULT_CHARACTER
        if wanted in available:
            return wanted
        logger.info("未知音色 %r，回退默认", wanted)
        if Config.VOICE_DEFAULT_CHARACTER in available:
            return Config.VOICE_DEFAULT_CHARACTER
        return sorted(available)[0]

