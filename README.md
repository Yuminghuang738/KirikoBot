<p align="center">
  <img src="assets/icon.png" width="136" height="136" alt="KirikoBot 图标">
</p>

<h1 align="center">KirikoBot</h1>

<p align="center">
  <b>一个住在 QQ 群里的 20 岁女生</b><br>
  会接梗，会傲娇，会弹电吉他，也会记得你上次说过什么。
</p>

<p align="center">
  <a href="https://github.com/Yuminghuang738/KirikoBot/actions/workflows/ci.yml">
    <img src="https://github.com/Yuminghuang738/KirikoBot/actions/workflows/ci.yml/badge.svg" alt="CI">
  </a>
  <img src="https://img.shields.io/badge/Python-3.11%20%7C%203.12-3776AB?logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/tests-1000%2B-4c1?logo=pytest&logoColor=white" alt="Tests">
  <img src="https://img.shields.io/badge/license-GPL--3.0-blue" alt="License">
  <img src="https://img.shields.io/badge/version-1.0.0-ff69b4" alt="Version">
</p>

---

## 这是什么

KirikoBot 是一个跑在 QQ 上的聊天机器人：**LLBot（OneBot）负责 QQ 接入，DeepSeek 负责对话，Flask + SQLite 负责记忆、工具和管理面板**。

它不是那种问一句答一句的客服机器人。Kiriko 有完整的人设、会看气氛、会记住群里谁是谁；你反复烦她，她会慢慢变敷衍，但不会真的翻脸。她还有 20 个工具：点歌、抽塔罗、发表情包、查好感度、设提醒、联网搜索，以及一个能跑代码、查资料、生成文件的「超级大脑」。

当前仓库：**20 个工具 · 1,000+ 条测试 · 3 个 Docker 容器 · Python 3.11**。

---

## 亮点

### 🎀 人设不是一句「你是一个可爱的女生」

- 6,800+ 字的完整人设：20 岁大三、计算机专业、玩摇滚、打游戏、追番、养猫狗
- 说话短、不反问、不 AI 腔；傲娇只留给熟人，被夸才嘴硬一下
- 被追问设定、要求复述/翻译/代码块包裹时，一律挡回去，并有泄露守卫兜底
- 提示词按「稳定前缀 + 每轮变化内容」拆开，尽量不打断 DeepSeek 的前缀缓存

### 🧠 对话记忆

- 默认保留最近 **24 条**原文（可按 `MEMORY_RAW_MESSAGES` 调整）
- 较早对话可滚动压缩成摘要，摘要默认**关闭**，在面板里按群灰度开启
- 用户画像一人一份，取样跨群 + 私聊；私聊也会触发画像与好感度更新
- 共同记忆单独存：具体发生过的事，比「性格开朗」这种标签有用

### 💕 好感度属于「人」，群只是分量

- 每个用户一个总分，群聊/私聊各算一个分量，按互动次数加权
- 事件制加分：第一次来、当天首次、连续出现、深夜、发图、情绪分
- 长期不来会掉分，掉分速度随离开时间加快
- 五档关系：冷淡 → 普通 → 友好 → 亲密 → 挚友；提示词、面板、工具读同一个分

### 🛠 20 个工具

| 分类 | 工具 | 说明 |
|---|---|---|
| 重度任务 | 🧠 `dsh_agent` | 桥接本地 DeepSeek Harness，跑代码、长程调研、找/生成文件 |
| 信息 | 🔍 `web_search` | DeepSeek 官方 Search API，结果精简后再交给模型 |
| 生活 | 🃏 `tarot` / `tarot_history` | 每日抽牌 + 历史记录 |
| | 🎵 `music_search` | 网易云搜歌 → QQ 音乐卡片 |
| | 😊 `sticker` | 从表情包库里按情绪挑一张 |
| 互动 | 💕 `check_affection` / `affection_leaderboard` | 查好感度 / 排行榜 |
| 群功能 | 📁 `send_file` | 发本地文件，或让超级大脑找/生成后直接上传 |
| | ⏰ `set_reminder` / `list_reminders` / `delete_reminder` | 秒/分/时/天，支持每日重复 |
| | 👥 `at_member` / 💬 `recall_message` | @群友 / 撤回自己刚发的消息 |
| | 📊 `group_stats` / 📖 `read_context` | 发言统计 / 读近期群聊补充语境 |
| | 💰 `check_balance` | DeepSeek API 余额 |
| | 🎤 `send_voice` | 用语音说话（走 LLBot 的 AI 语音） |
| | 🔁 `explain_self` | 调试：回放上一轮工具链与回复原文 |
| 常驻 | 🕐 `get_current_time` | 相对时间计算 |

