"""移除的功能不许长回来。

2026-10 精简了一批使用率低、或与 AI 本身能力重合的功能：

    掷骰子 / 吃什么 / 应要求找表情 / 表情对战 / 找库里最像的一张 / 功能建议

顺带修掉一处分层问题：设置页里的两个「推送」开关，一个（群推送订阅）是推送页
所有订阅项的总开关、与推送页重合；另一个（早间新闻推送）管的是一套**遗留的固定
7:00 早报**，而推送页里早就有「早间新闻」订阅项 —— 两者会各发一次。现在推送只由
推送页的订阅项控制。

这个文件把「已经删干净」钉住 —— 尤其是三处容易漏的地方：给 AI 的工具清单、
路由表、以及各群独立的功能开关清单。删了实现却留着清单，模型照样会去调一个
已经不存在的工具。
"""
from __future__ import annotations

import os

import pytest

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "KirikoBot")

REMOVED_TOOLS = (
    # 2026-10 第一批：使用率低 / 与 AI 能力重合
    "dice", "food_picker", "request_sticker",
    "sticker_battle", "similar_sticker",
    "submit_feature", "feature_list",
    # 2026-10 第二批：天气、B站热搜、一言
    # —— 这三个要么 AI 自己就能答，要么使用率太低
    "weather", "bilibili_trending", "hitokoto",
)
# 这些 feature key 已随功能/重复的推送开关一起移除
REMOVED_FEATURE_KEYS = (
    "dice", "food", "sticker_battle", "similar_sticker",
    "feature_request", "feature_list",
    "morning_news",   # 遗留的固定 7:00 早报
    "subscription",   # 与推送页重合的总开关
    "weather", "bilibili", "hitokoto",   # 对应工具已移除
)


class TestToolsAreGone:
    def test_not_in_the_schema_handed_to_the_model(self):
        from ai_tools_list import AiTools

        names = {t["function"]["name"] for t in AiTools().ai_tools()}
        for tool in REMOVED_TOOLS:
            assert tool not in names, f"{tool} 还在给 AI 的工具清单里"

    def test_not_in_the_route_table(self):
        """清单里删了、路由表里还留着，会变成「调了但没人接」。"""
        import main

        for tool in REMOVED_TOOLS:
            assert tool not in main.ROUTES, f"{tool} 还在 ROUTES 里"

    def test_not_declared_self_contained(self):
        import main

        for tool in REMOVED_TOOLS:
            assert tool not in main.SELF_CONTAINED_TOOLS

    def test_the_tool_classes_are_gone(self):
        import ai_tools

        for cls in ("DiceTool", "FoodPickerTool", "StickerBattleTool",
                    "SimilarStickerTool", "FeatureRequestTool", "FeatureListTool",
                    "WeatherTool", "BilibiliTool", "HitokotoTool"):
            assert not hasattr(ai_tools, cls), f"{cls} 还在 ai_tools 里"

    def test_the_battle_state_is_gone(self):
        import main

        for attr in ("_battle_state", "_sticker_pending", "_request_sticker_call"):
            assert not hasattr(main, attr), f"{attr} 还留在 main 里"


class TestFeatureRegistryIsClean:
    def test_removed_keys_are_not_offerable(self):
        """设置页的开关清单来自这里，漏一个就会在 UI 上冒出来。"""
        from feature_gate import FEATURE_KEYS

        for key in REMOVED_FEATURE_KEYS:
            assert key not in FEATURE_KEYS, f"{key} 还能在设置页里开关"

    def test_there_is_no_push_category_left(self):
        """推送不再出现在设置页 —— 只由推送页的订阅项控制。"""
        from feature_gate import FEATURE_DEFS

        assert not [f for f in FEATURE_DEFS if f["category"] == "推送"]

    def test_removed_tools_are_not_gated(self):
        from feature_gate import TOOL_FEATURE

        for tool in REMOVED_TOOLS:
            assert tool not in TOOL_FEATURE, f"{tool} 还挂在功能开关上"

    def test_vision_keeps_its_own_switch(self):
        """`request_sticker` 曾经挂在 `vision` 上；移除工具不能把图像识别也带走。"""
        from feature_gate import FEATURE_KEYS

        assert "vision" in FEATURE_KEYS


class TestTheWholePushSystemIsGone:
    """群订阅推送**整体移除**了 —— 机器人不再主动往群里推任何东西。

    移除的是这一整套：订阅表 / 主题常量 / 调度器里的推送逻辑 / API / 前端页面。
    早报、游戏速递、一言、发言榜、箱头推荐这几个「主题」全都跟着没了。
    留下的只有**用户自己设的提醒**。
    """

    def test_the_scheduler_has_no_push_machinery(self):
        import scheduler

        for attr in ("_check_subscriptions", "_push_topic", "_minutes_late",
                     "_build_morning_lines", "_build_hitokoto",
                     "_build_amp_head", "_build_roll_call", "_format_titles"):
            assert not hasattr(scheduler.BotScheduler, attr), f"{attr} 还在调度器里"

    def test_the_subscription_table_and_api_are_gone(self):
        from database_manager import DatabaseManager
        import main

        assert not hasattr(DatabaseManager, "SUBSCRIPTION_TOPICS")
        for attr in ("get_subscriptions", "set_subscription", "delete_subscription",
                     "due_subscriptions", "mark_subscription_fired"):
            assert not hasattr(DatabaseManager, attr), f"{attr} 还在"
        rules = {str(r) for r in main.app.url_map.iter_rules()}
        assert not any("/api/subscriptions" in r for r in rules), "订阅接口还在"

    def test_the_table_is_dropped_on_startup(self, db):
        """光删代码不够，数据也要清 —— 否则表里还留着各群的订阅配置。"""
        rows = db.fetch_data(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='group_subscriptions'"
        )
        assert rows == []

    def test_reminders_survive(self):
        """提醒不是「推送」—— 它是用户自己设的，必须留着。"""
        import scheduler

        assert hasattr(scheduler.BotScheduler, "_check_reminders")
        assert hasattr(scheduler.BotScheduler, "_fire_reminder")


