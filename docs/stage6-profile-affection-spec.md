# 阶段 6：好感度并入用户画像 — 统一模型与迁移 spec

> 目标：好感度从「群内分数」改成「这个人的分数」，群/私聊各算一个**分量**；私聊消息能触发画像与好感度分析；只加列、不重建、不删旧数据；面板、提示词、工具读同一个分。

## 0. 现状（2026-11-14 00:13 只读副本，main `9f031c7`）

| 项 | 现状 | 位置 |
|---|---|---|
| 画像表 | `user_profiles(user_id UNIQUE)`，已经是一人一份；取样跨群+私聊 | `database_manager.py:294`、`profile_service.py:323` |
| 好感度表 | `user_affection(group_id NOT NULL, UNIQUE(user_id, group_id))` | `database_manager.py:366-405` |
| 好感度触发 | 只有群消息：`if robot.msg_type == "group" and robot.group_id` | `main.py:636` |
| 私聊计分 | 直接返回：`if not group_id: return None` | `affection_service.py:283`、`:355` |
| 私聊情绪分 | `judge_turn` 在 `not robot.group_id` 时返回 | `judge_service.py:56` |
| 画像触发 | 只在群消息提交 `analyze_user` | `main.py:544` |
| 说话人上下文 | 非群聊直接返回空，私聊拿不到画像/关系/记忆 | `main.py:395` |
| 关系块 | 无 `group_id` 返回空 | `affection_service.py:433` |
| 面板 | `/api/affection` 直接列分量行；`/<user_id>` 强制要 `group_id` | `main.py:865-920` |
| 数据量 | `user_affection` 80 行 / 72 人 / 11 群；`user_profiles` 59；`profile_memories` 207；分数 42.3–85.0 | 只读副本 |

真实用户分布：5 人既有私聊又有群聊，4 人只有私聊（其中用户 A 43 条、用户 B 19 条是真实用户，另两个是 `test`/空 id）；2 人多群（用户 C 6 个群、用户 D 1 个群但私聊 2,261 条）。**纯私聊用户现在 0 好感度、0 画像**——这是本阶段要补的洞。

## 1. 统一模型：一人一个总分，群/私聊是分量

不改 `user_affection` 的语义，只给它加一列 `scope`：

- `scope='group'`：现有行，`group_id` 是群号；**旧行默认就是它**。
- `scope='private'`：私聊分量，`group_id=''`（`NOT NULL` 允许空串，`UNIQUE(user_id, group_id)` 保证一人一行）。
- `user_profiles` 不动：本来就一人一份。

**总分（global）不新存表，读时算**，避免双写漂移：

```
components = user_affection WHERE user_id=? AND scope IN ('group','private')
for c in components:
    score = apply_decay(c.affection_score, c.last_interaction)
    weight = min(max(c.interaction_count, 1), 200)
global = round(Σ(score*weight) / Σ(weight), 1)
```

- 没有任何分量 → 50（neutral），和现在的默认一致。
- **只有一个群分量时，global 就等于原来的群分数**（衰减后），现有 70/72 人的观感不变。
- 多群用户按互动次数加权，避免一个小群的高分把整个人拉高；权重封顶 200，防单群刷分。
- 私聊分量同样走 `apply_decay`，不会因为长期不私聊而永久保留。
- 不按「最高分」也不按「求和」：最高分会让人格分裂（群里 80、私聊 50 到底听谁），求和无上限。

`AffectionService.get_global(db, user_id)` 返回：`score / relationship / emoji / interaction_count / positive_count / negative_count / components[]`。所有读分的地方改用它。

## 2. 触发点（含纯私聊）

| 场景 | 现在 | 改成 |
|---|---|---|
| 群消息事件分 | `touch_streak` + `record_interaction(group_id)` | 不变，写 `scope='group'` 分量 |
| 私聊消息事件分 | 不触发 | `record_interaction(group_id=None/'')`，写 `scope='private'`、`group_id=''` |
| 私聊情绪分 | `judge_turn` 直接返回 | 去掉 `not robot.group_id`，`group_id or ''`，写私聊分量 |
| 私聊画像分析 | 不触发 | `_trigger_profile_update` 去掉 `msg_type == "group"` 门；`should_analyze` 本来就跨群+私聊计数 |
| 私聊说话人上下文 | `_speaker_context` 返回空 | 去掉群聊 early return；私聊也拼关系+画像+记忆 |
| 私聊共同记忆 | 已触发 | 不变 |

具体改点：

- `main.py:636`：去掉 `robot.msg_type == "group" and robot.group_id`，只留 `"affection" not in disabled`；`record_interaction(..., robot.group_id or "")`。
- `main.py:651`：`affection_on = "affection" not in disabled and has_text`，不再要求 `group_id`。
- `main.py:544`：去掉 `if robot.msg_type == "group" and robot.group_id:`，直接 `should_analyze`；`analyze_user(db, uid, name, robot.group_id or "")`。
- `main.py:395`：去掉 `robot.msg_type != "group" or not robot.group_id`；`group_id = robot.group_id or ""`；`affection` 改为 `get_global`（只读，不在读路径建行）。
- `affection_service.py:283/355`：`if not group_id: return None` 改成写 `scope='private'` 分量；`get_or_create` 插入时写 `scope`。
- `judge_service.py:56`：去掉 `not robot.group_id`；`_build_prompt` 在私聊时用「私聊中用户 X 说」，不拼群名。
- `affection_service.py:433`：`relationship_block` 不再对无群返回空，统一 `get_global`。
- `_speaker_context` 用 `get_global` 的结果传给 `build_speaker_context`，不调用 `get_or_create`（避免看一眼就建行）。