> 除 `get_current_time` 外，其余功能都能在面板里**按群/按用户独立开关**（19 项）。

### 🖼 图片、语音与群聊

- 图片先转文字描述，再走正常聊天链路；人格、历史、群语境、好感度全都带上
- 语音只在合适的时候发，发不出去自动回退文字
- 支持引用回复的上下文还原：别人引用了 Kiriko 说过的话，被引用的内容会附上
- 群聊限流可按「单用户」和「整群」两种模式组合，超额静默丢弃

### 🖥 管理面板

实时日志（SSE）· 对话记录 · 用户画像 · 好感度 · 提醒 · 塔罗 · 表情包库 ·
群管理 · 群活跃 · 聊天回看 · AI 用量与成本 · 功能开关 · 频率限制 ·
LLBot 连接状态 · **超级大脑（DSH）任务页**

---

## 架构

```
QQ 客户端
    │
    ▼
┌─────────┐     ┌──────────────┐     HTTP webhook      ┌──────────────────────────┐
│  pmhq   │────▶│    llbot     │───── HMAC-SHA1 ─────▶│  my-robot (Flask/waitress)│
│ QQ 协议 │     │ OneBot :3080 │                       │  :5000                   │
└─────────┘     └──────────────┘                       │                          │
                                                       │  prompt_builder / PERSONA│
                                                       │  memory / profile        │
                                                       │  affection / judge       │
                                                       │  20 tools ──▶ DSH Agent  │
                                                       │  scheduler / reminders   │
                                                       │  dashboard (Basic auth)  │
                                                       └────────────┬─────────────┘
                                                                    │
                                                       ┌────────────▼─────────────┐
                                                       │  SQLite  robot.db        │
                                                       │  history / profiles /    │
                                                       │  affection / memories /  │
                                                       │  tool_usage / ai_calls   │
                                                       └──────────────────────────┘
                                                                    │
                                                       DeepSeek API（chat / search / vision）
```

三个容器：

| 服务 | 镜像/构建 | 作用 | 端口 |
|---|---|---|---|
| `pmhq` | `linyuchen/pmhq:7.3.2` | QQ 协议端 | 内部 |
| `llbot` | `linyuchen/llbot:7.11.0` | OneBot 框架 + WebUI | `3080` |
| `my-robot` | `KirikoBot/Dockerfile`（python:3.11-slim） | 机器人本体 + 面板 | `5000` |

---

## 依赖与工具链

### 运行依赖（`KirikoBot/requirements.txt`）

| 包 | 版本约束 | 用途 |
|---|---|---|
| `flask` | `>=3.0,<4.0` | Webhook 与面板 |
| `requests` | `>=2.31,<3.0` | OneBot / DeepSeek / 搜索 API |
| `python-dotenv` | `>=1.0,<2.0` | 读取 `.env` |
| `markdown` | `>=3.5,<4.0` | 面板 Markdown 渲染 |
| `waitress` | `>=2.1,<4.0` | 生产 WSGI 服务器 |
| `lxml` | `>=5.0,<6.0` | 网页解析 |
| `Pillow` | `>=10.0,<13.0` | 图片处理 / 表情包 |
| `imagehash` | `>=4.3,<5.0` | 表情包去重 |

### 开发依赖（`KirikoBot/requirements-dev.txt`）

`pytest` · `pytest-cov` · `ruff`（CI 与本地测试用，运行镜像不需要）。

### 容器内额外依赖（**不在 requirements.txt**）

线上镜像还装了下面这些，用于语音与统计实验：

| 包 | 线上版本 | 说明 |
|---|---|---|
| `edge-tts` | 7.3.1 | 语音合成实验 |
| `numpy` | 2.4.6 | 数值/统计 |
| `scipy` | 1.17.1 | 数值/统计 |
| `tabulate` | 0.10.0 | 表格输出 |

> ⚠️ 这几个包**没有写进 requirements.txt**。所以线上只能 `rsync + docker compose restart my-robot`，
> **禁止 `docker compose up --build`**：重建镜像会把它们丢掉。

### DSH 超级大脑运行时

- Node.js 22 LTS
- `@deepseek-ai/dsh@0.2.2-rc.1`（Dockerfile 内安装）
- 默认超时 600 秒，最多 2 个任务并发；满了会立刻回「排队」而不是占住聊天线程

### 工具链

