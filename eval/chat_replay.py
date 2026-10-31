#!/usr/bin/env python3
"""回放真实群聊回合，对比不同配置变体的回复质量（LLM 裁判，双顺序）。

为什么需要它：「人味」「注意力」这类东西靠肉眼看一两条回复判断不了，改一处提示词
常常是此消彼长。这里把生产库里的**真实回合**拿出来，在同一份历史上跑不同变体，
让裁判只对比「谁更接住了上下文」「谁更像真人」「谁更像 Kiriko」。

防泄露：
  · 只用 DB 的**副本**（每次运行前复制一份），不写生产库。
  · 历史只取回合发生**之前**的消息；群背景只取回合时间点之前 15 分钟内的消息。

用法（在仓库根目录）：
    python3 eval/chat_replay.py --db KirikoBot/robot.db --n 40 --variants baseline,long_history
    python3 eval/chat_replay.py --list-variants

输出：eval/out/<时间戳>.json，并在终端打印胜率表。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import threading
import zlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "KirikoBot"
sys.path.insert(0, str(APP))

import requests  # noqa: E402

API_URL = os.getenv("DEEPSEEK_API") or "https://api.deepseek.com/chat/completions"
MODEL = os.getenv("EVAL_MODEL") or "deepseek-flash"


# ── 变体：每个变体 = 一组可控旋钮 ─────────────────────────────

@dataclass(frozen=True)
class Variant:
    name: str
    raw_limit: int = 12          # 从库里取的原始行数（生产现状 = 12）
    max_msgs: int = 8            # 装入上下文的最大**消息条数**（生产 MAX_HISTORY = 8 = 4 轮）
    group_context: bool = False  # 是否附带群背景
    thinking: bool = True        # 是否开思考模式
    temperature: float | None = None   # 仅非思考模式生效
    top_p: float | None = None
    style_rule: bool = False     # 句末不加句号、短句的语感规则
    exemplars: int = 0           # 附带多少条真人群友的短消息作为语感示例
    tight: bool = False          # 在 user message 末尾再压一次长度（近因效应）
    static_exemplars: bool = False  # 示例固定（所有回合相同）→ 可进系统提示词、利于缓存
    no_speaker: bool = False        # 去掉「当前说话人」的关系 + 画像块（测个性化的贡献）
    no_memory: bool = False         # 保留关系+画像，只去掉共同记忆（测记忆的贡献）
    no_affection: bool = False      # 保留画像+记忆，只去掉关系（好感度）块（测好感度的贡献）
    legacy: bool = False            # 还原改动前的提示词：剥掉语感章节与群聊尾注（真正的「旧生产」基线）
    note: str = ""


VARIANTS: dict[str, Variant] = {
    "baseline": Variant("baseline", note="生产现状：LIMIT 12 原始行，群背景关，思考开"),
    "long_history": Variant("long_history", raw_limit=40, max_msgs=16,
                            note="修掉历史截断：40 原始行，16 条消息 = 8 轮"),
    "group_ctx": Variant("group_ctx", raw_limit=40, max_msgs=16, group_context=True,
                         note="长历史 + 群背景开启"),
    "style_rule": Variant("style_rule", raw_limit=40, max_msgs=16, group_context=True,
                          style_rule=True,
                          note="群背景 + 语感规则（短句、句末不加句号）"),
    "style_full": Variant("style_full", raw_limit=40, max_msgs=16, group_context=True,
                          style_rule=True, exemplars=8,
                          note="群背景 + 语感规则 + 8 条真人群友短消息示例"),
    "style_tight": Variant("style_tight", raw_limit=40, max_msgs=16, group_context=True,
                           style_rule=True, exemplars=8, tight=True,
                           note="style_full + 末尾再压长度（十几个字，一两句就停）"),
    "tight_nothink": Variant("tight_nothink", raw_limit=40, max_msgs=16, group_context=True,
                             style_rule=True, exemplars=8, tight=True, thinking=False,
                             temperature=1.0, top_p=1.0,
                             note="style_tight + 关思考，温度 1.0（真正生效的采样）"),
    "tight_nogroup": Variant("tight_nogroup", raw_limit=40, max_msgs=16, group_context=False,
                             style_rule=True, exemplars=8, tight=True,
                             note="style_tight 去掉群背景（只保留长历史）"),
    "style_tight_static": Variant("style_tight_static", raw_limit=40, max_msgs=16,
                                  group_context=True, style_rule=True, exemplars=8,
                                  tight=True, static_exemplars=True,
                                  note="style_tight，但示例固定（可进系统提示词、利于缓存）"),
    "no_speaker": Variant("no_speaker", raw_limit=40, max_msgs=16, group_context=True,
                          style_rule=True, exemplars=8, tight=True, static_exemplars=True,
                          no_speaker=True,
                          note="style_tight_static 去掉个性化块（关系+画像）"),
    "mem_off": Variant("mem_off", raw_limit=40, max_msgs=16, group_context=True,
                       style_rule=True, exemplars=8, tight=True, static_exemplars=True,
                       no_memory=True, note="style_tight_static 去掉共同记忆（其余不变）"),
    "aff_off": Variant("aff_off", raw_limit=40, max_msgs=16, group_context=True,
                       style_rule=True, exemplars=8, tight=True, static_exemplars=True,
                       no_affection=True, note="style_tight_static 去掉好感度（关系）块"),
    "prod_old": Variant("prod_old", raw_limit=12, max_msgs=8, group_context=False,
                        legacy=True,
                        note="改动前的生产提示词：8 条历史、无群背景、无语感章节、无尾注"),
    "no_think_warm": Variant("no_think_warm", raw_limit=40, max_msgs=16, group_context=True,
                             thinking=False, temperature=1.0, top_p=1.0,
                             note="长历史 + 群背景 + 关思考，温度 1.0 才真正生效"),
}


# ── 样本抽取 ───────────────────────────────────────────────

@dataclass
class Sample:
    uid: str
    gid: str
    user_name: str
    user_text: str
    user_ts: str
    reply_ref: str            # 真人/机器人原始回复（仅作参照，不参与裁判对比）
    history_rows: list[tuple] = field(default_factory=list)   # newest-first, 最多 40
    group_rows: list[dict] = field(default_factory=list)


FOLLOWUP_RE = re.compile(r"(这|那|它|他|她|刚才|上面|前面|你说|那个|这个|还是|然后|所以|那咋|咋办|怎么|为啥|为什么|呢[？?]?$)")


def pick_samples(conn: sqlite3.Connection, n: int, seed: int, followup_only: bool = False,
                 min_ts: str = "", only_uids: set[str] | None = None) -> list[Sample]:
    """抽取「用户群聊 @ 消息 → 机器人文字回复」回合。图片、工具回复、过短消息剔除。"""
    cur = conn.execute(
        """
        SELECT u.id, u.user_id, u.group_id, u.content, u.timestamp, a.content
        FROM history u
        JOIN history a ON a.id = (
            SELECT MIN(id) FROM history
            WHERE id > u.id AND user_id = u.user_id AND group_id = u.group_id
              AND role = 'assistant')
        WHERE u.role = 'user' AND u.group_id IS NOT NULL
          AND length(u.content) >= 6 AND u.content NOT LIKE '[图片%'
          AND a.content IS NOT NULL AND length(a.content) > 0
          AND (a.tool_calls IS NULL OR a.tool_calls = '')
        """
    )
    rows = cur.fetchall()
    rnd = random.Random(seed)
    rnd.shuffle(rows)
    out: list[Sample] = []
    for uid_row_id, uid, gid, text, ts, reply in rows:
        if len(out) >= n:
            break
        if followup_only and not (len(text) <= 14 or FOLLOWUP_RE.search(text)):
            continue
        if min_ts and ts < min_ts:
            continue
        if only_uids is not None and uid not in only_uids:
            continue
        hist = conn.execute(
            "SELECT role, content, tool_calls, tool_call_id, id FROM history "
            "WHERE user_id = ? AND group_id = ? AND id < ? ORDER BY id DESC LIMIT 40",
            (uid, gid, uid_row_id),
        ).fetchall()
        name_row = conn.execute(
            "SELECT user_name FROM group_messages WHERE user_id = ? AND group_id = ? "
            "ORDER BY id DESC LIMIT 1", (uid, gid),
        ).fetchone()
        out.append(Sample(
            uid=uid, gid=gid, user_name=(name_row[0] if name_row else "某人"),
            user_text=text.strip(), user_ts=ts, reply_ref=reply.strip(),
            history_rows=[h[:4] for h in hist],
        ))
    return out


def extract_memories_before(db: Any, conn: sqlite3.Connection, cutoff: str, key: str,
                            min_turns: int = 10) -> int:
    """只用 cutoff 之前的 bot 对话提取共同记忆，写入 eval 副本。返回写入的用户数。"""
    import profile_service as ps
    svc = ps.ProfileService()
    uids = [r[0] for r in conn.execute(
        "SELECT user_id FROM history WHERE role = 'user' AND group_id IS NOT NULL "
        "AND timestamp < ? GROUP BY user_id HAVING COUNT(*) >= ?", (cutoff, min_turns))]
    wrote = 0
    for uid in uids:
        rows = conn.execute(
            "SELECT role, content FROM history WHERE user_id = ? AND role IN ('user','assistant') "
            "AND content IS NOT NULL AND content <> '' AND timestamp < ? ORDER BY id DESC LIMIT ?",
            (uid, cutoff, svc.MEMORY_WINDOW)).fetchall()
        rows.reverse()
        name = conn.execute("SELECT user_name FROM group_messages WHERE user_id = ? "
                            "ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
        who = name[0] if name else "某人"
        convo = "\n".join(f"{'Kiriko' if role == 'assistant' else who}: {str(c)[:160]}"
                          for role, c in rows)
        if not convo.strip():
            continue
        raw = _post({"model": MODEL, "temperature": 0.2, "max_tokens": 300, "stream": False,
                     "thinking": {"type": "disabled"},
                     "messages": [{"role": "system", "content": svc.MEMORY_SYSTEM},
                                  {"role": "user", "content": convo}]}, key)
        saved = svc._parse_and_save_memories(db, uid, raw)
        wrote += 1 if saved else 0
    return wrote


def fetch_group_rows(conn: sqlite3.Connection, s: Sample, limit: int = 20) -> list[dict]:
    """回合时间点之前 15 分钟内的群消息（不含之后的，防泄露）。"""
    rows = conn.execute(
        "SELECT user_name, content, user_id FROM group_messages "
        "WHERE group_id = ? AND timestamp < ? AND timestamp >= datetime(?, '-15 minutes') "
        "ORDER BY id DESC LIMIT ?",
        (s.gid, s.user_ts, s.user_ts, limit),
    ).fetchall()
    rows.reverse()
    return [{"user_name": r[0], "content": r[1], "is_bot": r[2] == "1954283072"} for r in rows]


# ── 提示词构造（复用生产代码，只替换历史与群背景） ──────────

class _FakeDB:
    """给 chat_history.load_history 用：返回指定的原始行（newest-first，取前 raw_limit）。"""

    def __init__(self, rows: list[tuple], raw_limit: int):
        self._rows = rows[:raw_limit]

    def takeout_chat_history(self, uid, gid):
        return list(reversed(self._rows))   # 生产里 takeout 后会 reverse 成 oldest-first


class _NoMemoryDB:
    """只把共同记忆查询压成空，其余原样转发（给 mem_off 对照用）。"""

    def __init__(self, inner: Any):
        self._inner = inner

    def get_profile_memories(self, *a, **kw):
        return []

    def __getattr__(self, name):
        return getattr(self._inner, name)


def build_prompt(db: Any, s: Sample, v: Variant, conn: sqlite3.Connection):
    import chat_history
    import prompt_builder as pb
    import profile_service as ps
    import affection_service as afs
    import learning_service as ls

    chat_history.MAX_HISTORY = v.max_msgs
    history = chat_history.load_history(_FakeDB(s.history_rows, v.raw_limit), s.uid, s.gid)

    class _Incoming:
        text = s.user_text
        has_images = False
        image_urls: list[str] = []
        is_at_bot = True

    class _Robot:
        msg_type = "group"
        group_id = s.gid
        group_name = "eval"
        user_id = s.uid
        user_name = s.user_name
        msg = s.user_text
        incoming = _Incoming()
        at_judgement = True

    r = _Robot()
    disabled: set[str] = set()
    profile_svc = ps.ProfileService()
    learn_svc = ls.LearningService()
    aff_svc = afs.AffectionService()

    system = pb.build_system_prompt(r, db, profile_svc, learn_svc, aff_svc, disabled)
    if v.legacy and "【说话的语感】" in system:
        a = system.index("【说话的语感】")
        b = system.index("【表情包】", a)
        system = system[:a] + system[b:]
    if v.style_rule or v.exemplars:
        system += "\n\n" + _style_block(conn, s, v)
    rel = aff_svc.relationship_block(db, s.uid, s.gid)
    aff = aff_svc.get_or_create(db, s.uid, s.gid, s.user_name)
    speaker_db = _NoMemoryDB(db) if v.no_memory else db
    if v.no_affection:
        rel = ""
    speaker = "\n".join(x for x in (rel,
                        profile_svc.build_speaker_context(speaker_db, s.uid, s.gid, s.user_name, affection=aff)) if x)
    if v.no_speaker:
        speaker = ""
    ctx = ""
    if v.group_context:
        ctx = pb.format_group_context(s.group_rows, minutes=15)
    user = pb.build_user_message(r, "", ctx, now=datetime.fromisoformat(s.user_ts.replace(" ", "T")),
                                 speaker=speaker)
    if v.tight:
        user += "\n（就像群里随口接一句：十几个字，一两句就停，句末不加句号。）"
    if v.legacy:
        user = user.replace("\n（就像群里随口接一句：十几个字，一两句就停，句末不加句号。）", "")
    return system, history, user


def _style_block(conn: sqlite3.Connection, s: Sample, v: Variant) -> str:
    """语感约束：规则 + （可选）真人群友短消息示例。示例只取回合之前的真人消息。"""
    lines = ["【说话的语感】（这是最重要的一条）",
             "- 群里聊天就是一两句、十几个字的随口话，句末不加句号；",
             "- 不要把回复写成完整的段落或小作文，别每句都接梗、都下结论。"]
    if v.exemplars:
        rows = conn.execute(
            "SELECT content FROM group_messages WHERE group_id = ? AND msg_type = 'text' "
            "AND user_id != '1954283072' AND length(content) BETWEEN 4 AND 14 "
            "AND timestamp < ? ORDER BY id DESC LIMIT 300",
            (s.gid, s.user_ts),
        ).fetchall()
        pool = [r[0].strip() for r in rows if r[0] and not re.search(r"\[|@|http", r[0])]
        rnd = random.Random(zlib.crc32(f'{s.uid}|{s.user_ts}'.encode()))
        picks = rnd.sample(pool, min(v.exemplars, len(pool)))
        lines.append("群里真人平时就是这种长短和语气（只学感觉，别照抄）：")
        lines += [f"  · {x}" for x in picks]
    return "\n".join(lines)


# ── 生成与裁判 ─────────────────────────────────────────────

def _post(payload: dict, key: str, retries: int = 3) -> str:
    last = ""
    for i in range(retries):
        try:
            r = requests.post(API_URL, json=payload, timeout=90,
                              headers={"Authorization": f"Bearer {key}"})
            if r.status_code == 200:
                msg = r.json()["choices"][0]["message"]
                return (msg.get("content") or "").strip()
            last = f"HTTP {r.status_code}: {r.text[:120]}"
        except Exception as e:  # noqa: BLE001
            last = str(e)
        time.sleep(1.5 * (i + 1))
    return f"[ERROR] {last}"


def generate(system: str, history: list[dict], user: str, v: Variant, key: str) -> str:
    payload: dict[str, Any] = {
        "model": MODEL,
        "messages": [{"role": "system", "content": system}, *history,
                     {"role": "user", "content": user}],
        "max_tokens": 800,
        "stream": False,
        "thinking": {"type": "enabled" if v.thinking else "disabled"},
    }
    if v.thinking:
        payload["reasoning_effort"] = os.getenv("DEEPSEEK_REASONING_EFFORT") or "low"
    else:
        if v.temperature is not None:
            payload["temperature"] = v.temperature
        if v.top_p is not None:
            payload["top_p"] = v.top_p
    return _post(payload, key)


JUDGE_SYSTEM = """你是严格的聊天质量裁判。你会看到一段 QQ 群聊的上下文、群友最后说的一句话，以及两个候选回复 A、B。
请分别判断四个维度谁更好（只能填 A、B 或 平）：
- ctx：谁更接住了对方**这句话**（指代、接的梗、前文的事），而不是答非所问或重复旧话题
- human：谁读起来更像一个真人在群里随口接话（口语、不端着、不像客服/AI 腔、不写小作文）
- persona：谁更像「20岁大三女生、傲娇但讨喜、说话短」的 Kiriko
- personal：谁更像是**专门对这个人说话**（用得上这个人的特点、称呼、之前的事），而不是对谁都一样的套话
只输出 JSON：{"ctx":"A|B|平","human":"A|B|平","persona":"A|B|平","personal":"A|B|平"}"""


def judge(context_text: str, last_user: str, a: str, b: str, key: str) -> dict:
    user = (f"【上下文】\n{context_text}\n\n【群友最后说】\n{last_user}\n\n"
            f"【回复 A】\n{a}\n\n【回复 B】\n{b}")
    payload = {"model": MODEL, "temperature": 0, "max_tokens": 120, "stream": False,
               "thinking": {"type": "disabled"},
               "messages": [{"role": "system", "content": JUDGE_SYSTEM},
                            {"role": "user", "content": user}]}
    raw = _post(payload, key)
    m = re.search(r"\{.*\}", raw, re.S)
    try:
        return json.loads(m.group(0)) if m else {"error": raw[:120]}
    except json.JSONDecodeError:
        return {"error": raw[:120]}


def stats(texts: list[str]) -> dict[str, float]:
    ok = [t for t in texts if t and not t.startswith("[ERROR]")]
    if not ok:
        return {}
    lens = sorted(len(t) for t in ok)
    return {
        "median_len": lens[len(lens) // 2],
        "p90_len": lens[int(len(lens) * 0.9)],
        "question_rate": round(sum(bool(re.search(r"[?？]", t)) for t in ok) / len(ok), 3),
        "markdown_rate": round(sum(bool(re.search(r"\*\*|^\s*[-*] ", t, re.M)) for t in ok) / len(ok), 3),
        "end_period_rate": round(sum(t.endswith("。") for t in ok) / len(ok), 3),
        "over40_rate": round(sum(len(t) > 40 for t in ok) / len(ok), 3),
    }


def rich_user_ids(conn: sqlite3.Connection) -> set[str]:
    """画像丰富 = 至少有一条共同记忆，或稳定画像里有内容的字段 ≥ 3 个。"""
    import json
    import profile_service as ps
    mem = {r[0] for r in conn.execute("SELECT DISTINCT user_id FROM profile_memories")}
    out: set[str] = set()
    for uid, pj in conn.execute("SELECT user_id, profile_json FROM user_profiles"):
        try:
            st = ps.normalize_profile(json.loads(pj or "{}")).get("stable") or {}
        except Exception:
            st = {}
        filled = sum(bool(st.get(k)) for k in ("nickname", "avoid", "catchphrase", "dislikes", "traits", "likes", "notes"))
        if uid in mem or filled >= 3:
            out.add(uid)
    return out


def run(args: argparse.Namespace) -> int:
    key = os.getenv("DEEPSEEK_TOKEN") or _read_token()
    if not key:
        print("找不到 DEEPSEEK_TOKEN（环境变量或 KirikoBot/.env）", file=sys.stderr)
        return 2

    tmpdir = Path(tempfile.mkdtemp(prefix="chat_eval_"))
    db_copy = tmpdir / "robot.db"
    shutil.copy(args.db, db_copy)            # 只用副本，绝不碰原库
    os.environ["KIRIKO_DB"] = str(db_copy)

    conn = sqlite3.connect(db_copy, check_same_thread=False)   # 只读查询，多线程安全
    from database_manager import DatabaseManager
    db = DatabaseManager(str(db_copy))      # 副本上跑迁移，安全

    names = [x.strip() for x in args.variants.split(",") if x.strip()]
    for nm in names:
        if nm not in VARIANTS:
            print(f"未知变体 {nm}，可选：{', '.join(VARIANTS)}", file=sys.stderr)
            return 2
    variants = [VARIANTS[n] for n in names]
    base = variants[0]

    if args.memory_cutoff:
        n_users = extract_memories_before(db, conn, args.memory_cutoff, key)
        print(f"记忆预提取（截止 {args.memory_cutoff}）：{n_users} 个用户写入了记忆")
        for uid, content, kind in conn.execute(
                "SELECT user_id, content, kind FROM profile_memories ORDER BY user_id, id"):
            print(f"   [{kind}] {content}")
    mem_uids = None
    if args.rich_users:
        mem_uids = rich_user_ids(conn)
        print(f"只评测画像丰富的用户：{len(mem_uids)} 人")
    if args.memory_cutoff and args.only_memory_users:
        mem_uids = {r[0] for r in conn.execute("SELECT DISTINCT user_id FROM profile_memories")}
        print(f"只评测有记忆的用户：{len(mem_uids)} 人")
    samples = pick_samples(conn, args.n, args.seed, followup_only=args.followup,
                           min_ts=args.memory_cutoff, only_uids=mem_uids)
    print(f"抽到 {len(samples)} 个真实回合，变体：{', '.join(names)}")

    # 1) 为每个样本、每个变体构造提示词并生成回复
    for s in samples:
        s.group_rows = fetch_group_rows(conn, s)

    jobs = []
    for idx, s in enumerate(samples):
        for v in variants:
            jobs.append((idx, v))

    # SQLite 连接不可并发使用：check_same_thread=False 只是允许跨线程，并发查询仍会报 API 误用。
    # 构造提示词（本地读库，很快）串行化；真正耗时的 API 调用仍然并发。
    db_lock = threading.Lock()

    def gen(job):
        idx, v = job
        s = samples[idx]
        with db_lock:
            system, history, user = build_prompt(db, s, v, conn)
        reply = generate(system, history, user, v, key)
        return {"sample": idx, "variant": v.name, "reply": reply,
                "context_turns": len(history), "user": user[-400:]}

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        results = list(ex.map(gen, jobs))
    print(f"生成完成：{len(results)} 条，用时 {time.time() - t0:.0f}s")

    by = {(r["sample"], r["variant"]): r for r in results}

    # 2) 每个非基线变体 vs 基线：双顺序裁判
    summary: dict[str, dict] = {}
    detail: list[dict] = []
    for v in variants[1:]:
        tally = {"ctx": [0, 0, 0], "human": [0, 0, 0], "persona": [0, 0, 0], "personal": [0, 0, 0]}  # [var, base, tie]

        def cmp(idx):
            s = samples[idx]
            b = by[(idx, base.name)]["reply"]
            x = by[(idx, v.name)]["reply"]
            if b.startswith("[ERROR]") or x.startswith("[ERROR]"):
                return None
            ctx_txt = "\n".join(f"{'Kiriko' if h['role']=='assistant' else s.user_name}: "
                                f"{h['content'][:200]}" for h in _ctx_for_judge(db, s, base))
            g = "\n".join(f"{r['user_name']}: {r['content'][:120]}" for r in s.group_rows[-8:])
            full_ctx = (f"【群里最近】\n{g}\n\n【与这位群友的历史】\n{ctx_txt}" if ctx_txt
                        else f"【群里最近】\n{g}")
            # 正序：A=基线 B=变体；反序：A=变体 B=基线，结果取平均消除位置偏差
            r1 = judge(full_ctx, s.user_text, b, x, key)
            r2 = judge(full_ctx, s.user_text, x, b, key)
            return idx, r1, r2

        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            cmps = [c for c in ex.map(cmp, range(len(samples))) if c]
        for idx, r1, r2 in cmps:
            for dim in ("ctx", "human", "persona", "personal"):
                if r1.get(dim) not in ("A", "B", "平") or r2.get(dim) not in ("A", "B", "平"):
                    tally.setdefault("_missing", [0, 0, 0])[0] += 1
                    continue
                # r1: A=base；r2: A=var
                def score(res, letter):
                    return res.get(dim)
                votes = []
                a1 = score(r1, "A")
                if a1 == "B": votes.append("var")
                elif a1 == "A": votes.append("base")
                a2 = score(r2, "A")
                if a2 == "A": votes.append("var")
                elif a2 == "B": votes.append("base")
                if votes.count("var") > votes.count("base"):
                    tally[dim][0] += 1
                elif votes.count("base") > votes.count("var"):
                    tally[dim][1] += 1
                else:
                    tally[dim][2] += 1
            detail.append({"variant": v.name, "sample": idx, "r1": r1, "r2": r2})

        n_cmp = len(cmps)
        summary[v.name] = {
            "note": v.note,
            "n": n_cmp,
            "win_vs_base": {d: {"variant": t[0], "baseline": t[1], "tie": t[2]} for d, t in tally.items()},
            "stats": stats([by[(i, v.name)]["reply"] for i in range(len(samples))]),
        }
    summary["_baseline_stats"] = {"note": base.note,
                                  "stats": stats([by[(i, base.name)]["reply"] for i in range(len(samples))])}

    out_dir = ROOT / "eval" / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = out_dir / f"{stamp}.json"
    out.write_text(json.dumps({"summary": summary, "results": results, "judge": detail},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    _print_table(summary)
    print(f"\n完整结果：{out.relative_to(ROOT)}")
    shutil.rmtree(tmpdir, ignore_errors=True)
    return 0


def _ctx_for_judge(db: Any, s: Sample, base: Variant) -> list[dict]:
    """给裁判看的「与这位群友的历史」：只取基线视角下的历史（公平，不偏向任何变体）。"""
    import chat_history
    chat_history.MAX_HISTORY = base.max_msgs
    hist = chat_history.load_history(_FakeDB(s.history_rows, base.raw_limit), s.uid, s.gid)
    return hist[-6:]


def _print_table(summary: dict) -> None:
    print("\n═══ 变体 vs 基线（胜/负/平，双顺序裁判）═══")
    print(f"{'变体':<16}{'样本':>5}  {'上下文':>16}  {'人味':>16}  {'人设':>16}  {'个性化':>16}")
    for name, data in summary.items():
        if name.startswith("_"):
            continue
        w = data["win_vs_base"]
        cells = [f"{w[d]['variant']:>2}/{w[d]['baseline']:>2}/{w[d]['tie']:>2}" for d in ("ctx", "human", "persona", "personal")]
        print(f"{name:<16}{data['n']:>5}  {cells[0]:>16}  {cells[1]:>16}  {cells[2]:>16}  {cells[3]:>16}")
    print("\n═══ 回复统计 ═══")
    for name, data in summary.items():
        st = data.get("stats", {})
        print(f"{name:<16} 中位长度 {st.get('median_len')}  P90 {st.get('p90_len')}  "
              f"问号率 {st.get('question_rate')}  markdown率 {st.get('markdown_rate')}  "
              f"句末。率 {st.get('end_period_rate')}  >40字率 {st.get('over40_rate')}")


def _read_token() -> str:
    env = APP / ".env"
    if env.is_file():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("DEEPSEEK_TOKEN"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default=str(APP / "robot.db"))
    p.add_argument("--n", type=int, default=40)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--workers", type=int, default=6)
    p.add_argument("--variants", default="baseline,long_history")
    p.add_argument("--memory-cutoff", default="",
                   help="截止时间，如 2026-09-01：只用它之前的对话提取记忆，并只评测它之后的回合")
    p.add_argument("--only-memory-users", action="store_true",
                   help="与 --memory-cutoff 合用：只评测已写入记忆的用户（记忆的直接效果）")
    p.add_argument("--rich-users", action="store_true",
                   help="只评测画像足够丰富的用户（有记忆，且稳定画像至少 3 个字段有内容）")
    p.add_argument("--followup", action="store_true",
                   help="只抽追问/指代类短句（注意力敏感子集）")
    p.add_argument("--list-variants", action="store_true")
    args = p.parse_args()
    if args.list_variants:
        for v in VARIANTS.values():
            print(f"{v.name:<16} {v.note}")
        return 0
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
