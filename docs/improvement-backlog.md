# KirikoBot 改进 Backlog（第一轮）

> 维护：Hina（策划）· 最后更新 2026-11-14 · 首轮基线 commit `8170c65`；R19 追加时 main `8aad829`
> 本文件是本阶段（诊断与提案）的唯一产出，仓库代码零改动。
> 排序原则：先按严重度（P0 安全/数据丢失 > P1 用户可感知 > P2 维护性），同档按修复成本从低到高。
> 证据来源：本地代码与测试、`eval/out/` 30 份历史结果、线上只读查询（docker logs/ps/stats、robot.db `mode=ro`）、Aya 安全审计复现、Ren 覆盖率与部署实测。数据截至 2026-11-13 17:30（线上 DB 到 17:25）。

---

## 0. 摘要

- **测试基线**：`python3 -m pytest tests/ -q -p no:cacheprovider` → `694 passed in 1.24s`；Ren 并入部署脚本测试后 `708 passed in 1.24s`。
- **线上**：3 容器 healthy；kirikobot 131 MB / llbot 117 MB / pmhq 335 MB；主机 2 核 1.6 G，`free -m` 已用 930 MB、swap 已用 620 MB；`/root/kirikobot` 2.2 GB，**不是 git 仓库**。
- **真实数据**：`group_messages` 132,579 行、`history` 16,726、`ai_calls` 16,768、`tool_usage` 1,815、`user_profiles` 59、`user_affection` 80、`profile_memories` 207、`stickers` 4,074（未分类 7）；前缀缓存命中 47.8%，chat 平均 1.8 s。
- **条目**：P0 4 条、P1 6 条（含 2026-11-14 追加的 R19）、P2 9 条，共 19 条。其中 9 条是 bug / 用户体验硬伤。
- **阶段 2 首批建议**：R01 / R02 / R03 —— 三个独立小 PR，都能用单测和只读验收，互不依赖。R04 要动 compose 挂载，单独一批。

## 0.1 总表（按 ROI 排序）

| 排名 | ID | 提案 | 级别 | 成本 | 状态 |
|---|---|---|---|---|---|
| 1 | R01 | send_file 路径穿越/任意文件写 + robot.db 可经未鉴权 `/download` 外泄 | P0 | 小 | 已采纳（P0 必修） |
| 2 | R02 | 面板密码明文进 docker logs + 密码文件 0777 | P0 | 小 | 已采纳（P0 必修） |
| 3 | R03 | `/webhook` 验签前整包读内存，无请求体上限，未鉴权可打 OOM | P0 | 小 | 已采纳（P0 必修） |
| 4 | R04 | dsh_agent 在容器内可任意执行，`.env` 又在 rw 挂载里 | P0 | 中 | 已采纳（P0 必修，单独一批） |
| 5 | R05 | 产物直链硬编码内网 IP、文件名未编码 | P1 | 小 | 已采纳（与 R01 同 PR） |
| 6 | R06 | 自回工具结果不落历史、不落 tool_usage：596 条空 assistant，56 条近 7 天 | P1 | 小 | 建议首批 |
| 7 | R07 | 部署链路无 git、文档仍写 `git pull`、三份 compose，误同步可覆盖 26 天数据 | P1 | 中 | 进行中（Ren 脚本，待 Aya 验） |
| 8 | R08 | 「超级大脑」页 3085 白屏：端口没监听、`web_token` 从不返回 | P1 | 小 | 待采纳 |
| 9 | R09 | `ai_server.py` 测试覆盖 9.4%，最贵的调用链没有回归网 | P1 | 中 | 待采纳 |
| 10 | R10 | 依赖不锁、镜像 2.84 GB、线上多装的包不在 requirements，重建即丢 | P1 | 中 | 待采纳（重建前必须先做） |
| 11 | R11 | DB 无 schema_version，迁移失败被静默吞掉，默认库路径随 CWD 漂 | P2 | 小 | 待采纳 |
| 12 | R12 | 限流器桶只删空的、从不按时间老化，内存只增 | P2 | 小 | 待排期 |
| 13 | R13 | compose 没有内存上限，swap 已用 620 MB，OOM 会连 QQ 一起带走 | P2 | 小 | 待排期 |
| 14 | R14 | `/tmp/dsh_run_debug.log` 无轮转、含任务原文 | P2 | 小 | 待排期 |
| 15 | R15 | 提醒发送失败仍标记 fired，用户永远收不到 | P2 | 小 | 待排期 |
| 16 | R16 | 死代码/重复实现：weather_service、test.py、三份 `_create_session`、import 即起调度器 | P2 | 小 | 待排期 |
| 17 | R17 | 文档漂移：测试数 621/681/694 三个数字，部署步骤与现实相反 | P2 | 小 | 待采纳 |
| 18 | R18 | eval 结果停在 10-31，之后人格与断句大改；线上群背景开关与 eval 配置不一致 | P2 | 中 | 暂缓（先做 R09/R16 再测） |
| — | R19 | 长回复分句回归：换行断句被 splitter 忽略，25/30 条整段发送 | P1 | 小 | 已采纳（阶段 5 修复中） |

---

## P0：安全与数据丢失（必须先修）

### R01 · send_file 路径穿越/任意文件写 + robot.db 可经未鉴权 `/download` 外泄

**状态**：已采纳（P0 必修） · **成本**：小