| 层 | 选型 |
|---|---|
| 模型 | `deepseek-flash`（DeepSeek V4.1 Flash，多模态；可用 `DEEPSEEK_MODEL` 覆盖） |
| 语言 | Python 3.11（CI 同时测 3.11 / 3.12） |
| Web | Flask 3 + waitress（16 线程） |
| 数据 | SQLite（WAL），无外部数据库 |
| 容器 | Docker Compose，三服务 |
| 测试 | pytest + pytest-cov，覆盖率门槛 65% |
| 静态检查 | ruff（E9/F63/F7/F82） |
| CI | GitHub Actions：lint / pip check / 3.11 / 3.12 |

---

## 快速开始（全新部署）

### 1. 克隆

```bash
git clone https://github.com/Yuminghuang738/KirikoBot.git
cd KirikoBot
```

### 2. 配置

```bash
cp KirikoBot/.env.example KirikoBot/.env
$EDITOR KirikoBot/.env
```

最少填这四项：

```ini
ROBOT_QQ="机器人 QQ 号"
ONEBOT_API="http://llbot:3000"
ONEBOT_TOKEN="OneBot 连接令牌"
DEEPSEEK_TOKEN="你的 DeepSeek API Key"
```

> 真实密钥只放在 `KirikoBot/.env`，这个文件已在 `.gitignore` 和部署保护名单里，不会进仓库。

### 3. 启动

```bash
docker compose up -d --build
```

首次安装需要 build；**已有线上环境不要重建**，代码更新走 `scripts/deploy.sh`。

### 4. 登录 QQ

打开 `http://<机器地址>:3080`，手机 QQ 扫码。登录成功后 OneBot 的 3000 端口才会就绪。

### 5. 打开面板

```
http://<机器地址>:5000
```

用户名默认 `admin`，密码在 `KirikoBot/.dashboard_password`（首次启动自动生成），
也可以在 `.env` 里用 `DASHBOARD_USER` / `DASHBOARD_PASSWORD` 覆盖。

---

## 部署与回滚

生产环境用仓库里的脚本，不手搓 rsync：

```bash
# 部署当前 main（只同步白名单文件，自动打快照 + 健康检查）
scripts/deploy.sh

# 只看计划，不动服务器
scripts/deploy.sh --plan

# 回滚到某个快照
scripts/rollback.sh 20261114-002246
scripts/rollback.sh --latest
```

部署规则（脚本里硬编码，没有关闭开关）：

- **只同步** `KirikoBot/*.py`、`templates/`、`static/`、`requirements.txt`
- **不删除**远端文件（rsync 不加 `--delete`）
- **永不覆盖** `robot.db*`、`.env`、`.dashboard_password`、`backups/`、
  `stickers/`、`shared_files/`、`llbot_config/`、`carside_picture/`
- 部署前把代码打 tar 到 `/root/kirikobot/.deploy-backup/<时间戳>/code.tgz`
- 部署后验收 `healthz`、容器状态、日志无 traceback；失败直接打印回滚命令
- 服务器是 2 核 1.6G：**不在上面跑构建和测试**

---

## 配置项

完整清单见 `KirikoBot/.env.example`，这里只列分组和用途（**不写任何密钥**）：

| 分组 | 变量 |
|---|---|
| 必填 | `ROBOT_QQ` `ONEBOT_API` `ONEBOT_TOKEN` `DEEPSEEK_TOKEN` |
| 模型 | `DEEPSEEK_API` `DEEPSEEK_MODEL` `DEEPSEEK_REASONING_EFFORT` |
| 人设补充 | `GROUP_ROLE` `PRIVATE_ROLE` `TAROT_ROLE`（从属于 `PERSONA`，冲突时以人设文件为准） |
| 视觉/语音 | `VISION_ENABLED` `VISION_MODEL` `VOICE_ENABLED` `VOICE_DEFAULT_CHARACTER` |
| 群语境/情绪 | `GROUP_CONTEXT_ENABLED` `GROUP_CONTEXT_MINUTES` `GROUP_CONTEXT_LIMIT` `PATIENCE_WINDOW_MINUTES` `MOOD_COOLDOWN_MINUTES` |
| 面板/安全 | `DASHBOARD_AUTH` `DASHBOARD_USER` `DASHBOARD_PASSWORD` `WEBHOOK_TOKEN` |
| 分层记忆 | `MEMORY_LAYERS_ENABLED` `MEMORY_RAW_MESSAGES` `MEMORY_RAW_MAX_CHARS` `MEMORY_SUMMARY_MAX_CHARS` `MEMORY_SUMMARY_FACTS` `MEMORY_SUMMARY_KEEP` `MEMORY_SUMMARY_RETRY_TURNS` |
| 超级大脑 | `DSH_ENABLED` `DSH_TIMEOUT` `DSH_REASONING_EFFORT` `DSH_MAX_CONCURRENCY` `DSH_WORKSPACE_DIR` `DSH_PATCH_PATH` |
| 运维 | `RETENTION_DAYS` `BACKUP_ENABLED` `BACKUP_KEEP` `BACKUP_DIR` |
| AI 用量 | `AI_METRICS_ENABLED` `AI_PRICE_CACHE_HIT` `AI_PRICE_CACHE_MISS` `AI_PRICE_OUTPUT` |
| LLBot WebUI | `LLBOT_WEBUI_URL` `LLBOT_WEBUI_TOKEN` `LLBOT_WEBUI_PUBLIC_URL` `LLBOT_TOKEN_FILE` |

