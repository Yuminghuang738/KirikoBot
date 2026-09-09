"""Per-group / per-user feature toggles.

Every feature defaults to ON. Settings are stored in the `feature_settings`
DB table; `settings_json` only contains DISABLED keys ({"weather": false}).
Missing row / missing key → enabled. Scope: ('group', group_id) for group
chats, ('user', user_id) for private chats.
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Feature registry — shared by gate logic and the dashboard UI.
# Order = display order on the settings page.
FEATURE_DEFS: list[dict[str, str]] = [
    {"key": "morning_news",    "label": "早间新闻推送", "category": "推送",     "desc": "每天 7:00 推送时政/游戏新闻和每日一言"},
    {"key": "affection",       "label": "好感度系统",   "category": "互动系统", "desc": "记录与 Kiriko 的好感度，影响回复语气"},
    {"key": "profiles",        "label": "用户画像",     "category": "互动系统", "desc": "分析群友发言生成人格画像"},
    {"key": "learning",        "label": "学习系统",     "category": "互动系统", "desc": "记录对话经验并自我改进"},
    {"key": "vision",          "label": "图像识别",     "category": "视觉",     "desc": "理解表情包/图片内容并生成回复"},
    {"key": "sticker_battle",  "label": "斗图模式",     "category": "视觉",     "desc": "表情包对战玩法"},
    {"key": "sticker",         "label": "表情包发送",   "category": "视觉",     "desc": "聊天中发送表情包"},
    {"key": "sticker_collect", "label": "表情包采集",   "category": "视觉",     "desc": "自动收藏群友发的表情包"},
    {"key": "music",           "label": "音乐点歌",     "category": "生活娱乐", "desc": "点歌/音乐分享卡片"},
    {"key": "weather",         "label": "天气查询",     "category": "信息查询", "desc": "查询城市天气"},
    {"key": "tarot",           "label": "塔罗占卜",     "category": "生活娱乐", "desc": "塔罗抽牌和解牌"},
    {"key": "web_search",      "label": "联网搜索",     "category": "信息查询", "desc": "联网搜索网页内容"},
    {"key": "political_news",  "label": "时政新闻",     "category": "信息查询", "desc": "时政新闻查询"},
    {"key": "gaming_news",     "label": "游戏新闻",     "category": "信息查询", "desc": "游戏新闻查询"},
    {"key": "bilibili",        "label": "B站热搜",      "category": "信息查询", "desc": "B站热搜榜"},
    {"key": "hitokoto",        "label": "一言",         "category": "生活娱乐", "desc": "随机一言/名言"},
    {"key": "food",            "label": "吃什么",       "category": "生活娱乐", "desc": "随机推荐吃什么"},
    {"key": "dice",            "label": "骰子",         "category": "生活娱乐", "desc": "掷骰子/随机数"},
    {"key": "reminder",        "label": "提醒",         "category": "群功能",   "desc": "设置/查询/删除提醒"},
    {"key": "at_member",       "label": "@群友",        "category": "群功能",   "desc": "帮用户@群友"},
    {"key": "balance",         "label": "余额查询",     "category": "群功能",   "desc": "查询 AI 服务余额"},
    {"key": "feature_request", "label": "功能建议",     "category": "群功能",   "desc": "向开发者提交功能建议"},
]

FEATURE_KEYS: list[str] = [f["key"] for f in FEATURE_DEFS]
FEATURE_LABELS: dict[str, str] = {f["key"]: f["label"] for f in FEATURE_DEFS}
VALID_SCOPES: tuple[str, ...] = ("group", "user")

# Tool name → feature key. Tools not listed here are always available.
TOOL_FEATURE: dict[str, str] = {
    "check_affection": "affection",
    "affection_leaderboard": "affection",
    "set_reminder": "reminder",
    "list_reminders": "reminder",
    "delete_reminder": "reminder",
    "tarot": "tarot",
    "tarot_history": "tarot",
    "gaming_news": "gaming_news",
    "political_news": "political_news",
    "bilibili_trending": "bilibili",
    "web_search": "web_search",
    "weather": "weather",
    "music_search": "music",
    "sticker": "sticker",
    "sticker_battle": "sticker_battle",
    "hitokoto": "hitokoto",
    "food_picker": "food",
    "dice": "dice",
    "at_member": "at_member",
    "check_balance": "balance",
    "submit_feature": "feature_request",
}


def disabled_tool_names(disabled_keys: set[str]) -> set[str]:
    """Map disabled feature keys → disabled tool names for _filter_tools."""
    return {tool for tool, feat in TOOL_FEATURE.items() if feat in disabled_keys}


def disabled_labels(disabled_keys: set[str]) -> list[str]:
    """Human-readable labels of disabled features, in registry order."""
    return [f["label"] for f in FEATURE_DEFS if f["key"] in disabled_keys]


class FeatureGate:
    """Reads/writes per-scope feature settings. Never raises on the chat path."""

    def __init__(self, db: Any) -> None:
        self.db = db

    @staticmethod
    def scope_of(robot: Any) -> tuple[str, str]:
        """Effective scope for a message: group chat → group, private → user."""
        if getattr(robot, "msg_type", "") == "group" and getattr(robot, "group_id", ""):
            return ("group", str(robot.group_id))
        return ("user", str(robot.user_id))

    def _raw_json(self, scope_type: str, scope_id: str) -> dict[str, Any]:
        rows = self.db.fetch_data(
            "SELECT settings_json FROM feature_settings WHERE scope_type=? AND scope_id=?",
            (scope_type, scope_id),
        )
        if not rows:
            return {}
        try:
            return json.loads(rows[0][0] or "{}")
        except (json.JSONDecodeError, ValueError):
            return {}

    def disabled_keys(self, scope_type: str, scope_id: str) -> set[str]:
        try:
            return {k for k, v in self._raw_json(scope_type, scope_id).items() if not v}
        except Exception:
            logger.exception("Failed to read feature settings for %s:%s", scope_type, scope_id)
            return set()

    def is_enabled(self, scope_type: str, scope_id: str, key: str) -> bool:
        return key not in self.disabled_keys(scope_type, scope_id)

    def effective_map(self, scope_type: str, scope_id: str) -> dict[str, bool]:
        disabled = self.disabled_keys(scope_type, scope_id)
        return {k: k not in disabled for k in FEATURE_KEYS}

    def set_enabled(self, scope_type: str, scope_id: str, key: str, enabled: bool) -> dict[str, bool]:
        if scope_type not in VALID_SCOPES:
            raise ValueError(f"invalid scope_type: {scope_type}")
        if key not in FEATURE_KEYS:
            raise ValueError(f"unknown feature key: {key}")
        data = self._raw_json(scope_type, scope_id)
        if enabled:
            data.pop(key, None)
        else:
            data[key] = False
        if data:
            self.db.execute_action(
                "INSERT INTO feature_settings(scope_type, scope_id, settings_json) "
                "VALUES(?,?,?) "
                "ON CONFLICT(scope_type, scope_id) DO UPDATE SET "
                "settings_json=excluded.settings_json, updated_at=datetime('now','localtime')",
                (scope_type, scope_id, json.dumps(data, ensure_ascii=False)),
            )
        else:
            self.db.execute_action(
                "DELETE FROM feature_settings WHERE scope_type=? AND scope_id=?",
                (scope_type, scope_id),
            )
        return self.effective_map(scope_type, scope_id)

    def reset(self, scope_type: str, scope_id: str) -> dict[str, bool]:
        self.db.execute_action(
            "DELETE FROM feature_settings WHERE scope_type=? AND scope_id=?",
            (scope_type, scope_id),
        )
        return self.effective_map(scope_type, scope_id)