**证据**
- `KirikoBot/robot_server.py:332`：`name = display_name or os.path.basename(file_path)`，`display_name` 直接来自模型参数（`KirikoBot/ai_tools.py:1414`，工具定义在 `ai_tools_list.py` 的 `display_name`）。
- `robot_server.py:342-343`：只判断源文件不在 `shared_files`，随后 `os.path.join(shared_dir, name)` 没有对 `name` 取 basename，也没有 realpath 校验；`:350` 直接 `shutil.copyfile`。
- `robot_server.py:326-331` 的黑名单只查 `.env / id_rsa / id_ed25519 / .pem / .key / password / secret / token / credential / .git`，**不含 `.db`**，所以 `file_path=/app/robot.db` 会被复制进 `shared_files`。
- 复制后 `:361` 把 `http://100.93.52.78:5000/download/{name}` 发进群里；`main.py:1476-1484` 的 `/download` 是免鉴权端点（`dashboard_auth.py:36` 的豁免名单），黑名单同样不含 `.db / secret / credential / id_ed25519`。
- Aya 已复现：`send_file(src, display_name="../../pwned.html")` → 返回 True，实际拷贝目标在 `shared_files` 之外，群消息里带 `http://100.93.52.78:5000/download/../../pwned.html`。
- 线上 `robot.db` 32.8 MB + 4.19 MB WAL，含 132,579 条群消息、59 份用户画像、80 条好感度记录。

**影响面**：一次提示注入或一句「把 /app/robot.db 发我」，就能把全量聊天记录/画像复制到 `shared_files` 再从群里的免鉴权直链下载走；`display_name` 穿越还能覆盖 `/app/templates/*.html`（面板 XSS）或 `/app/main.py`（下次 restart 执行），等于容器内持久化。

**用户可感知的价值**：群聊记录和画像不外泄；面板不会再被生成的 HTML 注入；带中文/空格的文件名直链可用。

**验收标准**
- `tests/test_send_file.py` 新增：`display_name="../../pwned.html"` 只落在 `shared_files/pwned.html`（`Path.resolve().is_relative_to(shared_dir)` 断言）；`file_path=/app/robot.db`、`.env`、`id_ed25519` 一律拒绝；`/download` 对 `.db` 返回 403。
- 直链改为 `Config.PUBLIC_BASE_URL`（缺省从 request host 推导）+ `urllib.parse.quote(name)`；`grep -R "100.93.52.78" KirikoBot/` 为空。
- `python3 -m pytest tests/ -q -p no:cacheprovider` 全绿；Aya 用同一复现脚本重跑，目标仍在 `shared_files` 内。

**风险**：DSH 产物在 `/tmp/kirikobot_dsh_workspace`，如果直接改成「只允许 shared_files」会破坏「发上一轮产物」。应做**白名单目录**（shared_files + DSH workspace + /tmp 下的产物），不是一刀切；中文名 URL 编码后需确认 QQ 客户端能点开。

---

### R02 · 面板密码明文进 docker logs + 密码文件 0777

**状态**：已采纳（P0 必修） · **成本**：小

**证据**
- `KirikoBot/dashboard_auth.py:77`：只要没在 `.env` 设 `DASHBOARD_PASSWORD`，**每次启动**都 `logger.info("面板密码（自动生成，保存在 %s）：%s", _PASSWORD_FILE, password)`，把口令原文打进 INFO 日志。
- 线上日志实证：`2026-11-11 17:48:49 [INFO] dashboard_auth: 面板密码（自动生成，保存在 /app/.dashboard_password）：<12 位口令>`（此处不复制口令）。Aya 统计 `docker logs | grep -c 面板密码` = **49**。
- 线上 `/root/kirikobot/KirikoBot/.dashboard_password` 是 `-rwxrwxrwx`（16 字节），`.env` 同样是 `-rwxrwxrwx`（1904 字节）；代码 `:57` 的 `os.chmod(..., 0o600)` 只在**新建**时执行，对老文件不生效。
- 面板权限：能删群及全部数据、能往所有群推消息、能读全部聊天记录、能通过 LLBot 驱动 QQ 号（`README.md` 安全一节）。

**影响面**：任何能看 docker logs 的人（宿主机、日志采集、日报）都拿到管理员口令；备份/整目录拷贝也会把这个 777 文件带走。属于「密码写在日志里」的硬洞。

**用户可感知的价值**：面板口令不再随日志扩散；文件权限从 777 收到 600，同机其他用户读不到。

**验收标准**
- 启动时只打印「已保存到 X（未在日志中显示）」；`caplog` 断言输出里不含口令本身。
- 每次 `_load_password()` 对已存在文件 `chmod 0o600`（失败要 WARNING，不是静默）；`stat -c %a .dashboard_password` = `600`。
- 重启一次后 `docker logs | grep -c "面板密码"` 中不再出现口令值。

**风险**：轮换口令会让已保存 Basic auth 的浏览器掉登录，需要在发布说明里写一句；容器内若以非 root 运行且文件属主不同，chmod 可能失败，要保留降级路径。

---

### R03 · `/webhook` 验签前整包读内存，无请求体上限，未鉴权可打 OOM

**状态**：已采纳（P0 必修） · **成本**：小

**证据**
- `KirikoBot/main.py:1600`：`if not _webhook_signature_ok(request.get_data(cache=True))` —— 先把整个请求体读进内存，**之后**才验签。
- `main.py:84` 只设了 `TEMPLATES_AUTO_RELOAD`，全仓没有 `MAX_CONTENT_LENGTH`；waitress 的请求体上限默认是 1 GB（Aya 核对）。
- 端口暴露：`docker-compose.yml:61` `5000:5000` 绑定 `0.0.0.0`；宿主机 `iptables INPUT policy ACCEPT`、未装 ufw（Aya 实测），目前只靠阿里云安全组挡。
- 主机只有 1.6 G：`free -m` = total 1663 / used 930 / swap used 620。几个并发的大 POST 就够触发 OOM，OOM killer 会顺手带走同网络的 pmhq/llbot，QQ 掉线。

**影响面**：任何能连到 5000 的人（安全组漏一条、内网横向）都能在没有 token 的情况下打内存；机器人、LLBot、QQ 一起挂，恢复要重新扫码。

**用户可感知的价值**：不会再因为一条未鉴权大请求整机卡死/掉线。