---

## 测试

```bash
cd /home/bosak/Documents/Projects/KirikoBot-LLBot
python3 -m pytest tests/ -q -p no:cacheprovider
```

写稿时 main：**1,049 passed**（以实际输出为准）。覆盖人格完整性、防泄露、渐进式情绪、
好感度事件制、频率限制、搜索接口、引用感知、历史完整性、数据迁移、面板接口、安全加固，
以及「已移除功能确实没了」。

测试**不联网**：真实搜索和模型调用不在单元测试里，避免测试本身不稳定。

---

## 目录结构

```
KirikoBot/
├── assets/icon.png              # 项目图标（README 顶部引用）
├── KirikoBot/                   # 机器人本体（挂载进容器 /app）
│   ├── main.py                  # Flask 入口、路由、服务编排
│   ├── prompt_builder.py        # 人格 PERSONA + 系统提示词装配
│   ├── ai_server.py             # DeepSeek 请求与工具调用
│   ├── ai_tools.py              # 20 个工具实现
│   ├── ai_tools_list.py         # Function Calling 工具定义
│   ├── dsh_client.py            # DeepSeek Harness 桥接（超级大脑）
│   ├── affection_service.py     # 好感度（人一份 + 群/私聊分量）
│   ├── profile_service.py       # 用户画像 + 共同记忆
│   ├── judge_service.py         # 情绪判官
│   ├── feature_gate.py          # 按群/按用户的功能开关
│   ├── rate_limiter.py          # 回复频率限制
│   ├── scheduler.py             # 提醒 + 每日维护
│   ├── database_manager.py      # SQLite 访问与迁移
│   ├── robot_server.py          # 消息解析 + 统一发送出口
│   ├── llbot_client.py          # OneBot HTTP API
│   ├── dashboard_auth.py        # 面板 Basic auth
│   ├── webhook_auth.py          # OneBot HMAC 验签
│   ├── templates/ static/       # 面板前端
│   ├── requirements.txt
│   └── requirements-dev.txt
├── scripts/
│   ├── deploy.sh                # 白名单部署 + 快照 + 健康检查
│   └── rollback.sh              # 按快照回滚代码
├── tests/                       # pytest（48 个文件，1,000+ 条）
├── eval/                        # 离线评测脚本与历史结果
├── docs/                        # 设计/评估/backlog 文档
├── docker-compose.yml
├── DEVELOPMENT_GUIDE.md
└── README.md
```

---

## 安全

面板权限比看起来大：能删群、读全部聊天记录、通过 LLBot 驱动 QQ 号。所以：

- 面板默认开启 Basic auth（`.dashboard_password` 自动生成，或 `.env` 覆盖）
- **不要把 5000 / 3080 直接暴露到公网**；推荐 Tailscale 或 SSH 隧道
- OneBot webhook 有独立 HMAC 验签（`WEBHOOK_TOKEN`），不走面板口令
- 密钥只放 `.env`；部署脚本和 `.gitignore` 都会保护它
- 管理面板和 webhook 的签名实现都有独立单测

---

## License

GPL-3.0。见 [LICENSE](LICENSE)。

## 致谢

- [LLBot](https://github.com/linyuchen/llbot) / [PMHQ](https://github.com/linyuchen/pmhq)：QQ 与 OneBot 接入
- [DeepSeek](https://www.deepseek.com/)：对话、搜索、视觉
- [DeepSeek Harness](https://github.com/deepseek-ai)：超级大脑运行时
- 以及群里每一位愿意跟 Kiriko 说话的人
