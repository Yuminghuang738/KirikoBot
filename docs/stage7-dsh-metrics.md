# 阶段 7：DSH 超级大脑指标口径与基线

> 目的：给「超级大脑」定可复算的指标（耗时 p50/p90、成功率、会话恢复率、队列等待），并用线上只读数据采一次基线。当前 `tool_usage` 没有耗时/状态/会话字段，45 条 dsh 记录的 `result` 全是空串，所以基线只能从日志和 AI 调用时间反推；这份文档同时说明要补哪些字段。

## 1. 指标定义

统计窗口默认 **滚动 7 天**，报告必须带 `n`；样本 < 10 只报原始值，不报 p90。

| 指标 | 定义 | 计算 |
|---|---|---|
| 任务耗时 `duration_ms` | 从 `DshAgentTool` 入口到结果可发送（含兜底发送尝试） | `finish_ts - start_ts`，单位 ms；只对 `status='ok'` 报 p50/p90，另报全状态 p90 |
| 成功率 | 被接受的任务里最终有可用结果的占比 | `ok / (ok+empty+timeout+error+interrupted)`；`rejected` 不计入分母，单独报拒绝率 |
| 超时率 | 跑到 `DSH_TIMEOUT` 被中止的占比 | `timeout / 被接受任务` |
| 会话恢复率 | 有存量 session_id 的任务里，续写成功的占比 | `session_recovered=1 / (session_recovered=1 + session_recovered=0)`；`-1` = 本来就没有会话，不进分母 |
| 队列等待 `queue_wait_ms` | 从提交到真正拿到并发槽的时间 | 仅在有并发上限后有意义；报 p50/p90 |
| 拒绝率 | 队列满/超过并发上限被拒的占比 | `rejected / (accepted+rejected)` |
| 文件兜底率 | DSH 没自己 deliver、由 `DshAgentTool` 兜底发送的占比 | `fallback / ok`（日志里 `DshAgent 兜底发送文件` 或 `already_delivered=False`） |

**状态取值**（`tool_usage.status`）：

| 值 | 含义 |
|---|---|
| `running` | 已开始、尚未结束；进程重启后由启动清理改成 `interrupted` |
| `ok` | 有非空文本结果，或 DSH 已自行 deliver，或至少发出一个文件 |
| `empty` | 进程正常结束但没有文本/文件（如「未产生文字输出」） |
| `timeout` | 超过 `DSH_TIMEOUT` 被中止 |
| `error` | 异常、非零退出、运行时不可用 |
| `interrupted` | 进程被杀/服务重启，任务没有收尾 |
| `rejected` | 并发/队列上限拒绝，任务没启动 |

**成功率的边界**：`empty` 算失败（用户没拿到东西）；`interrupted` 算失败（用户等待后没结果）；`rejected` 不算「执行失败」，单独看拒绝率。

## 2. 采集实现（只加列，不建表）

`tool_usage` 追加（幂等 ALTER）：

```sql
ALTER TABLE tool_usage ADD COLUMN duration_ms INTEGER DEFAULT NULL;
ALTER TABLE tool_usage ADD COLUMN status TEXT DEFAULT '';
ALTER TABLE tool_usage ADD COLUMN session_key TEXT DEFAULT '';
ALTER TABLE tool_usage ADD COLUMN session_recovered INTEGER DEFAULT -1;
ALTER TABLE tool_usage ADD COLUMN queue_wait_ms INTEGER DEFAULT 0;
ALTER TABLE tool_usage ADD COLUMN error TEXT DEFAULT '';
```

写入方式（一次任务只留一行，避免和 `main.py` 的通用记录重复）：

1. `DshAgentTool.dsh_agent_call` 入口先 `db.start_tool_usage('dsh_agent', user, group, args, session_key)`，`status='running'`，返回 `row_id`；`main.py:710` 对 `fn='dsh_agent'` 跳过通用 `record_tool_usage`。
2. 结束时 `db.finish_tool_usage(row_id, result, reasoning, duration_ms, status, session_recovered, queue_wait_ms, error)`。
3. 启动时 `UPDATE tool_usage SET status='interrupted' WHERE tool_name='dsh_agent' AND status='running'`——进程重启后不可能还有活任务，这样被重启杀掉的任务也能进成功率分母。
4. `/api/dsh/execute` 的网页任务也写一行（`user_id='web_console'`，`session_key='web_console'`），与聊天任务分开报。

当前代码落点：`ai_tools.py:1334`（入口）、`:1352`（`t_start`）、`dsh_client.py:195`（`run_task`）、`dsh_client.py:138`（锁清理）、`main.py:710`（通用 tool_usage 写入）、`database_manager.py:1932`（写入方法）。

