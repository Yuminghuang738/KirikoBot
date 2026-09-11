from __future__ import annotations

import html
import logging
import queue
import threading
import time
from typing import Any


class SSELogHandler(logging.Handler):
    """Pushes log records to an in-memory queue for SSE streaming."""

    MAX_QUEUE = 500

    def __init__(self) -> None:
        super().__init__()
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=self.MAX_QUEUE)
        self.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        ))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = {
                "time": self.format(record),    # e.g. "12:48:37"
                "level": record.levelname,       # INFO/WARNING/ERROR
                "name": record.name,             # logger name ("think", "main", etc.)
                "msg": record.getMessage(),      # actual log message
            }
            # `display` is injected into the dashboard with innerHTML, and log
            # messages routinely carry user-controlled text (QQ nicknames,
            # message bodies, model output). Escape it or a nickname like
            # `<img src=x onerror=...>` becomes stored XSS in the admin panel.
            msg = html.escape(entry["msg"], quote=False)
            ts = html.escape(entry["time"], quote=False)
            # Precomputed display line: time + level tag + name tag + msg
            if record.name == "think":
                entry["display"] = f'<span class="ts">{ts}</span><b class="think-tag">🧠思维链</b> {msg}'
            elif record.levelname == "ERROR":
                entry["display"] = f'<span class="ts">{ts}</span><b class="err-tag">ERR</b> {msg}'
            elif record.levelname == "WARNING":
                entry["display"] = f'<span class="ts">{ts}</span><b class="warn-tag">WARN</b> {msg}'
            else:
                entry["display"] = f'<span class="ts">{ts}</span>{msg}'
            # Non-blocking put; drop oldest if full
            while True:
                try:
                    self._queue.put_nowait(entry)
                    break
                except queue.Full:
                    try:
                        self._queue.get_nowait()
                    except queue.Empty:
                        break
        except Exception:
            pass

    def read(self) -> list[dict[str, Any]]:
        """Drain all pending log entries."""
        entries: list[dict[str, Any]] = []
        while True:
            try:
                entries.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return entries


# Global SSE handler singleton
sse_handler = SSELogHandler()


def setup_sse_logging() -> None:
    """Attach SSE handler to root logger."""
    root = logging.getLogger()
    root.addHandler(sse_handler)
