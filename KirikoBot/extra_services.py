from __future__ import annotations

import logging
import re
from typing import Any

import requests

logger = logging.getLogger(__name__)

# ── Hitokoto ──────────────────────────────────────────────

class HitokotoService:
    """Fetches random inspirational quotes from hitokoto.cn (free API)."""

    URL = "https://v1.hitokoto.cn/"
    TIMEOUT = 8

    def get_quote(self) -> dict[str, str] | None:
        try:
            r = requests.get(self.URL, timeout=self.TIMEOUT)
            r.raise_for_status()
            data = r.json()
            return {
                "text": data.get("hitokoto", ""),
                "source": data.get("from", "") or "未知",
                "author": data.get("from_who", "") or "",
            }
        except Exception:
            logger.exception("Failed to fetch hitokoto")
            return None
