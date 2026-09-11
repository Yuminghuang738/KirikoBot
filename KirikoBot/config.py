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
    # ── Model ─────────────────────────────────────────
    # DeepSeek V4.1 Flash (API model name `deepseek-flash`). V4.1 Flash tops
    # the retired V4 Pro / V4 Flash / V4 Flash Vision Exp models and is
    # natively multimodal, so it is the single model used across the bot.
    # Override with DEEPSEEK_MODEL in .env without touching code.
    DEEPSEEK_MODEL: Final[str] = os.getenv("DEEPSEEK_MODEL") or "deepseek-flash"
    # Thinking effort for the requests that DO enable thinking mode
    # (low / high / max). The API defaults to `high`, whose invisible
    # reasoning tokens dominate chat latency; `low` keeps tool selection
    # good enough while roughly halving the time to reply.
    # Background calls (news translation, vision, judge, profiling) pass
    # thinking: disabled explicitly and are unaffected by this setting.
    DEEPSEEK_REASONING_EFFORT: Final[str] = os.getenv("DEEPSEEK_REASONING_EFFORT") or "low"
    GROUP_ROLE: Final[str | None] = os.getenv("GROUP_ROLE")
    PRIVATE_ROLE: Final[str | None] = os.getenv("PRIVATE_ROLE")
    TAROT_ROLE: Final[str | None] = os.getenv("TAROT_ROLE")

    REQUEST_TIMEOUT: Final[int] = 30
    MAX_RETRIES: Final[int] = 3

    # ── LLBot WebUI bridge ────────────────────────────
    # The LLBot WebUI (React SPA) listens on :3080 and guards every /api/*
    # call with `x-webui-token: sha256(password)`. We proxy it same-origin so
    # the dashboard can show LLBot status/login/logs without a second login.
    # LLBOT_WEBUI_URL is how *this* process reaches it (Docker service name).
    LLBOT_WEBUI_URL: Final[str] = os.getenv("LLBOT_WEBUI_URL") or "http://llbot:3080"
    # How the *browser* reaches it, for the embedded WebQQ iframe. Empty means
    # "derive from the request host with port 3080".
    LLBOT_WEBUI_PUBLIC_URL: Final[str] = os.getenv("LLBOT_WEBUI_PUBLIC_URL") or ""
    # Explicit plaintext password; when unset we read the token file below.
    LLBOT_WEBUI_TOKEN: Final[str | None] = os.getenv("LLBOT_WEBUI_TOKEN")
    # Candidate locations of llbot_config/webui_token.txt (Docker mount first,
    # then paths relative to this file for running straight from the repo).
    LLBOT_TOKEN_PATHS: Final[tuple[str, ...]] = tuple(
        p for p in [
            os.getenv("LLBOT_TOKEN_FILE"),
            "/app/llbot_config/webui_token.txt",
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "llbot_config", "webui_token.txt"),
            os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "llbot_config", "webui_token.txt"),
        ] if p
    )

    # ── Vision (optional, for image description) ──────
    # Uses DeepSeek's official vision model via the same API endpoint and
    # token as the chat model (DEEPSEEK_API / DEEPSEEK_TOKEN).
    # V4.1 Flash is natively multimodal, so it defaults to DEEPSEEK_MODEL.
    # Set VISION_ENABLED=0 to disable; image understanding then falls back
    # to context-based responses.
    VISION_ENABLED: Final[bool] = os.getenv("VISION_ENABLED", "1") == "1"
    VISION_MODEL: Final[str] = os.getenv("VISION_MODEL") or DEEPSEEK_MODEL

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