## 3. 线上基线（只读副本 2026-11-14 00:13）

数据：`tool_usage` 中 `dsh_agent` **45 条**（2026-11-08 14:22 ～ 11-12 15:21），`result` 全为空串；45 条都有一条对应的「[超级大脑] 任务完成」assistant 消息；`app_state` 里活跃 DSH 会话 6 个。

**耗时（代理口径）**：起点取该任务前最近一次 `kind='chat'` 的 `ai_calls` 时间（45/45 在 600 秒内匹配上，且 group_id 全部一致），终点取 `tool_usage.timestamp`（handler 结束后写入）。

| 口径 | n | p50 | p90 | max |
|---|---:|---:|---:|---:|
| 全部（11-08 ～ 11-12） | 45 | **100 s** | **269 s** | 397 s |
| 去掉 11-08 测试爆发（11-10 ～ 11-12） | 14 | 106.5 s | 167 s | — |
| 调试日志里能对上起止的 4 条 | 4 | 88.6 s | 225.1 s | 225.1 s |

分布：>60 s 26/45，>180 s 7/45，>300 s 5/45，>600 s 0/45。注意 45 条里 31 条集中在 11-08，基线偏测试；去爆发后 p50 仍在 100 s 量级。

**成功率**：45 条完成记录在 7 天日志里 **0 条** `任务执行时间过长` / `没有产出有效回答` / `DSH 进程非零退出` / `执行 DSH 初始化任务失败`；但 `result` 为空，无法从 DB 判 status。采样到一个**中断**：`13:00:09` 启动的任务，服务在 `13:03:25` 重启，之后既没有完成行也没有「任务完成」历史。按调试日志里 5 个聊天任务算：4 完成、1 中断 = **80%**（n=5，只作方向）；库内完成口径 45/45 无显式失败。

**会话恢复**：7 天日志 `已自动清理孤儿会话锁` **23 次**（有存量 session 的续写尝试），`DSH 恢复会话 ... 失败` **5 次** → 恢复率约 **78.3%**（23-5）/23。失败后都会降级新会话，但用户要多等一轮。当前 `_clean_stale_session_lock` 是无条件删锁（注释写 10 秒），这是恢复失败的可疑来源，Ren 这轮修。

**队列/并发**：目前没有并发上限，`DshAgentTool` 同步执行，p90 269 s 意味着聊天线程被占约 4.5 分钟；没有队列等待数据。拒绝率、`queue_wait_ms` 从本阶段上线后开始记。

**文件兜底**：7 天日志 `DshAgent 兜底发送文件` 7 次。

## 4. 目标与验收

不设「耗时必须低于 X 秒」的硬目标（任务复杂度差异太大），只要求**可观测、可比较**；质量门槛：

- 成功率 ≥ **95%**（`ok / 被接受`），超时率 ≤ 2%，`interrupted` 单独报且部署后应为 0（除非人工重启）。
- 会话恢复率 ≥ **90%**（修锁后；基线 78.3%）。
- 队列/并发上限上线后：`rejected` 可见，`queue_wait_ms` p90 报出来；不能出现「任务占满聊天线程导致整轮超时」。
- 验收方式：`tool_usage` 里随机抽 10 条 dsh 任务，`status`/`duration_ms`/`session_recovered` 非空且与日志能对上；Aya 在只读副本和线上各抽一次；页面 3085 能打开并看到任务列表/状态。
- 指标看板（如果做）：7 天 p50/p90、成功率、恢复率、超时率、拒绝率，按 `group/private` 拆分。

## 5. 限制

- 代理耗时把 AI 调用的收尾算进了起点，略高估；新字段上线后以 `duration_ms` 为准，不与基线直接比绝对值，只看同口径趋势。
- 45 条样本集中在 11-08，且当时有测试爆发；去爆发子集只有 14 条，p90 不稳。
- 恢复率是「尝试数 - 失败日志数」，成功续写没有显式日志，可能低估/高估；新字段 `session_recovered` 上线后为准。
- 当前 `result` 全空，无法从 DB 判断「是否有用」；成功率依赖新 `status`，旧数据不回填。
- `tool_usage` 是通用表，dsh 任务的 `arguments` 只存前 500 字符（`database_manager.py:1939`），任务描述可能截断；指标不依赖原文。

## 6. 不做

- 不改 DSH 运行时本身、不改超时 600 s、不做批量压测；不为了指标好看去重试任务。
- 不删旧 `tool_usage` 行、不重建表；旧行 `status=''` 视为「未采集」，不进新指标分母。
