"""回复频率限制：按群、按用户两个维度。

两种模式，各自独立开关，**可以同时开**：

  · **单用户模式** —— N 分钟内对**同一个用户**最多回 Y 条。
    对所有用户一视同仁，不是给某个人单独设。
  · **群聊模式**   —— N 分钟内在这个群最多发 Y 条。

两个都开时，任何一条不满足就不发。超额时**静默丢弃**（不回复「你太频繁了」），
因为那条提示本身也会占额度、而且会把屏幕刷得更乱；日志里会记下是谁被限的。

设计上的取舍：
- 计数器放在内存里（滑动窗口）。重启后清零 —— 频率限制本来就是短时约束，
  没必要落盘，也不值得为它多写一次磁盘。
- 群配置读 DB，但缓存 `CONFIG_TTL` 秒。不然每条消息都要查一次库。
- 私聊不参与限流：这个功能是给群聊设计的，`group_id` 为空时直接放行。
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any

logger = logging.getLogger(__name__)

# 默认值：平时够用，真需要限制时在面板里调
DEFAULTS = {
    "user_enabled": False,
    "user_window_min": 10,
    "user_max": 20,
    "group_enabled": False,
    "group_window_min": 10,
    "group_max": 30,
}

LIMITS = {                      # 字段 → (最小, 最大)
    "user_window_min": (1, 1440),
    "user_max": (1, 1000),
    "group_window_min": (1, 1440),
    "group_max": (1, 1000),
}

CONFIG_TTL = 30.0               # 配置缓存秒数
_PRUNE_EVERY = 2000             # 每判定这么多次清理一次空桶


class RateLimiter:
    """滑动窗口限流器。线程安全。"""

    def __init__(self, db: Any, clock: Any = None) -> None:
        self.db = db
        # 时钟可注入：测试里要能把时间往前拨，验证窗口过期
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self._user: dict[tuple[str, str], deque[float]] = {}
        self._group: dict[str, deque[float]] = {}
        self._cfg: dict[str, tuple[float, dict[str, Any]]] = {}
        self._calls = 0

    # ── 配置 ─────────────────────────────────────────────

    def config_for(self, group_id: str) -> dict[str, Any]:
        """这个群生效的限流配置（带短缓存）。"""
        now = self._clock()
        hit = self._cfg.get(group_id)
        if hit and now - hit[0] < CONFIG_TTL:
            return hit[1]
        try:
            cfg = self.db.get_rate_limit(group_id) or {}
        except Exception:
            # 读不到配置就**放行** —— 限流是保护措施，不该因为它坏了就堵住聊天
            logger.debug("读取限流配置失败，按不限流处理", exc_info=True)
            cfg = {}
        merged = dict(DEFAULTS)
        merged.update(cfg)
        self._cfg[group_id] = (now, merged)
        return merged

    def invalidate(self, group_id: str | None = None) -> None:
        """配置改了之后调一下，别等 TTL 过期。"""
        with self._lock:
            if group_id is None:
                self._cfg.clear()
            else:
                self._cfg.pop(group_id, None)

    # ── 判定 ─────────────────────────────────────────────

    def allow(self, group_id: str, user_id: str) -> bool:
        """这条能不能发？能发就顺手记一笔额度。"""
        if not group_id:
            return True                      # 私聊不限
        cfg = self.config_for(group_id)
        user_on = bool(cfg.get("user_enabled"))
        group_on = bool(cfg.get("group_enabled"))
        if not user_on and not group_on:
            return True

        now = self._clock()
        with self._lock:
            self._calls += 1
            if self._calls % _PRUNE_EVERY == 0:
                self._prune(now, cfg)

            if group_on:
                win = max(1, int(cfg["group_window_min"])) * 60
                bucket = self._group.setdefault(group_id, deque())
                self._trim(bucket, now, win)
                if len(bucket) >= max(1, int(cfg["group_max"])):
                    logger.info(
                        "限流命中（群）：群 %s 在 %d 分钟内已发 %d 条，上限 %d",
                        group_id, cfg["group_window_min"], len(bucket), cfg["group_max"],
                    )
                    return False

            if user_on:
                win = max(1, int(cfg["user_window_min"])) * 60
                key = (group_id, user_id)
                bucket = self._user.setdefault(key, deque())
                self._trim(bucket, now, win)
                if len(bucket) >= max(1, int(cfg["user_max"])):
                    logger.info(
                        "限流命中（用户）：%s 在群 %s 的 %d 分钟内已回 %d 条，上限 %d",
                        user_id, group_id, cfg["user_window_min"], len(bucket), cfg["user_max"],
                    )
                    return False

            # 通过 —— 两个桶都记一笔
            if group_on:
                self._group[group_id].append(now)
            if user_on:
                self._user[(group_id, user_id)].append(now)
            return True

    @staticmethod
    def _trim(bucket: deque[float], now: float, window: float) -> None:
        """把窗口外的记录挤掉。"""
        while bucket and now - bucket[0] > window:
            bucket.popleft()

    def _prune(self, now: float, cfg: dict[str, Any]) -> None:
        """清掉过期的空桶，避免字典无限长。调用方已持锁。"""
        for key in [k for k, v in self._user.items() if not v]:
            del self._user[key]
        for key in [k for k, v in self._group.items() if not v]:
            del self._group[key]

    # ── 观测（面板/调试用） ──────────────────────────────

    def snapshot(self) -> dict[str, Any]:
        """当前各桶的用量，方便排查「为什么被限了」。"""
        now = self._clock()
        with self._lock:
            return {
                "groups": {g: len(q) for g, q in self._group.items() if q},
                "users": {f"{g}:{u}": len(q) for (g, u), q in self._user.items() if q},
                "cached_configs": len(self._cfg),
                "now": now,
            }
