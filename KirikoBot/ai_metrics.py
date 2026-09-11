"""Instrumentation for every DeepSeek call: latency, tokens, outcome, cost.

The dashboard needs to answer "where do the tokens and the seconds actually
go" — `tool_usage` only counts tool invocations, with no latency or outcome
dimension.

Design: a module-level sink set by ``main`` (mirroring ``LLBotClient``'s
recorder), so ``ai_server`` never imports the database and stays usable from
scripts. Every entry point is defensive — metrics must never break a reply.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable

from config import Config

logger = logging.getLogger(__name__)

_sink: Callable[[dict[str, Any]], None] | None = None


def set_sink(fn: Callable[[dict[str, Any]], None] | None) -> None:
    """Register where recorded calls go (main wires this to the database)."""
    global _sink
    _sink = fn


def extract_usage(data: dict[str, Any] | None) -> dict[str, int]:
    """Normalise the `usage` block into flat token counters.

    DeepSeek reports cache accounting as prompt_cache_hit/miss_tokens; older
    shapes only carry prompt_tokens_details.cached_tokens, so both are handled.
    """
    usage = (data or {}).get("usage") or {}
    details = usage.get("completion_tokens_details") or {}
    prompt = int(usage.get("prompt_tokens") or 0)

    hit = usage.get("prompt_cache_hit_tokens")
    miss = usage.get("prompt_cache_miss_tokens")
    if hit is None and miss is None:
        cached = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        hit, miss = cached, max(0, prompt - cached)

    return {
        "prompt_tokens": prompt,
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "reasoning_tokens": int(details.get("reasoning_tokens") or 0),
        "cache_hit_tokens": int(hit or 0),
        "cache_miss_tokens": int(miss or 0),
    }


def record(
    *,
    source: str,
    kind: str = "chat",
    model: str = "",
    latency_ms: int = 0,
    usage: dict[str, Any] | None = None,
    success: bool = True,
    error: str = "",
    group_id: str = "",
) -> None:
    """Record one API call. Silently does nothing when no sink is wired."""
    if _sink is None:
        return
    try:
        entry: dict[str, Any] = {
            "source": source or "unknown",
            "kind": kind,
            "model": model or Config.DEEPSEEK_MODEL,
            "group_id": str(group_id or ""),
            "latency_ms": max(0, int(latency_ms)),
            "success": 1 if success else 0,
            "error": str(error)[:200],
            # UTC hour/weekday decide peak vs off-peak pricing; storing them
            # avoids re-deriving timezone shifts at query time.
            "utc_hour": time.gmtime().tm_hour,
            "utc_weekday": time.gmtime().tm_wday,
        }
        entry.update(extract_usage(usage))
        _sink(entry)
    except Exception:
        logger.debug("ai_metrics.record failed", exc_info=True)


class timed:
    """Context manager: wall-clock the block and record the call.

        with ai_metrics.timed(source="judge") as t:
            resp = requests.post(...)
            t.usage = resp.json()
    """

    def __init__(self, *, source: str, kind: str = "chat", model: str = "",
                 group_id: str = "") -> None:
        self.source = source
        self.kind = kind
        self.model = model
        self.group_id = group_id
        self.usage: dict[str, Any] | None = None
        self.error = ""
        self.success = True
        self._t0 = 0.0

    def __enter__(self) -> "timed":
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is not None:
            self.success = False
            self.error = f"{exc_type.__name__}: {exc}" if exc_type else "error"
        record(
            source=self.source,
            kind=self.kind,
            model=self.model,
            group_id=self.group_id,
            latency_ms=(time.perf_counter() - self._t0) * 1000,
            usage=self.usage,
            success=self.success,
            error=self.error,
        )
        return False  # never swallow the exception