## 3. Schema 迁移（只加列，不建表、不重写）

`database_manager.py` 启动迁移里追加（都包 try/except，重复执行安全）：

```sql
ALTER TABLE user_affection ADD COLUMN scope TEXT DEFAULT 'group';
ALTER TABLE user_affection_log ADD COLUMN scope TEXT DEFAULT 'group';
```

- 新库的 `CREATE TABLE IF NOT EXISTS` 直接带上这两列；老库走 ALTER。
- 现有 80 行自动变成 `scope='group'`，`affection_score`/`interaction_count`/`last_interaction` 一个字段不动。
- 不 backfill：私聊分量在第一句私聊时创建，初始 50 + 事件分；不给历史私聊补分，避免凭空改分。
- 不新增汇总表：总分读时算，回滚只需删私聊行。
- 索引：现有 `idx_ua_user(user_id, group_id)` 够用；不新增。

## 4. 回滚方案

**代码回滚**：`scripts/rollback.sh <部署快照>`（旧代码不认识 `scope`，会忽略它；私聊行 `group_id=''` 在旧版排行榜里可能显示成空群名，属于显示问题，不崩）。

**DB 回滚**（只删本阶段新增的私聊分量，旧数据不动）：

```sql
DELETE FROM user_affection WHERE group_id = '';
DELETE FROM user_affection_log WHERE group_id = '';
```

- `scope` 列留着无害，旧代码不读；不需要 SQLite 重建表来 DROP COLUMN。
- 部署前额外备份一次 DB（部署快照只备代码）：`cp /root/kirikobot/KirikoBot/robot.db /root/kirikobot/backups/robot.db.pre-stage6.<ts>`；回滚时停容器、还原这个文件、再 `rollback.sh`。
- 验收回滚：在只读副本上执行上面两条 DELETE 后，`user_affection` 回到 80 行、`user_profiles` 59、`profile_memories` 207，旧代码 `pytest` 全绿。

## 5. 面板 / 提示词 / 工具一致

- `/api/affection`：默认按 user 聚合返回 global（`scope` 字段保留在分量里）；带 `group_id` 时返回该群分量，并附 `global_score` 方便对照。
- `/api/affection/<user_id>`：`group_id` 改成可选；默认返回 global + `components[]`（每群/私聊的分量、分数、互动次数）。
- `/api/affection/leaderboard`：无 `group_id` 用 global 排；有 `group_id` 用该群分量排。
- `check_affection` 工具：读 global；不再要求当前在群里。
- `relationship_block` / `_speaker_context`：读 global；群里也不再直接读单群分数，避免「面板 80、提示词 60」。
- 手动调整：保留按群调整；`group_id=''` 时调整私聊分量（API 允许空串即 private，`scope` 默认 group，需显式 `scope=private`）。

## 6. 验收

**单测（Ren）**
- `get_global`：单群 == 该群衰减后分数；双群按互动数加权；无分量 == 50。
- 私聊 `record_interaction` 建 `group_id=''`、`scope='private'` 行，事件分与群分量同一套。
- 私聊 `judge_turn` 写私聊分量；`_build_prompt` 私聊文案不拼群名。
- `_speaker_context` 私聊非空（有画像/记忆时）；无画像时至少关系块。
- 面板默认 global、带 group 返回分量；`/<uid>` 不带 group 返回 200。
- 迁移幂等：同一副本跑两次 init，schema 只多两列，80 行不变。
- 回滚 SQL：删私聊行后计数回到 80/59/207。

**Aya 在只读副本验收**
- 迁移前后：`user_affection` 80 行、72 人、11 群；`user_profiles` 59；`profile_memories` 207；旧分数逐行一致。
- 纯私聊真实用户 A（43 条）：模拟一条私聊 → 出现 `group_id=''` 分量；`should_analyze` 为真，画像可生成。
- 单群用户 B：global 与 `user_affection` 的 76.35（衰减后）一致。
- 多群用户 C：global 等于 6 个分量按互动数加权的值，面板/工具/提示词三处同数。

**部署即时冒烟**：healthz、三容器、traceback；私聊发一句后 `user_affection` 出现 `group_id=''` 行、`scope='private'`；面板无 500；`ai_calls` 只多一次 profile/memory（按现有 gap，不额外跑批）。

## 7. 风险与成本

- **多群用户分数会变**：只有 2 人多群，且从「当前群分数」变成加权总分；单群 70 人不变。面板要显示「分量」解释来源。
- **私聊触发画像会增加 API 调用**：`should_analyze` 是每 100 条新消息一次，纯私聊用户只有 2–3 个，成本可忽略；不做批量回填。
- **隐私**：私聊内容开始进画像/好感度（原先只进共同记忆）。数据仍在本机 DB，不新增外发；面板仅管理员可见。
- **回滚显示**：旧代码排行榜可能把私聊行显示成空群名，按 §4 删掉即可。
- **不做**：不重建表、不删旧行、不改 `SCORE_MIN/MAX` 与衰减规则、不做 per-group 提示词、不跑大评测。

## 8. 实施顺序

1. Ren 按 §3 加列 + §1 `get_global` + 单测（先不动触发）。
2. 接 §2 触发点（私聊事件/情绪/画像/上下文），补单测。
3. 改 §5 面板/工具/提示词，保证三处同数。
4. Aya 在只读副本验迁移与回滚，再部署即时冒烟。
