"""用户画像：两层结构 + 分层渲染。

## 为什么改

旧版是 7 个平铺字段，其中 `personality` 还硬限 10 字，渲染进提示词后每人只有
**一行**。两个后果：

1. **标签太薄** —— 一行里塞不下称呼、雷点、惯用语、最近在忙什么；
   实测抽出来的三个画像，`interests` 全是「吉他/音乐」——因为它学到的
   是**这个群的话题**，不是**这个人的特点**。
2. **看不出差异** —— 当前说话人和旁观者拿到的是同样的一行，
   个性化自然体现不出来。

现在拆成两层：

- **stable**（慢变量）：性格关键词、称呼偏好、雷点、口头禅、聊天节奏、
  爱聊/不爱聊
- **recent**（快变量）：最近的情绪、在忙什么、提过的事

渲染也分两层：

- **当前说话人** → 详细块（进 user message，见 `build_speaker_context`）
- **其他人** → 保持一行（进 system prompt，省 token 且缓存友好）

另外画像分析时会顺带产出 `memories` —— 和这个人之间**具体**发生过的事。
标签说「直率」，记忆说「上周答应帮他听一段录音」，后者才有用。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from ai_server import quick_chat
from config import Config

logger = logging.getLogger(__name__)


def normalize_profile(data: Any) -> dict[str, Any]:
    """把任意版本的画像统一成 v2 结构。

    老画像（v1）长这样：
        {"personality": "直率急躁，夜猫子", "interests": [...],
         "speaking_style": "...", "topics": [...], "mood": "...", "note": "..."}
    直接字段映射过来，剩下的等下次分析补齐 —— 不丢信息，也不用重跑模型。
    """
    if not isinstance(data, dict):
        return {"version": 2, "stable": {}, "recent": {}}
    if isinstance(data.get("stable"), dict) or isinstance(data.get("recent"), dict):
        out = {
            "version": 2,
            "stable": dict(data.get("stable") or {}),
            "recent": dict(data.get("recent") or {}),
        }
        return out

    # ── v1 → v2 ──
    personality = str(data.get("personality") or "").strip()
    traits = [t.strip() for t in personality.replace("，", ",").replace("、", ",")
              .split(",") if t.strip()][:3]
    interests = [str(i).strip() for i in (data.get("interests") or []) if str(i).strip()]
    topics = [str(t).strip() for t in (data.get("topics") or []) if str(t).strip()]
    return {
        "version": 2,
        "stable": {
            "traits": traits,
            "nickname": "",
            "avoid": [],
            "catchphrase": [],
            "style": {"length": str(data.get("speaking_style") or ""), "emoji": "", "night": False},
            "likes": interests or topics,
            "dislikes": [],
            "notes": str(data.get("note") or ""),
        },
        "recent": {"mood": str(data.get("mood") or ""), "doing": "", "mentioned": []},
    }


class ProfileService:
    """Analyzes user messages via DeepSeek to generate personality profiles."""

    MIN_MESSAGES = 20       # minimum messages before profiling
    MAX_SAMPLE_MSGS = 50    # max messages to send for analysis
    REANALYZE_GAP = 100     # re-analyze every N new messages
    MAX_MEMORIES = 3        # 每次分析最多产生几条共同记忆

    SYSTEM_PROMPT = (
        "你是一个用户画像分析助手。根据这个人的聊天记录，提炼出**能用来跟这个人聊天**的信息。"
        "以JSON输出，不要有别的内容：\n"
        '{"stable":{"traits":["性格关键词(最多3个)"],'
        '"nickname":"他自己说过的称呼，没有就留空",'
        '"avoid":["不能提的话题或不能开的玩笑"],'
        '"catchphrase":["他的口头禅"],'
        '"style":{"length":"短句|中等|长句","emoji":"不用|偶尔|多","night":true},'
        '"likes":["爱聊的"],"dislikes":["不爱聊的"],"notes":"一句补充"},'
        '"recent":{"mood":"最近的情绪","doing":"最近在忙什么","mentioned":["最近提过的具体事"]},'
        '"memories":["值得记住的具体的事，最多3条"]}\n'
        "要求：\n"
        "1. 只写**能从记录里看出来**的，看不出来就留空，不要猜。\n"
        "2. traits 是**这个人**的特点，不是这个群的话题 —— "
        "别所有人都写「喜欢吉他」，那是群的性质。\n"
        "3. memories 要具体、可复述（「答应帮他听一段录音」），"
        "不要写「聊过音乐」这种废话；没有就给空数组。\n"
        "4. 只输出JSON，不要 markdown 代码块。"
    )

    def __init__(self) -> None:
        self._analyzing: set[str] = set()  # prevent duplicate analysis

    @staticmethod
    def _is_bot(user_id: str) -> bool:
        """Check if a user_id is a known bot account."""
        return user_id in Config.BOT_QQ_LIST

    def should_analyze(self, db: Any, user_id: str) -> bool:
        """Check if user needs profile analysis.

        计数是**跨所有群 + 私聊**的：画像是这个人唯一的一份，所以「攒够消息
        了没有」也得看他在所有地方一共说了多少。
        """
        if user_id in self._analyzing:
            return False
        if self._is_bot(user_id):
            return False
        existing = db.get_user_profile(user_id)
        if not existing:
            return True
        # Re-analyze if enough new messages accumulated
        total = db.count_user_messages(user_id)
        old_count = existing.get("message_count", 0)
        return (total - old_count) >= self.REANALYZE_GAP

    def analyze_user(
        self, db: Any, user_id: str, user_name: str, group_id: str = "",
    ) -> dict[str, Any] | None:
        """Analyze a user's messages and save profile. Non-blocking wrapper."""
        if user_id in self._analyzing:
            return None
        if self._is_bot(user_id):
            return None
        self._analyzing.add(user_id)
        try:
            return self._do_analyze(db, user_id, user_name, group_id)
        finally:
            self._analyzing.discard(user_id)

    def _do_analyze(
        self, db: Any, user_id: str, user_name: str, group_id: str = "",
    ) -> dict[str, Any] | None:
        # 跨群 + 私聊取样，画出来的才是「他这个人」而不是「他在这个群的样子」
        messages = db.get_user_messages(user_id, self.MAX_SAMPLE_MSGS)
        if len(messages) < self.MIN_MESSAGES:
            logger.info(
                "User %s has %d messages (<%d), skipping profile",
                user_name, len(messages), self.MIN_MESSAGES,
            )
            return None

        sample = "\n".join(
            f"[{ts}]: {content[:200]}" for content, ts in messages[:self.MAX_SAMPLE_MSGS]
        )
        total_count = db.count_user_messages(user_id)

        result = quick_chat(
            self.SYSTEM_PROMPT,
            f"用户 {user_name} 的聊天记录：\n{sample}",
            max_tokens=700, temperature=0.3, timeout=30, source="profile",
        )
        if result is None:
            logger.warning("Profile analysis API failed for %s", user_name)
            return None

        try:
            result = result.strip()
            if result.startswith("```"):
                result = result.split("\n", 1)[1].rsplit("\n", 1)[0]
            raw = json.loads(result)
        except (json.JSONDecodeError, ValueError):
            logger.warning("Failed to parse profile JSON for %s: %s", user_name, result[:100])
            raw = {"notes": result[:200]}

        profile = normalize_profile(raw)

        # 共同记忆：分析时顺带记下来，比画像标签有用
        for memory in (raw.get("memories") or [])[: self.MAX_MEMORIES]:
            text = str(memory).strip()
            if text:
                db.add_profile_memory(user_id, text)

        try:
            db.save_user_profile(
                user_id, user_name,
                json.dumps(profile, ensure_ascii=False), total_count, group_id,
            )
        except Exception:
            logger.exception("Failed to save profile for %s", user_name)
            return None

        logger.info(
            "Profile saved for %s: traits=%s likes=%s",
            user_name,
            profile.get("stable", {}).get("traits"),
            profile.get("stable", {}).get("likes"),
        )
        return profile

    # ── 渲染：当前说话人（详细块，进 user message） ───────

    @staticmethod
    def resolve_address(stable: dict[str, Any], user_name: str, score: float) -> str:
        """该叫这个人什么。

        **不自动编外号** —— 昵称叫错了比不叫更尴尬。只用他自己说过的称呼，
        没有就用名字；关系还没到「友好」就不叫（用「你」）。
        """
        nickname = str((stable or {}).get("nickname") or "").strip()
        if nickname:
            return nickname
        if score >= 40:
            return user_name or ""
        return ""

    def build_speaker_context(
        self, db: Any, user_id: str, group_id: str, user_name: str,
        affection: dict[str, Any] | None = None,
    ) -> str:
        """当前说话人的详细块。

        和「其他人」那条一行列表不同，这里给足信息：称呼、雷点、最近的事、
        共同记忆。**差异化的关键不在形容词，而在信息不对称** ——
        说得出对方上次提过的事，比说十句「你很活泼」都有用。
        """
        score = float((affection or {}).get("affection_score") or 50.0)
        prof = db.get_user_profile(user_id) or {}
        data = normalize_profile(prof.get("profile") or {})
        stable, recent = data.get("stable") or {}, data.get("recent") or {}

        lines: list[str] = []
        address = self.resolve_address(stable, user_name, score)
        head = f"【正在跟你说话的人】{user_name}"
        if address and address != user_name:
            head += f"（称呼 TA「{address}」）"
        lines.append(head)

        traits = [t for t in (stable.get("traits") or []) if t]
        if traits:
            lines.append("性格：" + "、".join(traits[:3]))

        likes = [x for x in (stable.get("likes") or []) if x][:3]
        dislikes = [x for x in (stable.get("dislikes") or []) if x][:2]
        if likes:
            lines.append("爱聊：" + "、".join(likes))
        if dislikes:
            lines.append("不爱聊：" + "、".join(dislikes))

        avoid = [x for x in (stable.get("avoid") or []) if x][:2]
        if avoid:
            lines.append("雷点（别碰）：" + "、".join(avoid))

        phrases = [x for x in (stable.get("catchphrase") or []) if x][:3]
        if phrases:
            lines.append("口头禅：" + "、".join(phrases))

        style = stable.get("style") or {}
        bits = []
        if style.get("length"):
            bits.append(f"说话{style['length']}")
        if style.get("emoji"):
            bits.append(f"表情{style['emoji']}")
        if style.get("night"):
            bits.append("夜猫子")
        if bits:
            lines.append("节奏：" + "，".join(bits))

        if stable.get("notes"):
            lines.append(f"备注：{stable['notes']}")

        recents = []
        if recent.get("doing"):
            recents.append(f"最近在忙{recent['doing']}")
        if recent.get("mood"):
            recents.append(f"情绪{recent['mood']}")
        mentioned = [m for m in (recent.get("mentioned") or []) if m][:2]
        if mentioned:
            recents.append("提过：" + "、".join(mentioned))
        if recents:
            lines.append(" | ".join(recents))

        # 共同记忆 —— 挑最久没提过的几条，提过就换
        try:
            memories = db.get_profile_memories(user_id, limit=3)
        except Exception:
            memories = []
        if memories:
            lines.append("你们之间：" + "；".join(m["content"] for m in memories))
            try:
                db.mark_memory_used([m["id"] for m in memories])
            except Exception:
                logger.debug("mark_memory_used failed", exc_info=True)

        return "\n".join(lines)

    # ── 渲染：其他人（一行，进 system prompt） ─────────────

    def build_context_prompt(
        self, db: Any, group_id: str, current_user_id: str,
    ) -> str:
        """群成员的**简版**印象（一行一个）。

        当前说话人不在这里出现 —— 他有单独的详细块（`build_speaker_context`），
        放进 user message。这样既省 token，也不破坏系统提示词的前缀缓存。
        """
        profiles = db.get_group_profiles(group_id)
        if not profiles:
            return ""

        lines = ["【群里的其他人】（只是印象，聊天时自然带出来，别照着念）"]
        shown = 0
        for p in profiles:
            if p["user_id"] == current_user_id:
                continue                      # 当前说话人另有详细块
            data = normalize_profile(p["profile"] or {})
            stable = data.get("stable") or {}
            traits = [t for t in (stable.get("traits") or []) if t]
            likes = [x for x in (stable.get("likes") or []) if x][:2]
            if not traits and not likes:
                continue
            bits = []
            if traits:
                bits.append("、".join(traits[:2]))
            if likes:
                bits.append("爱聊" + "、".join(likes))
            if not bits:
                continue
            lines.append(f"- {p['user_name']}：" + "；".join(bits))
            shown += 1
            if shown >= 15:
                break

        return "\n".join(lines) if shown else ""