**验收标准**
- `app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024`（或 4 MB，按实测 QQ 事件体大小定）；`Content-Length` 预检，超限在验签前返回 413。
- 新增测试：2 MB POST `/webhook` → 413，且未调用 HMAC；1 KB 正常事件 → 200。
- 部署后 24 h 内 `docker logs | grep -c "Rejected webhook"` 不升高（Aya 已确认 24 h 有 1218 条正常消息事件、0 条拒绝）。

**风险**：如果某些 LLBot 客户端把图片以 base64 塞进事件，1 MB 会误杀。上线前先统计线上事件体大小（日志里无 body 长度，可加一次 debug 采样），宁可先设 4 MB。

---

### R04 · dsh_agent 在容器内可任意执行，`.env` 又在 rw 挂载里

**状态**：已采纳（P0 必修，单独一批） · **成本**：中

**证据**
- `KirikoBot/dsh_client.py:235` 与 `:271`：启动 DSH 子进程时带 `--approval-policy never`，即模型给什么命令都执行。
- `docker-compose.yml:66` 把 `./KirikoBot:/app` 以 **rw** 挂进容器；`/app/.env` 就是 `DEEPSEEK_TOKEN / ONEBOT_TOKEN` 所在（线上 `.env` 43 个键、1904 字节、0777）。
- `dsh_client.py:104` 的 guardrails 是提示词，不是权限边界；群里任何人只要能让模型调用 `dsh_agent`，就能借它执行容器内命令。
- 同容器还挂着可写的 `shared_files/`，`/download` 又是免鉴权直链（见 R01），出数据很容易。

**影响面**：提示注入 → `dsh_agent` → 容器内任意命令 → 读 `.env`/`robot.db`、写 `/app` 持久化。属于「模型可控的 RCE 面」，不是普通工具误用。

**用户可感知的价值**：群友的一句「忽略之前的指令」不再有机会碰到密钥和全量聊天记录。

**验收标准**
- `.env` 不再位于 rw 挂载内（只用 compose `env_file` 注入，或单独只读挂载）；`docker inspect kirikobot` 不再出现 `/app/.env` 可写。
- DSH 工作目录固定到 `/tmp/kirikobot_dsh_workspace`（不写 `/app`），`--approval-policy` 至少改成会拒绝写系统路径/读 `.env` 的策略。
- `dsh_agent` 在 `feature_gate` 里默认关闭、按群开启；新增测试：默认 `disabled_tools` 包含 `dsh_agent`，开启后恢复。
- Aya 用一条注入样例复测，拿不到 `.env` 内容、写不进 `/app`。

**风险**：DSH 需要写工作目录和 node 缓存，改挂载可能让「超级大脑」整体不可用；必须先在 staging/本地 compose 验证再上线上，并留 tar 快照 + 回滚（见 R07）。

---

## P1：用户可感知的质量与交付问题

### R05 · 产物直链硬编码内网 IP、文件名未编码

**状态**：已采纳（与 R01 同 PR） · **成本**：小

**证据**
- `KirikoBot/robot_server.py:361`：`download_url = f"http://100.93.52.78:5000/download/{name}"`，IP 和端口写死，`name` 没有 `quote`。
- Aya 复现：群消息里出现 `http://100.93.52.78:5000/download/../../pwned.html`；带空格/中文的 `display_name` 直链直接坏。
- 这是 tailscale 内网地址，群友的公网浏览器根本打不开。

**影响面**：每次通过 `send_file` 发非图片产物，都把内网地址广播给全群；文件名带空格/中文时「客户端没弹下载」的兜底链接也点不开。

**用户可感知的价值**：产物链接在群里直接可点，不再泄露内网拓扑。

**验收标准**：`Config.PUBLIC_BASE_URL` 可配（缺省从 request host 推导）；URL 用 `urllib.parse.quote(name)`；`grep -R "100.93.52.78" KirikoBot/` 为空；测试断言 URL 主机来自配置、中文名被编码。

**风险**：反向代理下 request host 可能不是公网域名，需要部署时显式设 `PUBLIC_BASE_URL`；没有配置时退回 `http://<localhost>:5000` 也比内网 IP 好。

---

### R06 · 自回工具结果不落历史、不落 tool_usage：596 条空 assistant，56 条近 7 天

**状态**：建议首批 · **成本**：小

**证据**
- `KirikoBot/main.py:677-682` 记录 `tool_usage.result` 时只取 `ai.tool_result_text`；但自回工具普遍不设它：`ai_tools.py:232` 的 `web_search` 只设 `ai.user_text`，`ai_tools.py:305` 的 `sticker` 也只设 `ai.user_text`；只有 tarot/dsh/send_file 设了 `handled_summary`（`ai_tools.py:139/1388/1438-1447`）。
- `main.py:716`：`saved_text = final_text or getattr(ai, "handled_summary", "") or ""`，没有 `ai.ai_text` / `ai.user_text` 兜底。自回工具本来就不产生 `final_text`，于是这一轮 assistant 内容存成空串。
- 线上数据（只读）：
  - `tool_usage.result = ''`：sticker **560/560**、web_search **337/337**、tarot 222/222、music_search 217/217、dsh_agent 45/45、check_balance 55/76。
  - `history` 里 `role='assistant' AND content='' AND tool_calls<>''` 共 **596** 条；按工具：web_search 212、sticker 143、music_search 68、tarot 53、send_voice 21、explain_self 19、check_balance 19；最近 7 天仍有 **56** 条，最近一条是 **2026-11-13 15:14:06 的 sticker**，web_search 最近一条 **2026-11-12 22:47:53**。
  - 下一轮实际拿到的是 `[已调用工具处理]` 标记（`chat_history.py` 的 load 分支），模型不知道刚才搜到了什么、抽了什么、发了哪首歌。

**影响面**：所有自回工具（20 个里 13 个）的「刚才那个」连续性；`explain_self`（执行回放）只能回放参数，看不到结果；面板/排查无法知道 web_search 是超时、额度用完还是没结果。

