from __future__ import annotations

import os
from typing import Final

from dotenv import load_dotenv

load_dotenv()


class Config:
    ROBOT_QQ: Final[str | None] = os.getenv("ROBOT_QQ")
    ONEBOT_API: Final[str | None] = os.getenv("ONEBOT_API")
    # QQ accounts to exclude from profiling, affection, and data collection
    # (the bot itself + other known bots like QQ's built-in 小冰)
    BOT_QQ_LIST: Final[set[str]] = {
        qq for qq in [
            os.getenv("ROBOT_QQ"),
            os.getenv("EXTRA_BOT_QQ", "2854196306"),  # QQ 小冰
        ] if qq
    }
    ONEBOT_TOKEN: Final[str | None] = os.getenv("ONEBOT_TOKEN")
    DEEPSEEK_API: Final[str] = os.getenv("DEEPSEEK_API") or "https://api.deepseek.com/chat/completions"
    DEEPSEEK_TOKEN: Final[str | None] = os.getenv("DEEPSEEK_TOKEN")
    GROUP_ROLE: Final[str | None] = os.getenv("GROUP_ROLE")
    PRIVATE_ROLE: Final[str | None] = os.getenv("PRIVATE_ROLE")
    TAROT_ROLE: Final[str | None] = os.getenv("TAROT_ROLE")

    REQUEST_TIMEOUT: Final[int] = 30
    MAX_RETRIES: Final[int] = 3

    # ── Vision (optional, for image description) ──────
    # Uses DeepSeek's official vision model via the same API endpoint and
    # token as the chat model (DEEPSEEK_API / DEEPSEEK_TOKEN).
    # Set VISION_ENABLED=0 to disable; image understanding then falls back
    # to context-based responses.
    VISION_ENABLED: Final[bool] = os.getenv("VISION_ENABLED", "1") == "1"
    VISION_MODEL: Final[str] = os.getenv("VISION_MODEL") or "deepseek-v4-flash-vision-exp"

    @classmethod
    def validate(cls) -> None:
        required: dict[str, str | None] = {
            "ROBOT_QQ": cls.ROBOT_QQ,
            "ONEBOT_API": cls.ONEBOT_API,
            "ONEBOT_TOKEN": cls.ONEBOT_TOKEN,
            "DEEPSEEK_TOKEN": cls.DEEPSEEK_TOKEN,
            "GROUP_ROLE": cls.GROUP_ROLE,
            "PRIVATE_ROLE": cls.PRIVATE_ROLE,
            "TAROT_ROLE": cls.TAROT_ROLE,
        }
        missing = [k for k, v in required.items() if not v]
        if missing:
            raise ValueError(
                f"Missing required environment variables: {', '.join(missing)}. "
                "Please check your .env file."
            )


Config.validate()
