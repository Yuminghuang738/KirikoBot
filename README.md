# 🤖 KirikoBot

基于 **LLBot（OneBot）+ DeepSeek** 的 QQ 聊天机器人。一个 20 岁的可爱女生 Kiriko
在群里陪你聊天，会傲娇、会发表情包、记得你是谁、也会因为太久不来而跟你疏远。

20 个工具 · 550 条测试

---

## ✨ 主要功能

### 💬 AI 对话与人格

- 接入 DeepSeek（模型名 `deepseek-flash`），支持 Thinking 思维链
- 群聊 @机器人 或私聊触发，16 轮对话记忆
- **完整人格**：20 岁大三女生，玩摇滚、打游戏、追番、养猫狗；说话短、不反问、不 AI 腔
- **渐进式情绪**：被反复骚扰时从「正常」慢慢变成「懒得搭理」，但不会真的生气，更不会突然不回复
- **防提示词泄露**：被追问设定、要求复述/翻译/代码块包裹时，一律挡回去

### 🛠 内置工具（20 个）

| 分类 | 工具 | 说明 |
|---|---|---|
| 信息查询 | 🔍 联网搜索 | 走 DeepSeek 官方 Search API，一次拿到正文 |
| | 📰 时政新闻 | BBC / VOA 翻译播报 |
| | 🎮 游戏新闻 | 热点游戏资讯 |
| 生活娱乐 | 🃏 塔罗牌 | 每日一抽 + AI 解读，有历史记录 |
| | 🎵 点歌 | 网易云搜歌 → QQ 音乐卡片 |
| 视觉 | 😊 表情包 | 从 2000+ 张库里按情绪挑一张 |
| 互动系统 | 💕 好感度 | 查自己 / 别人的好感度、排行榜 |
| 群功能 | ⏰ 提醒 | 秒/分/时/天 精确提醒，支持每日重复 |
| | 👥 @群友 | 帮用户 @ 群成员 |
| | 💬 撤回 | 撤回自己刚发的消息 |
| | 📊 发言统计 | 当天条数 / 活跃人数 / 时段分布 |
| | 📖 语境读取 | 当前这句读不懂时，翻一下近期聊天 |
| | 💰 余额查询 | DeepSeek API 余额 |
| | 🎤 语音回复 | 用语音说话而不是打字 |
| | 🔁 执行回放 | 调试用：把上一轮原始记录照录出来 |
| 常驻 | 🕐 当前时间 | 算「30 秒后」这类相对时间 |

> 表里有的行包含多个工具：「塔罗牌」= 抽牌 + 历史，「提醒」= 设 / 查 / 删，
> 「好感度」= 自己查询 + 排行榜，合计 **20 个**。
> 除 `get_current_time` 外，都能在群设置里**按群独立开关**（共 19 个开关）。

### 🎭 好感度与用户画像

**好感度是事件制的，不是积分制** —— 奖励「来过」，不奖励「刷屏」：

| 事件 | 分值 |
|---|---|
| 当天首次出现 | +0.3 |
| 第一次说话 | +1.0 |
| 连续出现 | +0.05 × 天数（封顶 0.5） |
| 深夜出现（0–5 点） | +0.1 |
| 发图/表情包 | +0.1 |
| AI 判官情绪分 | ±0.3，每日净额 ±1.0 |

每日事件分总计上限 **+3.0**。掉分 1.5/周，封底 20 —— 关系**可以变差**。
五档：冷淡 → 普通 → 友好 → 亲密 → 挚友。

**用户画像是两层的**：`stable`（性格关键词、称呼偏好、雷点、口头禅、爱聊/不爱聊）
和 `recent`（最近的情绪、在忙什么、提过的事），外加**共同记忆**（具体发生过的事）。

差异化体现在三处：**称呼分层**（熟人才叫名字）、**记得对方提过的事**、
**关系好才偶尔多说一句 / 才敢傲娇**。

### 🚦 回复频率限制

按群配置，两种模式可独立开关、也可组合：

| 模式 | 含义 |
|---|---|
| **单用户** | N 分钟内对**同一个用户**最多回 Y 条（对所有用户一视同仁） |
| **群聊** | N 分钟内在这个群最多发 Y 条 |