**用户可感知的价值**：搜完接着追问「那第二点呢」「刚才那首歌叫啥」时，机器人接得住；「你刚才怎么查的」能回放真实结果。

**验收标准**
- 新增测试：模拟一个自回工具（sticker/web_search），走完 `main_logic` 后断言 `tool_usage.result` 非空、`history` 的 assistant 行内容非空且包含工具产出摘要。
- 修复点：`main.py:677` 的 result 兜底为 `tool_result_text or handled_summary or user_text`（截 300 字）；`main.py:716` 的 `saved_text` 兜底加 `ai.ai_text`；`web_search_call` 在成功分支设 `ai.handled_summary = ai.ai_text[:300]`。
- 线上观察：部署后 24 h 新增的 sticker/web_search 行 `result<>''` 比例 ≥95%；`content='' AND tool_calls<>''` 的新增行接近 0。

**风险**：历史变长会吃 token（一条摘要 ≤300 字，只有用了工具才写）；web_search 的 `ai.ai_text` 是 300 字内的整理结果，不是原始搜索结果，存它合适；但别把 `content[:500]` 的搜索原文塞进历史。

---

### R07 · 部署链路无 git、文档仍写 `git pull`、三份 compose，误同步可覆盖 26 天数据

**状态**：进行中（Ren 的 `scripts/deploy.sh` / `rollback.sh` 已出现，待 Aya 验） · **成本**：中

**证据**
- 线上 `git -C /root/kirikobot rev-parse --is-inside-work-tree` → `fatal: not a git repository`；部署是整目录拷贝，没有版本号、没有回滚。
- `DEVELOPMENT_GUIDE.md:754` 仍写「如需在生产服务器部署，执行 `git pull` + 重建容器」，与线上现实相反；`README.md` 的「克隆项目」也不是线上实际路径。
- 本地 `KirikoBot/robot.db` 21.9 MB / **2026-10-18**，线上 32.8 MB / **2026-11-13** + 4.19 MB WAL；一次不带排除的 rsync 就会用 26 天前的库覆盖线上真实数据。
- compose 有三份：根 `docker-compose.yml`（`./KirikoBot:/app`、`5000/3085`）、被 gitignore 的 `KirikoBot/docker-compose.yml`（`container_name: kiriko_robot`、健康检查走 `/`，401 会判 unhealthy）、未跟踪的 `docker-compose-new.yml`。三份挂载/健康检查都不同。
- 线上主文件哈希与本地 HEAD 一致（main/ai_tools/prompt_builder/chat_history/dsh_deliver/docker-compose/VERSION 全同），但 Ren 发现 `ai_tools_list.py` 线上还是 11-11 的旧版（少 TTS 的 voice 参数）——「线上 = 仓库」这个假设已经破了一次。
- 目录体积：`/root/kirikobot` 2.2 GB（stickers 1.5 GB、backups 435 MB），镜像 2.84 GB（Ren 实测）。

**影响面**：一次误操作 = 覆盖线上库/密钥/贴纸，或起第二套栈（`kiriko_robot` vs `kirikobot2`）；出问题无法快速回到上一个版本。

**用户可感知的价值**：部署不再赌运气；出问题 30 分钟内能回滚到上一个代码快照，聊天记录/画像/贴纸不受影响。

**验收标准**
- `scripts/deploy.sh --plan` 只打印将执行的命令，`--dry-run` 不写远端；rsync **不带任何删除参数**，白名单只放行 `KirikoBot/*.py`、`templates/`、`static/`，硬排除 `robot.db* .env .dashboard_password backups/ stickers/ shared_files/ llbot_config/ carside_picture/`。
- 部署前在 `/root/kirikobot/.deploy-backup/<ts>/code.tgz` 打快照；部署后验证 `healthz=ok`、容器 healthy、日志无 traceback；失败时 `scripts/rollback.sh` 能恢复快照。
- `tests/test_deploy_scripts.py` 覆盖：不删除、白名单、保护名单、快照命令、健康检查、分支/脏工作区拦截。
- 文档同步：`DEVELOPMENT_GUIDE.md:754` 改成脚本入口，不再写 `git pull`；README 部署章节与脚本一致。

**风险**：rsync 路径拼错会把文件放到错误目录（脚本已 `set -euo pipefail` + 远端前置检查）；`--delete` 绝不能加回来；回滚脚本要能处理「快照之后又部署了两次」的情况（按时间戳选）。

---

### R08 · 「超级大脑」页 3085 白屏：端口没监听、`web_token` 从不返回

**状态**：待采纳 · **成本**：小

**证据**
- `docker-compose.yml:62` 映射了 `3085:3085`，但容器内没有进程监听；Aya `curl localhost:3085` → `000`（连接失败）。
- 面板 iframe 固定拼 `http://host:3085`（`KirikoBot/static/js/app.js:1881`：`let port = 3085`；`:1886` 读 `d.web_port`），`web_token` 从 `d.web_token` 取（`:1885`）。
- `main.py` 的 `api_dsh_status` 只返回 `available/bin_path/version/workspace/patch_file/timeout/sessions`，**从不返回 `web_token` / `web_port`** → 前端永远拿不到 token，iframe 即使端口通了也是 401。

**影响面**：面板里的 DSH 控制台整页不可用；3085 是一个死端口，多开一个 0.0.0.0 监听面。

**用户可感知的价值**：管理员在面板里能直接看到 DSH 状态/执行任务，不用 SSH。

**验收标准**
- 二选一：① 删掉 3085 映射，Flask 同源反代 DSH WebUI 并注入 `x-webui-token`；② 真起 3085 监听，`/api/dsh/status` 返回 `web_port` + `web_token`（token 从 `llbot_webui.py` 已有读取逻辑拿）。
- 手工验收：面板打开「超级大脑」不再白屏，能看到版本/会话/插件；`docker compose config` 里没有未使用的端口映射。
- 新增测试：`/api/dsh/status` 响应包含前端需要的字段（或前端不再依赖 3085）。

