#!/usr/bin/env python3
"""私聊学习判定的精确率与召回率（对照独立判断）。

学习判定（LearningService.evaluate_prev）只在私聊路径上运行。它保存的笔记会被注入提示词，
所以误记的代价是直接的。这里取线上最近的真实私聊回合，让学习判定和一个独立的裁判分别
判断「用户后续是否明确表达不满」，然后对照：
  · 精确率：学习判定保存了笔记，其中独立判断确为不满的比例
  · 召回率：独立判断确为不满的回合中，学习判定记录了笔记的比例

用法（仓库根目录，需要 KirikoBot/robot.db 的副本路径）：
    python3 eval/learning_precision.py --db /tmp/lcopy_local.db
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "KirikoBot"
sys.path.insert(0, str(APP))

import requests  # noqa: E402

API = "https://api.deepseek.com/chat/completions"
JUDGE_SYS = (
    "判断用户后续这句话对机器人上一轮回复的反应。只输出 JSON：{\"reaction\": \"不满|认可|换话题|继续玩梗|其他\"}。"
    "不满=用户明确纠正、抱怨、说答得不对/没听懂/没用、或指出机器人做错了事；"
    "认可=明确感谢或表示有用；换话题=开始问无关的新事；继续玩梗=接着开玩笑、撒娇、发表情图；其他=其它。"
)


def _key() -> str:
    for line in (APP / ".env").read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("DEEPSEEK_TOKEN"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("找不到 DEEPSEEK_TOKEN")


def private_pairs(conn: sqlite3.Connection, limit: int = 40) -> list[tuple[str, str, str, str]]:
    """(uid, 用户原话, 机器人回复, 用户后续) —— 只取私聊、同一用户、紧邻的三条。"""
    rows = conn.execute(
        "SELECT user_id, role, content FROM history WHERE role IN ('user','assistant') "
        "AND group_id IS NULL AND content IS NOT NULL AND content <> '' ORDER BY id DESC LIMIT 400"
    ).fetchall()
    rows.reverse()
    pairs = []
    for i in range(len(rows) - 2):
        a, b, c = rows[i], rows[i + 1], rows[i + 2]
        if a[1] == "user" and b[1] == "assistant" and c[1] == "user" and a[0] == b[0] == c[0]:
            pairs.append((a[0], a[2], b[2], c[2]))
    return pairs[-limit:]


def judge(um: str, ai: str, fu: str, key: str) -> str:
    user = f"【机器人上一轮】用户说：{um[:150]}\n机器人回：{ai[:150]}\n\n【用户后续】{fu[:150]}"
    payload = {"model": "deepseek-flash", "temperature": 0, "max_tokens": 60, "stream": False,
               "thinking": {"type": "disabled"},
               "messages": [{"role": "system", "content": JUDGE_SYS}, {"role": "user", "content": user}]}
    try:
        r = requests.post(API, json=payload, timeout=60, headers={"Authorization": f"Bearer {key}"})
        m = re.search(r"\{.*\}", r.json()["choices"][0]["message"]["content"], re.S)
        return json.loads(m.group(0)).get("reaction", "其他")
    except Exception:  # noqa: BLE001
        return "其他"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="线上数据库的副本路径（只读拷贝，脚本会再拷一份写入）")
    ap.add_argument("--n", type=int, default=40)
    args = ap.parse_args()

    import os
    os.environ.setdefault("ROBOT_QQ", "1")
    os.environ.setdefault("ONEBOT_API", "x")
    os.environ.setdefault("ONEBOT_TOKEN", "x")
    os.environ.setdefault("DEEPSEEK_TOKEN", _key())

    from database_manager import DatabaseManager
    import learning_service as ls

    key = _key()
    src = Path(args.db)
    tmp = Path(tempfile.mkdtemp(prefix="learnprec_")) / "copy.db"
    shutil.copy(src, tmp)
    pairs = private_pairs(sqlite3.connect(tmp), args.n)
    db = DatabaseManager(str(tmp))
    svc = ls.LearningService()

    # 两种「正确」：用户明确不满（回复不当），或客观的工具失误（该调用没调用、选错了）。
    # 工具失误不需要用户抱怨，用户后续说「来张塔罗牌」时它本身就是有效教训，不应算作噪声。
    tp = fp = fn = 0
    valid = 0
    tool_saved = tool_valid = 0
    for uid, um, ai, fu in pairs:
        note = svc.evaluate_prev(db, uid, {"user_msg": um[:300], "ai_text": ai[:300], "tool": ""}, fu[:300])
        complaint = judge(um, ai, fu, key) == "不满"
        is_tool = bool(note) and note.startswith(("[遗漏工具]", "[工具选择错误]"))
        valid_note = bool(note) and (complaint or is_tool)
        if note and is_tool:
            tool_saved += 1
            tool_valid += 1
        if valid_note: valid += 1
        if valid_note and complaint: tp += 1
        if note and not valid_note: fp += 1
        if complaint and not note: fn += 1
        time.sleep(0.15)
    shutil.rmtree(tmp.parent, ignore_errors=True)

    saved_total = valid + fp
    print(f"对数 {len(pairs)}；保存 {saved_total}（其中工具失误 {tool_saved}）")
    print(f"有效教训精确率（不满或工具失误）{valid}/{saved_total} = {100 * valid / max(1, saved_total):.0f}%")
    print(f"「用户明确不满」召回率 {tp}/{tp + fn} = {100 * tp / max(1, tp + fn):.0f}%（样本小，仅供参考）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
