# 🤖 KirikoBot — 基于 LLBot + DeepSeek 的 QQ 聊天机器人

一个运行在 **LLBot / OneBot** 框架上的 QQ 机器人，集成 **DeepSeek V4.1 Flash** 大模型，提供智能对话、工具调用、定时任务、版本管理等丰富功能。机器人采用可爱女孩风格（Kiriko），支持颜文字。

---

## ✨ 主要功能

### 💬 AI 对话
- 接入 DeepSeek V4.1 Flash API（模型名 `deepseek-flash`），支持 Thinking 思维链
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
DEEPSEEK_MODEL   = "deepseek-flash"     # DeepSeek V4.1 Flash
DEEPSEEK_REASONING_EFFORT = "low"       # 思考强度 low/high/max
GROUP_ROLE       = "你是聊天小助手Kiriko...（群聊人设）"
PRIVATE_ROLE     = "你是聊天小助手Kiriko...（私聊人设）"
TAROT_ROLE       = "你是牌面解读助手Kiriko..."

# 管理面板内嵌 LLBot（可选，留空则用默认值）
LLBOT_WEBUI_URL        = "http://llbot:3080"        # 后端访问地址（容器内网）
LLBOT_WEBUI_PUBLIC_URL = ""                          # 浏览器访问地址，留空自动推导
```

> 面板的「连接状态 / WebQQ」页面通过 `llbot_config/webui_token.txt` 自动登录 LLBot WebUI，
> 无需再手工填密码。`docker-compose.yml` 已把该目录只读挂载进机器人容器。

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
├── llbot_webui.py         # LLBot WebUI 同源反代（自动注入密码）
├── .env.example           # 环境变量模板
├── VERSION                # 当前版本号
├── requirements.txt       # Python 依赖
├── Dockerfile             # Docker 镜像构建
├── docker-compose.yml     # Docker Compose 配置
├── stickers/              # 机器人表情包素材
├── carside_picture/       # 汽车侧面图素材
├── static/
│   ├── css/app.css        # 面板设计系统（日夜主题）
│   └── js/app.js          # 面板前端逻辑
└── templates/
    └── dashboard.html     # Web 管理面板外壳
```

---

## 🖥 管理面板

打开 `http://localhost:5000`，共 16 个页面，支持日间 / 夜间主题，纯本地资源、无外部 CDN 依赖。

| 分组 | 页面 |
|------|------|
| 概览 | 总览、实时日志 |
| 对话与用户 | 对话记录、群消息、用户画像、好感度、自学习 |
| 内容与工具 | 提醒、塔罗、表情包、功能清单 |
| 系统 | 版本日志、群管理、群设置 |

**表情包自动分类**：新表情包收集时会自动调用视觉模型识别分类（可爱 / 搞笑 / 动物 / 动漫 …）
并记录描述与情绪，无需手动批量分类。分类在后台线程执行，不影响回复速度。

**删除群聊**：「群管理」页每个群都有删除按钮，会**永久清除该群全部数据**
（群消息、对话记录、用户画像、好感度、工具调用、提醒、功能需求、学习笔记、功能开关），
弹窗会先列出各项条数并要求输入群号确认；勾选「同时让机器人退出该 QQ 群」还会调用
OneBot `set_group_leave` 让机器人退群（退群后需重新邀请）。
表情包图库是全局共享的，不会被删除。
| LLBot 连接 | 连接状态、WebQQ |

**LLBot 整合方式**：面板不重造轮子 —— LLBot 自己的 React WebUI 功能很全，
所以这里做的是「原生面板 + 原版兜底」：

- **连接状态**：原生重做。登录状态、好友/群数量、消息收发、内存与 CPU、
  设备版本、快速登录账号列表、**LLBot 实时日志**（SSE 转发）。
- **WebQQ**：`iframe` 直接内嵌 LLBot 原版 WebUI，收发消息、群成员、通知等全部功能保持原样。

认证是自动的：LLBot WebUI 的每个 `/api/*` 都要求请求头
`x-webui-token: sha256(密码)`，密码明文存放在 `llbot_config/webui_token.txt`。
后端的 `/llbot-api/*` 同源反代会自己读取并哈希该密码注入请求，
**密码不会下发到浏览器**，因此不需要在面板里二次登录。

> 若提示「未找到 LLBot WebUI 密码」，确认 `docker-compose.yml` 中
> `my-robot` 服务保留了 `./llbot_config:/app/llbot_config:ro` 挂载。

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