**风险**：把 LLBot WebUI 暴露到公网会扩大攻击面；保持同源反代 + Basic auth 最稳，不要新开 0.0.0.0 端口。

---

### R09 · `ai_server.py` 测试覆盖 9.4%，最贵的调用链没有回归网

**状态**：待采纳 · **成本**：中

**证据**
- Ren 用 stdlib trace 跑全量 tests：整体 55.2%（4096/7425）；`ai_server.py` **9.4%**（38/406），`ai_request:149`、`add_tool_result:265`、`follow_up_request:268`、`vision_analyze:348`、`_format_for_qq:516` 零覆盖；`sticker_collector.py` 14.0%、`scheduler.py` 33.7%（`_loop:44` 零覆盖）、`main.py` 40.2%、`ai_tools.py` 46.0%（`sticker_call:272`、`at_member_call:378`、`set_reminder_call:440` 零覆盖）。
- 线上 16,768 次 `ai_calls`，chat 平均 1.8 s、p99 4.7 s；超时/429/非 JSON 都由 `ai_request`/`follow_up_request` 处理，但没有任何单测锁行为。

**影响面**：最贵、最容易被改坏的一条链（重试、超时、usage 解析、错误文案、follow-up 的 reasoning_content 回传）没有回归保护；一次改动就可能让所有回复变成「抱歉，AI服务暂时不可用」。

**用户可感知的价值**：改提示词/重试策略时，不会悄悄把错误分支改成给用户发错误文案；发布前就能发现 429/超时路径回归。

**验收标准**
- `ai_server.py` 行覆盖 ≥70%；至少 6 个用例：正常响应 + usage、超时、429、500、非 JSON、follow-up 带 `reasoning_content`。
- 全部 mock `requests.Session`，不联网；`python3 -m pytest tests/ -q` 全绿。
- CI 里能跑（不依赖真实 token）。

**风险**：mock 过深会测到实现细节；只锁「输入 → 用户可见输出/记录」这一层，别断言请求体的每个字段。

---

### R10 · 依赖不锁、镜像 2.84 GB、线上多装的包不在 requirements，重建即丢

**状态**：待采纳（**重建镜像前必须先做**） · **成本**：中

**证据**
- `KirikoBot/requirements.txt` 全是区间约束（`flask>=3.0,<4.0` 等），没有 lock；Ren 实测线上容器装了 `edge-tts 7.3.1`、`numpy`、`scipy`、`tabulate`，requirements 里没有；反过来 requirements 里的 `bs4` 已无人引用。
- `KirikoBot/Dockerfile:1-12` 装了 Node 22 + pnpm + `@deepseek-ai/dsh@0.2.2-rc.1`；线上镜像 2.84 GB，其中 apt/npm 层 **1.76 GB**、pip 293 MB、代码层 811 KB（Ren）。根分区 40 G 已用 20 G。
- 本地 Python **3.14.10**，镜像/CI 是 **3.11**；同一份代码在两个解释器上跑测试，结果不能直接互推。
- 部署规则已经明确：**禁止 `docker compose up --build`**，因为重建会丢上述依赖。

**影响面**：任何「重建镜像」的动作都会让语音/贴纸相关功能静默失效；镜像体积让构建/传输在 2 核 1.6 G 的机器上很慢；升级依赖不可复现。

**用户可感知的价值**：重建后语音、贴纸、搜索都不掉功能；部署时间从「不敢重建」变成可预期。

**验收标准**
- `requirements.txt` 补齐实际使用依赖（先 `grep -rn "import numpy\|import scipy\|import tabulate\|import edge_tts"` 确认谁在用），删掉无人引用的 `bs4`；加 `requirements.lock`（`pip freeze` 或 `pip-compile`）。
- 镜像 <1 GB：把 DSH/node 拆成独立镜像或按需安装，或至少合并/清理 apt 层；`docker history` 有对比。
- CI 用 3.11 跑全量测试 + `pip check`；本地 3.14 不作为验收环境。

**风险**：DSH 依赖 node/pnpm，粗暴删 apt 层会让「超级大脑」挂；必须先在分支构建并跑一遍 `dsh_agent` 冒烟，再谈上线上。

---

## P2：维护性与资源

### R11 · DB 无 schema_version，迁移失败被静默吞，默认库路径随 CWD 漂

**状态**：待采纳 · **成本**：小

**证据**
- `KirikoBot/robot.db` 的 `PRAGMA user_version` = **0**；全仓 `grep user_version` 0 处。
- `database_manager.py` 有 6 处 `ALTER TABLE ... except sqlite3.OperationalError: pass`（`:166/:228/:321/:342/:404/:433`），迁移失败与「列已存在」无法区分。
- `DatabaseManager.__init__` 默认 `db_file="robot.db"`（`database_manager.py:35`），相对 CWD；`ai_tools.py:264` 的 sticker 路径每次 `DatabaseManager()` 都会新建连接并跑 `_create_table()`；Ren 实测测试会在仓库根生成一个 20 表 0 行的 `robot.db`。

**影响面**：换机/换工作目录后连到空库，表现为「画像/好感度全没了」；schema 漂移没有任何报警。

**用户可感知的价值**：不会因为启动目录不同而「失忆」；升级失败时启动即报错，而不是带着坏 schema 跑。

**验收标准**：设置并校验 `PRAGMA user_version`；迁移只对「列已存在」吞错，其他错误必须抛；DB 路径统一为绝对路径（`Config.DB_FILE`），`DatabaseManager()` 不再在 CWD 造库；新增测试在临时目录启动后断言根目录无 `robot.db`。

**风险**：已有线上库需要一次性 stamp 到当前版本；迁移逻辑改动要在库副本上演练。

---

### R12 · 限流器桶只删空的、从不按时间老化，内存只增

**状态**：待排期 · **成本**：小

