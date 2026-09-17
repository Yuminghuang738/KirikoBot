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
    # Shared secret LLBot signs its http-post events with. LLBot (OB11HttpPost)
    # sends `x-signature: sha1=<HMAC-SHA1(raw body)>` keyed by the token set on
    # its http-post connection — NOT an Authorization header. Defaults to
    # ONEBOT_TOKEN so a correctly configured deployment is protected with no
    # extra setting; set WEBHOOK_TOKEN to use a distinct secret.
    WEBHOOK_TOKEN: Final[str | None] = os.getenv("WEBHOOK_TOKEN") or os.getenv("ONEBOT_TOKEN")
    # ── Dashboard access ──────────────────────────────
    # The panel can delete data and drive the QQ account, so it is protected by
    # HTTP Basic auth. Leave DASHBOARD_PASSWORD empty and one is generated on
    # first start into KirikoBot/.dashboard_password (gitignored).
    DASHBOARD_USER: Final[str] = os.getenv("DASHBOARD_USER") or "admin"
    DASHBOARD_PASSWORD: Final[str | None] = os.getenv("DASHBOARD_PASSWORD")
    DASHBOARD_AUTH_ENABLED: Final[bool] = os.getenv("DASHBOARD_AUTH", "1") == "1"
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
    # OPTIONAL extra notes appended AFTER the built-in persona. Kiriko's
    # identity and delivery rules live in prompt_builder.PERSONA — that is the
    # single source of truth, and anything here is explicitly subordinate to it
    # (a contradictory line can no longer redefine her).
    GROUP_ROLE: Final[str | None] = os.getenv("GROUP_ROLE")
    PRIVATE_ROLE: Final[str | None] = os.getenv("PRIVATE_ROLE")
    TAROT_ROLE: Final[str | None] = os.getenv("TAROT_ROLE")

    REQUEST_TIMEOUT: Final[int] = 30
    MAX_RETRIES: Final[int] = 3

    # ── AI usage metrics ──────────────────────────────
    # Peak rates in USD per 1M tokens for deepseek-flash (off-peak is half).
    # Defaults match https://api-docs.deepseek.com/quick_start/pricing — prices
    # change, so override in .env rather than editing code.
    AI_PRICE_CACHE_HIT: Final[float] = float(os.getenv("AI_PRICE_CACHE_HIT") or 0.006)
    AI_PRICE_CACHE_MISS: Final[float] = float(os.getenv("AI_PRICE_CACHE_MISS") or 0.30)
    AI_PRICE_OUTPUT: Final[float] = float(os.getenv("AI_PRICE_OUTPUT") or 1.20)
    # Peak hours are 01:00-04:00 and 06:00-10:00 UTC, Mon-Fri.
    AI_METRICS_ENABLED: Final[bool] = os.getenv("AI_METRICS_ENABLED", "1") == "1"

    # ── Data retention & backups ──────────────────────    # Raw message tables grow forever otherwise. 0 disables pruning.
    # Aggregates (profiles, affection, tool counts) are never pruned.
    RETENTION_DAYS: Final[int] = int(os.getenv("RETENTION_DAYS") or 180)
    BACKUP_ENABLED: Final[bool] = os.getenv("BACKUP_ENABLED", "1") == "1"
    BACKUP_KEEP: Final[int] = int(os.getenv("BACKUP_KEEP") or 14)
    # Defaults inside the app directory: in Docker the app is /app and that is
    # the only path guaranteed to be writable *and* persisted by the compose
    # mount. Point BACKUP_DIR elsewhere only if you also mount it.
    BACKUP_DIR: Final[str] = os.getenv("BACKUP_DIR") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "backups"
    )

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

    # ── Ambient group context (OFF by default) ────────
    # Reading the room is meant to be a *decision*: the model calls
    # `read_context` when it judges that it needs to. Attaching a transcript
    # to every message instead was tried and reverted — it made the bot's
    # awareness unconditional and blanketed, when what was actually wanted was
    # a looser trigger for the tool.
    #
    # What IS always on is quote awareness: if a message quotes something, the
    # quoted content is always resolved and attached (see _reply_note). That is
    # a fact about the current message, not a judgement call.
    #
    # Set GROUP_CONTEXT_ENABLED=1 to attach the transcript to every group
    # message as well. When on, it goes in the user message rather than the
    # system prompt so the large stable system prompt keeps hitting
    # DeepSeek's prefix cache.
    GROUP_CONTEXT_ENABLED: Final[bool] = os.getenv("GROUP_CONTEXT_ENABLED", "0") == "1"
    GROUP_CONTEXT_MINUTES: Final[int] = int(os.getenv("GROUP_CONTEXT_MINUTES") or 15)
    GROUP_CONTEXT_LIMIT: Final[int] = int(os.getenv("GROUP_CONTEXT_LIMIT") or 20)

    # How far back to count "how many times has this user pestered me" — the
    # signal behind the persona's escalating temper. Larger = slower to anger.
    PATIENCE_WINDOW_MINUTES: Final[int] = int(os.getenv("PATIENCE_WINDOW_MINUTES") or 10)
    # A temper that never subsides is worse than no temper at all, and one
    # that resets the instant the counting window rolls over isn't human
    # either. The mood decays to normal over this many minutes.
    MOOD_COOLDOWN_MINUTES: Final[int] = int(os.getenv("MOOD_COOLDOWN_MINUTES") or 30)

    @classmethod
    def validate(cls) -> None:
        required: dict[str, str | None] = {
            "ROBOT_QQ": cls.ROBOT_QQ,
            "ONEBOT_API": cls.ONEBOT_API,
            "ONEBOT_TOKEN": cls.ONEBOT_TOKEN,
            "DEEPSEEK_TOKEN": cls.DEEPSEEK_TOKEN,
        }
        missing = [k for k, v in required.items() if not v]
        if missing:
            raise ValueError(
                f"Missing required environment variables: {', '.join(missing)}. "
                "Please check your .env file."
            )


Config.validate()
