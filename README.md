# 🤖 KirikoBot — 基于 LLBot + DeepSeek 的 QQ 聊天机器人

一个运行在 **LLBot / OneBot** 框架上的 QQ 机器人，集成 **DeepSeek V4** 大模型，提供智能对话、工具调用、定时任务、版本管理等丰富功能。机器人采用可爱女孩风格（Kiriko），支持颜文字。

---

## ✨ 主要功能

### 💬 AI 对话
- 接入 DeepSeek V4 API，支持 Thinking 思维链
- 群聊 @机器人 或私聊触发，16 轮对话记忆
- 用户画像分析 + 自学习反馈系统

### 🛠 内置工具（19个）
| 工具 | 说明 |
|------|------|
| 🃏 塔罗牌 | 随机抽牌 + AI 解读 |
| 🎵 点歌 | 网易云搜歌 → QQ 音乐卡片播放 |
| 🔍 联网搜索 | 实时搜索 + AI 总结 |
| 🌤 天气查询 | 全国城市天气 + 预报 |
| 🎲 掷骰子 | D6/D20/D100 |
| 🍜 吃什么 | 随机美食推荐 |
| ⏰ 提醒 | 秒/分/时/天 精确提醒，支持每日重复 |
| 📰 时政新闻 | BBC/VOA 翻译播报 |
| 🎮 游戏新闻 | 热点游戏资讯 |
| 📺 B站热搜 | Bilibili 热门排行 |
| 💬 一言 | 随机 Hitokoto 语录 |
| 💰 余额查询 | DeepSeek API 余额 |
| 😊 表情包 | 随机 Kiriko 表情 |
| 👥 @群友 | AI 自主 @群成员 |
| 📋 功能建议 | 群友提需求 → 自动收录 |

### 📦 版本管理 + 群聊推送
- 语义化版本号 `X.Y.Z`，一键自增
- 变更日志分类：🎉新功能 / 🔧修复 / 💡改进 / ⚠️重大变更
- 新版本/新功能**自动推送到所有 QQ 群**
- 前端管理面板支持手动推送 + 防重复

### 🌅 定时任务
- 早安问候（7:00）— 时政新闻 + 游戏资讯 + 每日一言
- 精确到秒的提醒触发器

### 🖥 Web 管理面板
- 实时日志流（SSE）
- 功能需求清单（收集 → 开发 → 完成 → 自动推送）
- 版本历史 + 变更日志浏览
- 提醒管理 / 塔罗记录 / 对话历史 / 用户画像 / 自学习日志
- 群组管理 / 表情包画廊

---

## 📋 前置要求

- 一台可运行 Docker 的机器（Linux / Windows WSL2 / macOS）
- 一个可以登录的 QQ 账号
- [DeepSeek API Key](https://platform.deepseek.com/api_keys)

---

## 🚀 快速部署

### 1. 克隆项目

```bash
git clone https://github.com/1315318/KirikoBot.git
cd KirikoBot
```

### 2. 配置 `.env`

```ini
ROBOT_QQ         = "你的机器人QQ号"
ONEBOT_API       = "http://llbot:3000"
ONEBOT_TOKEN     = "llbot_kiriko_token"
DEEPSEEK_API     = "https://api.deepseek.com/chat/completions"
DEEPSEEK_TOKEN   = "你的DeepSeek API Key"
GROUP_ROLE       = "你是聊天小助手Kiriko...（群聊人设）"
PRIVATE_ROLE     = "你是聊天小助手Kiriko...（私聊人设）"
TAROT_ROLE       = "你是牌面解读助手Kiriko..."
```

### 3. 安装 LLBot Docker 框架

```bash
curl -fsSL https://gh-proxy.com/https://raw.githubusercontent.com/LLOneBot/LuckyLilliaBot/refs/heads/main/script/install-llbot-docker.sh -o llbot-docker.sh && \
chmod u+x ./llbot-docker.sh && ./llbot-docker.sh
```

### 4. 合并文件并启动

```bash
cp -r ./* ./llbot-docker/
cd llbot-docker
docker compose up -d
```

### 5. 扫码登录

打开 `http://localhost:3080`，用手机 QQ 扫描二维码登录。

---

## 🏗 项目结构

```
KirikoBot/
├── main.py                # Flask 入口，路由注册，服务编排
├── ai_server.py           # DeepSeek AI 请求
├── ai_tools.py            # 工具实现（Tarot, Weather, Music, Dice...）
├── ai_tools_list.py       # AI Function Calling 工具定义
├── llbot_client.py        # OneBot HTTP API + MessageBuilder
├── robot_server.py        # 消息解析
├── msg_package.py         # 消息封装
├── config.py              # 环境变量配置
├── database_manager.py    # SQLite 数据库管理
├── scheduler.py           # 定时任务（早安/提醒）
├── version_manager.py     # 版本号 + 变更日志 + 群聊推送
├── music_service.py       # 网易云音乐搜索
├── weather_service.py     # 天气查询服务
├── balance_service.py     # DeepSeek API 余额查询
├── profile_service.py     # 用户画像分析
├── learning_service.py    # 自学习反馈模块
├── log_stream.py          # SSE 实时日志流
├── extra_services.py      # 额外服务（骰子、B站热搜等）
├── hot_news.py            # 热点新闻抓取
├── news_crawler.py        # 新闻爬虫
├── political_news.py      # 时政新闻（BBC/VOA）
├── sticker_collector.py   # 表情包收集
├── web_search.py          # 联网搜索
├── .env.example           # 环境变量模板
├── VERSION                # 当前版本号
├── requirements.txt       # Python 依赖
├── Dockerfile             # Docker 镜像构建
├── docker-compose.yml     # Docker Compose 配置
├── stickers/              # 机器人表情包素材
├── carside_picture/       # 汽车侧面图素材
└── templates/
    └── dashboard.html     # Web 管理面板
```

---

## 🔧 开发指南

新增工具需修改 4 个文件：`ai_tools_list.py` → `ai_tools.py` → `main.py`（导入+注册路由+分类）。

### 修改 .env 后

```bash
# ⚠️ 必须重建容器，restart 不会更新环境变量
docker compose up -d --force-recreate my-robot
```

### 常见问题

| 症状 | 原因 | 解决 |
|------|------|------|
| 推送显示成功但群聊收不到 | ONEBOT_API 使用了过期 IP | 用 `llbot:3000` 并重建容器 |
| 容器 exit 137 | OOM/SIGKILL | 重启容器 |
| QQ 消息收发失效 | 登录会话过期 | 打开 WebUI(:3080) 扫码 |
| curl llbot:3000 返回 502 | 主机代理拦截 | 在 Docker 内部测试 |

---

## 📄 许可证

[GPL V3](LICENSE) © 2026 Bosak

---

## 🙏 致谢

- [LLOneBot / LuckyLilliaBot](https://github.com/LLOneBot/LuckyLilliaBot) – QQ 机器人 Docker 框架
- [DeepSeek](https://www.deepseek.com/) – 大语言模型 API