**证据**
- `rate_limiter.py:143-148` 的 `_prune` 只删 `not v` 的桶；`_trim` 只对当前查询的 key 做窗口裁剪（`:107-121`），其他用户的桶即使窗口早过了也不删。
- 每 2000 次判定才 prune 一次；桶的 key 是 `(group_id, user_id)`，群多/人多时只增不减。
- 线上 `group_rate_limits` 目前只有 1 行、默认关闭，所以现在还没爆；一旦开限流并长期运行就会累积。

**影响面**：开启限流后进程内存随「历史出现过的用户数」增长，直到重启；1.6 G 机器上属于慢性风险。

**用户可感知的价值**：开限流不会让机器人越跑越胖，重启周期拉长。

**验收标准**：prune 时按「桶最后一次写入时间」淘汰（或给桶数设上限 + LRU）；新增测试：模拟 10,000 个用户各一条后推进时钟，`len(snapshot()["users"])` 有界；`pytest tests/test_rate_limiter.py` 全绿。

**风险**：误删仍在窗口内的桶会让限流短暂失效；用「窗口结束时间 < now」判据，且保留当前 key。

---

### R13 · compose 没有内存上限，swap 已用 620 MB，OOM 会连 QQ 一起带走

**状态**：待排期 · **成本**：小

**证据**
- `docker-compose.yml` 三个服务都没有 `mem_limit` / `deploy.resources.limits`。
- 线上 `docker stats`：kirikobot 131 MB、llbot 117 MB、pmhq 335 MB；主机 `free -m` total 1663 / used 930 / **swap used 620**。
- DSH 一跑就是几百 MB 的 node 进程（Aya）；叠加 R03 的未鉴权大请求，OOM 概率不低。
- OOM killer 在同一个 compose 网络里不会区分「谁该被杀」，pmhq 被杀就是 QQ 掉线。

**影响面**：内存峰值时整栈不稳定；恢复需要重新扫码，用户感知是「机器人突然消失」。

**用户可感知的价值**：DSH 跑重任务时聊天不掉线；出问题只挂一个容器，不是整栈。

**验收标准**：给三个服务设上限（按基线留余量，如 my-robot 512 MB、llbot 512 MB、pmhq 768 MB）+ `restart: unless-stopped`；`docker inspect` 能看到 limit；部署后 24 h `dmesg | grep -i oom` 为空。

**风险**：上限太低会让正常高峰被 OOM；先在线上观察一天 `docker stats` 峰值再定，别拍脑袋。

---

### R14 · `/tmp/dsh_run_debug.log` 无轮转、含任务原文

**状态**：待排期 · **成本**：小

**证据**
- `KirikoBot/dsh_client.py:226`：`with open("/tmp/dsh_run_debug.log", "a") as f:`，每次任务追加原文，没有大小上限/轮转。
- 内容包含群友的 prompt / 任务描述（可能含隐私），且容器重启后 `/tmp` 不保留，排障价值有限、磁盘风险实在。

**影响面**：长期运行后 `/tmp` 被写满（容器层），或别人拿到容器就能读到历史任务原文。

**用户可感知的价值**：不会因为日志写满导致容器异常；群聊内容不落一个无保护的明文文件。

**验收标准**：改成 `logger.debug`（走统一日志）或写到有轮转的文件（>10 MB 切分，保留 2 份）；新增测试断言不再无条件 append 固定路径；`/tmp/dsh_run_debug.log` 不再出现。

**风险**：DSH 排障少了一个原始入口；保留 `logger.debug` + 需要时临时开。

---

### R15 · 提醒发送失败仍标记 fired，用户永远收不到

**状态**：待排期 · **成本**：小

**证据**
- `scheduler.py:91-94`：`self.llbot.send_group_msg(...)` / `send_private_msg(...)` 的返回值被丢弃。
- `scheduler.py:114/118`：无论发送是否成功，`_finalize_reminder` 都把 `fired=1`（或改到明天）。
- 表里现有 2 条提醒都是 `fired=1`（`reminders` 只读查询）；没有重试/失败状态字段。
- 典型触发：LLBot 正在重连、QQ 风控、容器重启的那几秒。

**影响面**：用户设的提醒静默消失，且没有任何日志/提示；这是「答应过的事没做到」的直接体验伤害。

**用户可感知的价值**：LLBot 短暂抖动时，提醒会在恢复后补发，而不是永远丢失。

**验收标准**：发送返回 False 时 `fired` 保持 0，按 10s/1min 退避重试，最多 N 次后在日志里明确「提醒 #id 发送失败」；新增测试 monkeypatch `send_*` 返回 False → 行未标记 fired，第二次成功后才标记。

**风险**：如果接口返回 False 但实际已送达，会重复提醒；用「短退避 + 最多 3 次」控制，重复概率远小于丢提醒。

---

### R16 · 死代码/重复实现：weather_service、test.py、三份 `_create_session`、import 即起调度器

**状态**：待排期 · **成本**：小

**证据**
- `KirikoBot/weather_service.py` 被 git 跟踪（`git ls-files` 有），但全仓除自身外 0 处引用；`tests/test_feature_removal.py:73` 已确认天气工具移除。
- `KirikoBot/test.py` 是写死 QQ 号的调试脚本（未跟踪）；`tts_engine.py` / `tts_service.py` 也未被跟踪、无任何 import，且 `tts_service.py` 里 `edge_tts` 未导入（线上 2026-11-11 日志里就是 `NameError: name 'edge_tts' is not defined`）。
- `_create_session` 三份、策略不同：`ai_server.py:136`（POST/backoff 1.0）、`balance_service.py:19`（GET/total=2/0.5）、`llbot_client.py:274`（POST+GET/参数化）。
- `main.py:122` 在 import 时 `scheduler.start()`；`tests/conftest.py` 专门警告不能 import main，测试只能靠拆分模块绕开。