class TestVersionManagementIsGone:
    """版本管理 + 版本推送整体废弃：没有任何东西再往里写，留着只是负担。"""

    def test_the_module_is_deleted(self):
        import importlib

        try:
            importlib.import_module("version_manager")
        except ModuleNotFoundError:
            return
        raise AssertionError("version_manager 还在")

    def test_the_tables_are_dropped_on_startup(self, db):
        for table in ("app_versions", "changelog"):
            rows = db.fetch_data(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
            )
            assert rows == [], f"{table} 还在"

    def test_the_api_and_pages_are_gone(self):
        import main

        rules = {str(r) for r in main.app.url_map.iter_rules()}
        for path in ("/api/versions", "/api/changelog", "/api/digest/push"):
            assert not any(path in r for r in rules), f"{path} 还在"

        html = open("KirikoBot/templates/dashboard.html", encoding="utf-8").read()
        assert 'data-page="versions"' not in html
        assert 'data-page="push"' not in html

    def test_no_version_file(self):
        import os

        assert not os.path.exists("KirikoBot/VERSION")


class TestFeatureRequestChainIsGone:
    def test_no_api_endpoints(self):
        import main

        rules = {str(r) for r in main.app.url_map.iter_rules()}
        assert not any("/api/features" in r for r in rules), "功能建议的接口还在"

    def test_no_db_table_or_accessor(self):
        import database_manager
        from database_manager import DatabaseManager

        assert not hasattr(DatabaseManager, "get_feature_requests")
        # VALID_TABLES 是模块级常量，不是类属性
        assert "feature_requests" not in database_manager.VALID_TABLES

    def test_the_frontend_page_is_gone(self):
        """整条链路一起移除，前端那页也删了。"""
        js = open(os.path.join(APP_DIR, "static", "js", "app.js"), encoding="utf-8").read()
        html = open(os.path.join(APP_DIR, "templates", "dashboard.html"), encoding="utf-8").read()
        for gone in ("featuresHTML", "bindFeatures", "addFeature"):
            assert gone not in js, f"{gone} 还在 app.js 里"
        assert 'data-page="features"' not in html
        assert "api/features" not in js


class TestPushPageIsTheSingleControl:
    def test_the_page_no_longer_points_at_a_master_switch(self):
        """原来的说明让用户去设置页找总开关，那个开关已经没了。"""
        js = open(os.path.join(APP_DIR, "static", "js", "app.js"), encoding="utf-8").read()
        assert "群设置 → 群推送订阅" not in js


class TestDataToolsAnswerDirectly:
    """统计 / 余额 / 好感度这类**数字型**回答直接发原文，不经 AI 复述。

    理由：让模型复述数字没有任何收益，还可能把数读错、把排行顺序讲乱。
    这类工具改成 SELF_CONTAINED 之后，自己把结果发出去，模型不再参与。
    """

    DATA_TOOLS = {
        "GroupStatsTool": "group_stats_call",
        "BalanceTool": "balance_call",
        "AffectionTool": "check_affection_call",
        "AffectionLeaderboardTool": "affection_leaderboard_call",
    }

    def test_they_are_declared_self_contained(self):
        """不在这个集合里就会走「工具返回 → 模型再复述一遍」那条路。"""
        import main

        for tool in ("group_stats", "check_balance",
                     "check_affection", "affection_leaderboard"):
            assert tool in main.SELF_CONTAINED_TOOLS, f"{tool} 还是跟进式"

    def test_they_send_their_own_message(self):
        import inspect

        import ai_tools

        for cls_name, method in self.DATA_TOOLS.items():
            src = inspect.getsource(getattr(getattr(ai_tools, cls_name), method))
            assert "robot.reply" in src or "robot.send" in src, f"{cls_name} 没自己发消息"
            assert "tool_result_text" not in src, \
                f"{cls_name}.{method} 还在把结果交给模型加工"

    def test_the_removed_services_are_not_instantiated(self):
        """天气 / B站热搜的服务只被工具用，工具没了就该停止实例化。

        一言是例外：定时推送的「一言」订阅项还要用它，所以服务保留。
        """
        import main

        for name in ("weather_tool", "bilibili_tool", "hitokoto_tool"):
            assert not hasattr(main, name), f"{name} 还在"
        # 一言推送移除后，这个服务也没有调用方了
        assert not hasattr(main, "hitokoto_service"), "一言推送已经没了，服务不该还留着"