超额**静默丢弃**（不回复「你太频繁了」，那条提示本身也会占额度）。
用滑动窗口，计数器在内存里，重启清零。**私聊不参与**。

### 🖼 图片与表情包

图片不是「另一个模型单独回一句」，而是**先转成文字描述，再走正常聊天链路**：

- 视觉模型只负责**看图**（画的是什么、什么风格、有没有文字）
- 回复由平时那条链路生成 —— 人格、历史、群语境、好感度全都带上

所以发图和打字说话是**同一个声音**，也接得上前面聊的内容。

### 🌅 定时任务

唯一会主动发消息的只剩**用户自己设的提醒**。调度器还会做每日数据维护
（过期清理 + 数据库备份）。

> 早报 / 游戏速递 / 一言 / 发言榜这类**群推送订阅**已经整体移除 ——
> 机器人不再主动往群里推任何东西。

### 🖥 Web 管理面板

实时日志（SSE）· 对话记录 · 用户画像 · 好感度 · 自学习 · 提醒 · 塔罗 ·
表情包库 · 群管理 · 群活跃 · 聊天回看 · AI 用量与成本 ·
**群功能设置 + 频率限制** · LLBot 连接状态

---

## 📋 前置要求

- 一台能跑 Docker 的机器（Linux / Windows WSL2 / macOS），建议 **2 核 4G 起**
- 一个可以登录的 QQ 账号
- [DeepSeek API Key](https://platform.deepseek.com/api_keys)

---

## 🚀 快速部署

### 1. 克隆项目

```bash
git clone git@github.com:Yuminghuang738/KirikoBot-LLBot.git
cd KirikoBot-LLBot
```

### 2. 配置 `KirikoBot/.env`

```ini
ROBOT_QQ        = "你的机器人QQ号"        # 只用于日志/面板显示
ONEBOT_API      = "http://llbot:3000"
ONEBOT_TOKEN    = "llbot_kiriko_token"
DEEPSEEK_API    = "https://api.deepseek.com/chat/completions"
DEEPSEEK_TOKEN  = "你的 DeepSeek API Key"
DEEPSEEK_MODEL  = "deepseek-flash"
DEEPSEEK_REASONING_EFFORT = "low"        # 思考强度 low/high/max

GROUP_ROLE      = ""                     # 可选：群聊人设补充，从属于人格
PRIVATE_ROLE    = ""                     # 可选：私聊人设补充
TAROT_ROLE      = ""                     # 可选：解牌口吻

VISION_ENABLED  = "true"                 # 图像/表情包识别
VOICE_ENABLED   = "true"                 # AI 语音回复
GROUP_CONTEXT_ENABLED = "false"          # 群聊背景是否默认附带
```

`.env.example` 里有完整的变量清单。

### 3. 启动

```bash
docker compose up -d --build
```

三个容器：`pmhq`（QQ 客户端）、`llbot`（OneBot 框架）、`my-robot`（KirikoBot 本体）。

### 4. 扫码登录

浏览器打开 `http://<机器地址>:3080`，用手机 QQ 扫码。

登录成功后 OneBot 的 3000 端口才会起来，机器人开始收消息。

### 5. 打开管理面板

```
http://<机器地址>:5000
```

用户名随意，密码在 `KirikoBot/.dashboard_password`（首次启动自动生成）。

> **部署到公网前请务必看下面的「安全」一节** —— 这个面板能删群、能读全部聊天记录、
> 能驱动 QQ 号，Basic auth 走明文 HTTP 时只和网络本身一样私密。

---

## 🔐 安全

面板的权限比看起来大得多：能**删除一个群和它的全部数据**、能**给所有群推消息**、
能**读所有聊天记录**、还能通过 LLBot 桥**驱动 QQ 号本身**。所以：

- **默认就有 Basic auth**（`KirikoBot/.dashboard_password`，或 `.env` 里设 `DASHBOARD_PASSWORD`）
- **不要把 5000 直接暴露到公网**。Basic auth 走明文 HTTP 时只和网络本身一样私密，
  要么套 HTTPS，要么根本不暴露
- 推荐做法：**Tailscale**（云服务器加入 tailnet，本地用 `100.x.x.x:5000` 访问，
  安全组只开 22），或者 **SSH 隧道**（`ssh -L 5000:localhost:5000`）
- OneBot webhook 有独立的 HMAC 验签，不走面板口令

---

## 🏗 项目结构

```
KirikoBot-LLBot/
├── docker-compose.yml          # 三个容器的编排
├── README.md
├── DEVELOPMENT_GUIDE.md
├── recalibrate_affection.py    # 好感度重算脚本（一次性）
├── bootstrap_affection.py      # ⚠️ 旧模型遗留，已标过时
├── merge_db.py                 # 数据库合并工具
├── llbot_config/               # LLBot 数据（挂载进容器）
├── tests/                      # 558 条测试
└── KirikoBot/                  # ← 机器人本体，整个目录挂进容器的 /app
    ├── main.py                 # Flask 入口、路由、服务编排
    ├── prompt_builder.py       # 人格 + 系统提示词装配
    ├── ai_server.py            # DeepSeek 请求
    ├── ai_tools.py             # 工具实现
    ├── ai_tools_list.py        # Function Calling 工具定义（20 个）
    ├── affection_service.py    # 好感度（事件制）
    ├── profile_service.py      # 用户画像（两层）+ 共同记忆
    ├── rate_limiter.py         # 回复频率限制
    ├── judge_service.py        # AI 判官（情绪 + 学习反馈）
    ├── learning_service.py     # 自学习
    ├── feature_gate.py         # 按群/按用户的功能开关
    ├── scheduler.py            # 定时任务（提醒 + 每日维护）
    ├── web_search.py           # 联网搜索（DeepSeek Search API）
    ├── database_manager.py     # SQLite
    ├── dashboard_auth.py       # 面板 Basic auth
    ├── webhook_auth.py         # OneBot webhook HMAC 验签
    ├── llbot_client.py         # OneBot HTTP API + MessageBuilder
    ├── llbot_webui.py          # LLBot WebUI 同源反代
    ├── robot_server.py         # 消息解析 + 统一发送出口
    ├── config.py               # 环境变量
    ├── *.py                    # 各业务服务（音乐/天气/新闻/塔罗/表情包…）
    ├── static/                 # 面板前端（css/js）
    ├── templates/dashboard.html
    ├── stickers/               # 表情包库（不进镜像）
    ├── carside_picture/        # 牌面素材
    ├── Dockerfile
    ├── .dockerignore           # 排除运行时数据，构建上下文 909MB → 0.7MB
    └── requirements.txt
```

---

## 🧪 测试

```bash
python3 -m pytest tests/ -q
```

覆盖：人格完整性与防泄露、渐进式情绪、好感度事件制、频率限制、搜索接口、
引用感知、历史完整性、数据迁移、面板接口、安全加固、
以及「已移除功能确实没了」（推送 / 版本管理 / 旧工具）。

测试**不联网** —— 真实搜索和模型调用不在单元测试里，那会让测试本身变得不可靠。

---

## 🔧 开发指南

### 改完代码

```bash
docker compose restart my-robot        # 只重启机器人（代码是 bind mount，不用重建）
docker compose up -d --build           # 改了依赖 / Dockerfile 才需要重建
docker compose logs -f my-robot        # 看日志
```

### 改完 `.env`

```bash
docker compose up -d --force-recreate my-robot
```

> ⚠️ `env_file` 只在**容器创建**时读取 —— `restart` **不会**更新环境变量，
> 必须 `--force-recreate`。

### 数据库

`KirikoBot/robot.db`（SQLite，宿主机上，靠 bind mount 进容器）。
备份就是复制这个文件；`KirikoBot/backups/` 里还有程序按天存的。

### 修改人格

人格全部在 `KirikoBot/prompt_builder.py` 的 `PERSONA`。改完记得跑测试 ——
`tests/test_persona_integrity.py` 会检查章节锚点、防泄露断言和长度上限。

> 人格是系统提示词的第一段，也是 DeepSeek 前缀缓存的锚点：改它会让缓存失效一次，
> 之后重新建立。**每条消息都在变的内容（时间、当前说话人的画像和关系）故意放在
> user message 里**，就是为了不废掉这段缓存。