**影响面**：读代码要判断哪份是真的；重试策略改一处漏两处；import 副作用让 `main.py` 无法直接单测；`tts_service.py` 的 600 权限还导致过线上 `PermissionError: '/app/tts_service.py'`（2026-11-11 17:47 日志）。

**用户可感知的价值**：语音/天气这些「已经不在菜单里」的残留不再半夜报错；重试行为统一，超时表现一致。

**验收标准**：删除 `weather_service.py`、`test.py`、`tts_engine.py`、`tts_service.py`（或明确接线并把 `edge_tts` 加进 requirements，二选一）；`_create_session` 收敛成一个工厂并保留三处不同策略的参数；`main.py` 的 `scheduler.start()` 移到 `if __name__ == "__main__"` 或显式 `create_app()`；`pytest` 全绿，`grep -rn "weather_service\|tts_service\|tts_engine"` 为空。

**风险**：DSH 或外部脚本可能直接 import 这些模块；删前 `grep` 全仓 + 搜线上 `/root/kirikobot`，并在发布说明里写「已删」。

---

### R17 · 文档漂移：测试数 621/681/694 三个数字，部署步骤与现实相反

**状态**：待采纳 · **成本**：小

**证据**
- `README.md:6` 写「20 个工具 · **681** 条测试」，`README.md:196` 又写「tests/ # **621** 条测试」；实际 `python3 -m pytest tests/ -q` = **694**（17:30 基线），Ren 并入部署脚本测试后 **708**。
- `DEVELOPMENT_GUIDE.md:754` 写「执行 `git pull` + 重建容器」；线上无 git（`fatal: not a git repository`），且部署规则明确禁止重建镜像。
- `VERSION` 文件是 `1.0.0`，从 9 月到 11 月没变；面板/`healthz` 都不暴露当前运行版本。
- README 部署章节写 `git clone`，线上实际是 rsync 整目录拷贝。

**影响面**：新同事按文档操作会走错路（git pull / rebuild），对测试规模没有准确预期。

**用户可感知的价值**：文档可信；接手的人不需要先问一遍「到底怎么部署」。

**验收标准**：README 两处测试数统一为当前实际值，并改成「以 `pytest tests/ -q` 输出为准」；DEVELOPMENT_GUIDE 部署章节改指 `scripts/deploy.sh`/`rollback.sh`；`/healthz` 或面板返回 `VERSION` + git sha（可选）。

**风险**：测试数会继续变，写死数字必然再次过期；最好只保留命令，不写死条数。

---

### R18 · eval 结果停在 10-31，之后人格与断句大改；线上群背景开关与 eval 配置不一致

**状态**：暂缓（先做 R09/R16 再测） · **成本**：中

**证据**
- `eval/out/` 最新一份是 `20261031_144143.json`（2026-10-31 14:41）；之后 11-11 改了人格（`c45cc72`），11-12 重构了断句（`850760d`），期间没有新的 eval 结果。
- 10-31 的 `learn_on/learn_off` 备注写「线上配置」，包含 `group_context=True`、`style_rule`、`tight`、`static_exemplars`；但线上 `.env` 只有 43 个键，**没有 `GROUP_CONTEXT_ENABLED`**（默认 0，关闭），`DEEPSEEK_REASONING_EFFORT=low`。
- 早期 `group_ctx` 对比是 22/15/23（`20261031_115122.json`），后期 `tight_nogroup` 49/39 vs `style_tight` 49/31（`20261031_120059.json`）方向并不一致。
- `Config` 注释本身也写「turn it on with GROUP_CONTEXT_ENABLED=1 and re-measure with eval/chat_replay.py before deciding」。

**影响面**：现在讨论「群背景开不开」「人格改得好不好」都没有当前数据支撑；10-31 的胜率不能证明 11-13 的代码。

**用户可感知的价值**：用 200 条真实回合重新测一次，决定群背景开关；避免凭印象改提示词。

**验收标准**：用**只读 DB 副本**跑 `python3 eval/chat_replay.py --db <copy> --n 200 --variants prod_old,<当前配置>`，产出 `eval/out/<新时间戳>.json`；报告写清 ctx/human/persona 胜率与样本量；据此决定 `GROUP_CONTEXT_ENABLED`，并把决定写回本文件。

**风险**：eval 要花 token（200 样本 × 变体 × 裁判），且裁判有噪声；`n=200` 是当前配置能接受的下限，不要用 40 样本下结论。

> 2026-11-14 追加 R19：用户报告的分句回归，不在首轮 ROI 排序内，按 P1 立即插入阶段 5 修复。

---

### R19 · 长回复分句回归：换行断句被 splitter 忽略，25/30 条整段发送

**状态**：已采纳（阶段 5 插队修复，Ren 实现中；PR 待开） · **成本**：小

**证据**（线上只读副本，2026-11-14 00:13，`history` 8,231 条 assistant）
- 最近 30 条 `length(content)>50` 的 assistant 回复（id 17043–17167，21:22–23:48）：**30/30 的每一行都不以 `。！？` 结尾**；其中 25 条全文连一个 `。！？` 都没有，5 条只有 1 个且不在行尾。
- 这些回复的行数 2–9，**3–4 行占 22/30**；长度 min 57 / 中位 134 / max 448。
- 把这 30 条原文喂给 main 的 `split_into_pacing_chunks`（detached worktree @ `8aad829`）：**25 条返回 1 段**（整段发送），5 条只拆出 2 段，0 条按行拆成 3 段。
- 近 7 天 `length(content)>50` 的 assistant 回复 472 条，其中 **463 条（98.1%）行尾没有句末标点**，说明不是个别样本。
- 代码路径：`KirikoBot/robot_server.py:183` `split_into_pacing_chunks`；`:210` 只有在「每行都以 `_SENTENCE_END` 或闭引号结尾」时才按行拆；`:214` 否则先把换行归一成空格，再按句末标点扫描。模型改成换行断句后，`:210` 永远不成立，`:214` 后也找不到句末标点，于是 `:216` 附近返回 `[text]`。
- 调用链：`RobotServer.reply_chunked`（`robot_server.py:343`）→ `split_into_pacing_chunks`；DSH 产物发送也走同一函数（`dsh_deliver.py:87`）。

**影响面**
- 所有 >50 字、靠换行分段的回复（技术解释、长建议、情绪回应）都整段发送；QQ 上一屏一大段，用户直接感知为「分句失效」。
- 短回复（≤35 字）、代码块、列表、表格、≥2 URL 仍会走「整段发送」白名单，不受影响。

**用户可感知的价值**
- 恢复 2–3 条自然发送，阅读节奏和「活人感」回来；不用改模型输出，也不用在提示词里硬塞句号。

**验收标准**
- 4 行以上、每行 ≥8 字、行尾不是逗号/分号/冒号/顿号/开括号的回复：合并成 **≤3 段**，拼接后正文不丢（允许换行差异）。
- ≤35 字、代码块、列表、表格、≥2 URL：仍返回 1 条。
- 空行、引号、括号不误伤；行尾闭引号不能丢；一句话不能被拆成两半。
- 单测覆盖以上边界；用这 30 条真实长回复回归：3–4 行且不属于白名单的，段数 ≥2，且拼接无损。
- 部署后即时冒烟正常；线上观察交给用户，段数分布由用户看。

**风险**
- 误拆代码/引用/列表；合并相邻行时丢内容；把一句完整的话拆成两半。
- 缓解：结构化内容白名单、行长 ≥8、行尾软标点不切、拼接校验、逐条单测；>3 段时只合并最短相邻行，不按字符硬切。

**不做**
- 不改模型提示词逼它输出句号；不恢复旧的「按句号切」逻辑；不在发送层做字符数截断。

## 1. 决策记录（采纳 / 否决 / 暂缓）

| 决定 | 结论 | 原因 |
|---|---|---|
| rsync 加 `--delete` 部署 | **否决** | 会删除线上 `robot.db*`、`backups/`、`stickers/`、`shared_files/`，一次误操作不可逆 |
| 常规部署执行 `docker compose up -d --build` | **否决** | requirements 缺 edge-tts/numpy/scipy/tabulate，重建即丢依赖；只允许 `restart my-robot` |
| 直接把 `GROUP_CONTEXT_ENABLED` 改成 1 | **暂缓** | eval 结果停在 10-31，之后的提示词已改；先按 R18 重测再决定 |
| 删掉「超级大脑」页/3085 映射就完事 | **否决** | 管理员需要 DSH 控制台；按 R08 修数据链路，而不是下线功能 |
| 把自学习笔记加回来 | **已否决** | 10-31 两次 learn_off 都优于 learn_on（66/44、66/55），且 `tests/test_learning_removed.py` 锁住「不再存在」 |
| 清空/重建 stickers 库 | **否决** | 4,074 张、1.5 GB，是产品资产；只做去重/分类，不重建 |
| 用本地 `robot.db` 覆盖线上库 | **否决** | 本地是 10-18 的 21.9 MB，线上是 11-13 的 32.8 MB；只能单向从线上只读导出副本 |
| 阶段 2 首批 R01/R02/R03 | **采纳** | 三个独立小 PR、都能单测、不互相依赖；R04 动挂载，单独一批 |
| R19 分句回归修在 splitter，而不是要求模型改回句号 | **已采纳** | 模型改用换行断句是自然演化；让模型回退会牺牲排版，且在提示词里加硬规则会继续漂。改 splitter 的边界判定更稳、可单测 |

## 2. 证据采集（可复现）

```bash
# 测试基线（本地）
cd /home/bosak/Documents/Projects/KirikoBot-LLBot
python3 -m pytest tests/ -q -p no:cacheprovider        # 694 passed in 1.24s（17:30）
                                                        # 708 passed in 1.24s（Ren 并入部署脚本测试后）

# 线上容器 / 主机（只读）
ssh -F ~/.ssh/config -i ~/.ssh/Fedora.pem root@47.108.183.60 \
  'docker ps --format "{{.Names}}\t{{.Status}}"; free -m; docker stats --no-stream'
# → 3 容器 Up/healthy；total 1663 / used 930 / swap used 620

# 线上库（只读，mode=ro；不写、不搬、不删）
ssh ... 'python3 - <<"PY"
import sqlite3
con=sqlite3.connect("file:/root/kirikobot/KirikoBot/robot.db?mode=ro",uri=True)
c=con.cursor()
print(c.execute("select count(*) from group_messages").fetchone())   # 132579
print(c.execute("select count(*) from history where role=\"assistant\" and content=\"\" and tool_calls<>\"\"").fetchone())  # 596
print(c.execute("select tool_name,count(*),sum(result=\"\") from tool_usage group by 1 order by 2 desc limit 8").fetchall())
# → sticker 560/560、web_search 337/337、tarot 222/222 的 result 全空
PY'

# 线上日志（48h，只读）
ssh ... 'docker logs --since 48h kirikobot 2>&1' > /tmp/kb-logs.txt
# → 3574 行；2307 group + 124 private 消息；149 轮对话；4 条 main ERROR（语音）、3 条 tts ERROR
# → 2026-11-11 17:47 PermissionError: '/app/tts_service.py'
# → 2026-11-11 17:49 NameError: name 'voice_choice' is not defined
# → 2026-11-11 17:48 dashboard_auth 把 12 位面板口令原文打进 INFO 日志（此处不复制）

# 线上不是 git 仓库
ssh ... 'git -C /root/kirikobot rev-parse --is-inside-work-tree'   # fatal: not a git repository
```

> 说明：本轮所有线上操作均为只读（`docker ps/logs/stats`、`ls`、sqlite `mode=ro`），没有重启容器、没有写线上文件、没有复制线上库到本地项目目录。
