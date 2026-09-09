# KirikoBot 开发维护规范

> 本规范面向 Claude Code 后续维护此项目时使用。
> 最后更新：2026-06-11

---

## 一、项目架构

```
QQRobot/
├── main.py              # Flask 主入口，路由注册，服务初始化
├── robot_server.py       # 消息解析封装
├── llbot_client.py       # LLBot/OneBot HTTP API 客户端 + MessageBuilder
├── ai_server.py          # DeepSeek AI 请求封装
├── ai_tools.py           # 工具实现类（Tarot, Weather, MusicTool 等）
├── ai_tools_list.py      # 工具函数定义（给 AI 的 function calling schema）
├── config.py             # 环境变量配置
├── database_manager.py   # SQLite 数据库管理
├── feature_gate.py       # 每群/每用户功能开关（FEATURE_DEFS 注册表 + FeatureGate）
├── scheduler.py          # 定时任务（早安/提醒）
├── version_manager.py    # 版本号管理 + 变更日志 + 群聊通知
├── music_service.py      # 音乐搜索服务（网易云 API）
├── weather_service.py    # 天气服务
├── balance_service.py    # DeepSeek 余额查询
├── profile_service.py    # 用户画像分析
├── learning_service.py   # 自学习模块
├── news_crawler.py       # 游戏新闻抓取
├── political_news.py     # 时政新闻
├── hot_news.py           # 热搜
├── web_search.py         # 联网搜索
├── log_stream.py         # SSE 日志推送
├── sticker_collector.py  # 表情包收集
├── extra_services.py     # 第三方服务（一言、B站）
├── msg_package.py        # 消息组装
├── templates/dashboard.html  # 前端管理面板（单文件）
├── VERSION               # 当前版本号
└── robot.db              # SQLite 数据库
```

### Docker 架构

| 容器 | 镜像 | 用途 |
|------|------|------|
| `kirikorobot_claudecode-pmhq-1` | PMHQ | QQ 协议层（登录/收发消息） |
| `kirikorobot_claudecode-llbot-1` | LLBot | OneBot HTTP API + WebUI（端口 3080） |
| `kiriko_robot` | 自建 | Flask 机器人核心（端口 5000） |

**关键通信链路**：
```
QQ 群消息 → PMHQ → LLBot → webhook → Flask(:5000) → DeepSeek API
                                                    → LLBot API(:3000) → PMHQ → QQ 群
```

LLBot 的 OneBot API 在 Docker 内网监听 `llbot:3000`，**不对外暴露**。Flask 必须在 Docker 内才能通过此地址通信。

---

## 二、核心开发规范

### 2.1 新增工具（Function Calling）

新增一个机器人功能需要修改 **4 个文件**：

| 步骤 | 文件 | 操作 |
|------|------|------|
| 1 | `ai_tools_list.py` | 添加 `function_xxx` 定义 + `tool_xxx` 对象 + 加入 return 列表 |
| 2 | `ai_tools.py` | 创建 `XxxTool` 类，实现 `xxx_call(robot, ai)` 方法 |
| 3 | `main.py` | 导入类 → 初始化实例 → 注册到 `ROUTES` |
| 4 | `main.py` | 决定加入 `SELF_CONTAINED_TOOLS` 还是 `FOLLOW_UP_TOOLS` |

**工具分类规则**：
- `SELF_CONTAINED_TOOLS`：工具自己完成回复（发送消息/图片/语音），不需要 AI 二次回复。如：`tarot`, `sticker`, `music_search`, `web_search`
- `FOLLOW_UP_TOOLS`：工具只返回数据，需要 AI 根据结果生成回复。如：`weather`, `dice`, `set_reminder`

```python
# ai_tools.py 中的标准模式
class XxxTool:
    def __init__(self, service, msg_package):
        self.service = service
        self.msg_package = msg_package

    def xxx_call(self, robot, ai):
        tool_calls = ai.ai_message.get("tool_calls")
        if not tool_calls:
            return
        args = json.loads(tool_calls[0]["function"].get("arguments", "{}"))
        # ... 业务逻辑 ...
        _set_tool_meta(ai, tool_calls)
        ai.user_text = "结果摘要"
```

### 2.2 发送消息到 QQ

使用 `MessageBuilder` 构建消息，通过 `robot.llbot` 发送：

```python
from llbot_client import MessageBuilder

# 文本消息
builder = MessageBuilder()
builder.text("你好")
if robot.msg_type == "group":
    robot.llbot.send_group_msg(robot.group_id, builder.build())
else:
    robot.llbot.send_private_msg(robot.user_id, builder.build())

# @某人
builder = MessageBuilder()
builder.at(user_qq).text(" 消息内容")

# 图片
builder = MessageBuilder()
builder.image("/path/to/image.png")

# 音乐分享卡片（OneBot music 类型）
builder = MessageBuilder()
builder.music("163", song_id)  # "163"=网易云, "qq"=QQ音乐

# 语音消息（OneBot record 类型）
builder = MessageBuilder()
builder.record("/path/to/audio.mp3")

# 回复消息（引用 + @）
robot.reply("回复内容")  # 便捷方法，自动处理群聊/私聊
```

### 2.3 数据库操作

```python
# 查询
db.fetch_data("SELECT * FROM table WHERE id = ?", (id,))
# 写入
db.deposit("table_name", "(col1, col2)", "(?, ?)", (val1, val2))
# 更新
db.execute_action("UPDATE table SET col = ? WHERE id = ?", (val, id))
# 删除
db.execute_action("DELETE FROM table WHERE id = ?", (id,))
```

新增表需要在 `database_manager.py` 中：
1. `VALID_TABLES` 集合中添加表名
2. `_create_table()` 方法中添加 `CREATE TABLE IF NOT EXISTS`

---

## 三、版本号与变更日志管理

### 3.1 版本号格式

采用语义化版本 `X.Y.Z`：
- **Major（X）**：重大架构变更 / 不兼容改动
- **Minor（Y）**：新功能上线
- **Patch（Z）**：Bug 修复 / 小改进

版本号存储在 `VERSION` 文件和 `app_versions` 数据库表中。

### 3.2 发布新版本流程

1. **前端操作**：管理面板 → 「📦 版本日志」→ 点击 `patch++` / `minor++` / `major++` 自动生成版本号
2. 填写版本说明 → 勾选「群聊通知」→ 点击「创建版本」
3. 系统自动：
   - 写入数据库 `app_versions` 表
   - 更新 `VERSION` 文件
   - 向所有活跃 QQ 群聊发送版本更新通知
4. 为该版本添加变更日志条目（点击 `➕日志`）

### 3.3 变更日志条目类型

| 类型 | 标识 | 用途 |
|------|------|------|
| `feature` | 🎉 新功能 | 新增功能 |
| `fix` | 🔧 修复 | Bug 修复 |
| `improve` | 💡 改进 | 性能/体验优化 |
| `breaking` | ⚠️ 重大变更 | 不兼容的 API 变更 |

### 3.4 功能需求完成时的联动

当功能需求被标记为 `done` 时，系统自动：
1. 写入一条 `feature` 类型变更日志到当前版本
2. 向所有活跃 QQ 群发送通知

**注意**：只在新标记为 done 时触发，重复标记已完成的不会产生重复日志。

---

## 四、群聊推送通知系统

### 4.1 自动推送触发时机

| 触发事件 | 推送内容 | 推送范围 |
|----------|----------|----------|
| 创建新版本 | 版本发布通知（含变更日志摘要） | 所有活跃群 |
| 功能需求标记完成 | 单条功能上线通知 | 所有活跃群 |
| 手动添加变更日志 | 单条变更通知 | 所有活跃群 |

### 4.2 手动推送

前端「📦 版本日志」页面中：
- 每个版本行有「📢推送」按钮 → 重推整个版本更新
- 每条变更日志有「📢」按钮 → 单独推送该条变更

后端 API：
- `POST /api/versions/<id>/push` — 推送版本更新
- `POST /api/changelog/<id>/push` — 推送单条变更日志

### 4.3 推送消息格式规范

推送消息应该**简洁、友好**，不要包含原始用户 ID 或数据库字段。格式参考：

```
🎉 新功能上线：点歌功能

群友建议：可以添加点歌功能吗

📦 版本：v1.0.0
感谢大家对 KirikoBot 的支持！✨
```

消息构建逻辑在 `version_manager.py` 的 `_build_changelog_message()` 方法中。

### 4.4 推送失败排查

推送失败通常是以下原因：
1. **QQ 未登录**：检查 LLBot 日志是否有「请使用手机QQ扫描二维码登录」
2. **ONEBOT_API 配置错误**：必须是 `http://llbot:3000`（Docker 内网地址）
3. **Docker 环境变量过期**：修改 `.env` 后必须重建容器（`up -d --force-recreate`），不能只用 `restart`
4. **没有活跃群**：`_get_active_group_ids()` 查询 `group_messages` 表，需要群里有消息记录

---

## 五、Docker 操作规范

### 5.1 日常操作

```bash
# 启动全部服务
docker compose -f /home/bosak/Documents/ClaudeCode_Projects/KirikoRobot_ClaudeCode/docker-compose.yml up -d

# 查看状态
docker ps --format "table {{.Names}}\t{{.Status}}"

# 查看日志
docker logs kiriko_robot --tail 50
docker logs kirikorobot_claudecode-llbot-1 --tail 50

# 重启单个服务
docker restart kiriko_robot
```

### 5.2 修改 .env 后

**必须重建容器，不能只 restart**：

```bash
# ❌ 错误 — 环境变量不会更新
docker restart kiriko_robot

# ✅ 正确 — 重建容器以加载新的 env_file
docker compose -f /home/bosak/Documents/ClaudeCode_Projects/KirikoRobot_ClaudeCode/docker-compose.yml up -d --force-recreate my-robot
```

原因：`env_file` 在容器创建时固化到 Docker 环境变量，`os.environ` 优先于 `python-dotenv` 读取的文件值。

### 5.3 容器全部崩溃后

```bash
docker compose -f /home/bosak/Documents/ClaudeCode_Projects/KirikoRobot_ClaudeCode/docker-compose.yml up -d --force-recreate
```

重建后检查 QQ 是否在线（可能需要重新扫码登录）。

### 5.4 测试 API 时注意代理

主机的 `http_proxy=127.0.0.1:7890` 会导致 `curl http://llbot:3000` 走代理返回 502。
- 在 Docker **内部**测试：`docker exec kiriko_robot curl http://llbot:3000/...`
- 从主机测试 Flask API：`curl http://localhost:5000/...`（Flask 端口已暴露）

---

## 六、前端管理面板规范

### 6.1 技术栈

单文件 `templates/dashboard.html`，纯 HTML + CSS + Vanilla JS，无框架依赖。

### 6.2 新增页面

1. 侧边栏 `<nav>` 中添加 `<a data-page="xxx">`
2. `loadPage()` 函数中添加 `case 'xxx'` 分支
3. 实现 `xxxHTML()` 异步函数返回页面 HTML
4. 对应的交互逻辑单独写 JS 函数

### 6.3 CSS 变量

```css
--bg, --sidebar, --card, --border, --text, --muted
--accent (橙色), --blue, --green, --yellow, --red, --purple
```

### 6.4 JS 工具函数

```javascript
toast(msg, 'ok'|'err')  // 弹出提示
$('id')                  // document.getElementById
$$('selector')           // querySelectorAll
```

---

## 七、新增功能自检清单

每次开发新功能后，按以下清单自检：

- [ ] `ai_tools_list.py`：工具定义添加且加入 return 列表
- [ ] `ai_tools.py`：工具类实现，正确处理 group/private 消息
- [ ] `main.py`：导入、初始化、注册 ROUTES、加入 SELF_CONTAINED/FOLLOW_UP
- [ ] 代码通过 `python3 -c "import py_compile; py_compile.compile('file.py', doraise=True)"`
- [ ] 如果新增 Python 文件，确认 Dockerfile 无需修改（COPY . . 已包含）
- [ ] 如果新增数据库表，在 `database_manager.py` 的 `VALID_TABLES` 和 `_create_table()` 中添加
- [ ] 功能需求标记 `done` 后验证自动推送
- [ ] 前端手动推送按钮验证
- [ ] 重建容器后验证功能正常

---

## 八、常见问题速查

| 症状 | 原因 | 解决 |
|------|------|------|
| 推送日志显示 sent 但群聊收不到 | ONEBOT_API IP 过期 | 改用 `http://llbot:3000` 并重建容器 |
| LLBot WebUI(3080) 502 | 容器挂了 | `docker compose up -d` |
| 容器 exit code 137 | OOM/SIGKILL | 检查内存，重启容器 |
| QQ 消息收发失效 | QQ 会话过期需重新登录 | 打开 WebUI(3080) 扫码 |
| Flask 500 错误 | 数据库表缺失或代码 bug | `docker logs kiriko_robot` 查看堆栈 |
| curl 访问 llbot:3000 返回 502 | 主机代理拦截 | 在 Docker 内测试，或用 localhost:5000 API |

---

## 九、文件路径速查

```
项目根目录: /home/bosak/Documents/ClaudeCode_Projects/KirikoRobot_ClaudeCode
Docker Compose: /home/bosak/Documents/ClaudeCode_Projects/KirikoRobot_ClaudeCode/docker-compose.yml
LLBot 配置: /home/bosak/Documents/ClaudeCode_Projects/KirikoRobot_ClaudeCode/llbot_config/
LLBot config: /home/bosak/Documents/ClaudeCode_Projects/KirikoRobot_ClaudeCode/llbot_config/config_193392307.json
WebUI 密码: /home/bosak/Documents/ClaudeCode_Projects/KirikoRobot_ClaudeCode/llbot_config/webui_token.txt
环境变量:   /home/bosak/Documents/ClaudeCode_Projects/KirikoRobot_ClaudeCode/QQRobot/.env
数据库:     /home/bosak/Documents/ClaudeCode_Projects/KirikoRobot_ClaudeCode/QQRobot/robot.db
版本文件:   /home/bosak/Documents/ClaudeCode_Projects/KirikoRobot_ClaudeCode/QQRobot/VERSION
开发规范:   /home/bosak/Documents/ClaudeCode_Projects/KirikoRobot_ClaudeCode/DEVELOPMENT_GUIDE.md
```

---

## 十、开发完成后的推送流程

### 10.1 推送前自检

- [ ] 所有新增/修改的 Python 文件通过编译检查：
  ```bash
  python3 -c "import py_compile; py_compile.compile('file.py', doraise=True)"
  ```
- [ ] 机器人 Docker 容器重建后功能正常：
  ```bash
  docker compose -f /home/bosak/Documents/ClaudeCode_Projects/KirikoRobot_ClaudeCode/docker-compose.yml up -d --force-recreate my-robot
  ```
- [ ] 新功能在群聊和私聊中均测试通过
- [ ] 没有引入新的 ERROR 级别日志（检查 `docker logs kiriko_robot --tail 50`）
- [ ] 管理面板（`http://localhost:5000`）各页面加载正常

### 10.2 推送到 GitHub

```bash
# 确保在项目根目录
cd /home/bosak/Documents/ClaudeCode_Projects/KirikoBot

# 查看变更
git status
git diff --stat

# 暂存所有变更
git add -A

# 提交（使用规范的提交信息）
git commit -m "feat: <简短描述>"

# 推送到远程仓库
git push origin main
```

### 10.3 提交信息规范

| 前缀 | 用途 |
|------|------|
| `feat:` | 新功能 |
| `fix:` | Bug 修复 |
| `improve:` | 改进/优化 |
| `docs:` | 文档更新 |
| `refactor:` | 代码重构 |
| `chore:` | 杂项（依赖更新等） |

示例：
```
feat: 添加贴纸理解功能和自动分类

- 修复贴纸收集 STICKER_ONLY 过滤器导致所有图片被跳过
- 修复 AtMemberTool 自我 @ 和私聊消息错误
- 添加贴纸内容理解功能（用户 @ 机器人后发贴纸）
- 添加贴纸自动分类和批量整理功能
- 新增 stickers 数据库表和 API 端点
- 更新仪表板支持分类过滤和批量整理
```

### 10.5 图像识别配置

图像识别使用 DeepSeek 官方视觉模型（`deepseek-v4-flash-vision-exp`），与聊天模型共用同一 API 地址和密钥（`DEEPSEEK_API` / `DEEPSEEK_TOKEN`），无需额外申请。

如需更换模型或关闭图像识别，可在 `.env` 中调整：
```ini
# 关闭图像识别（回退为上下文推断）
VISION_ENABLED="0"
# 更换视觉模型（默认 deepseek-v4-flash-vision-exp）
VISION_MODEL="deepseek-v4-flash-vision-exp"
```

**工作流程**：
```
用户发图片 → DeepSeek 视觉模型理解图片并直接生成回复（单次调用）
         → 后台异步调用视觉模型分类贴纸
```

**关闭图像识别时**：贴纸理解回退为上下文推断（基于用户之前说的话），贴纸分类需手动通过管理面板标记。

### 10.6 群功能开关（每群/每用户独立配置）

每个功能一个独立开关，默认全部开启。群聊按**群**配置，私聊按**用户**配置。管理面板 →「⚙️ 群设置」页面操作，修改即时生效（无需重启）。

**存储**：`feature_settings` 表（`scope_type`='group'/'user' + `scope_id` + `settings_json`），json 只存关闭项；缺行/缺 key = 开启；json 为空自动删行。

**门控模块**：`feature_gate.py`
- `FEATURE_DEFS`：功能注册表（key/label/category/desc），UI 与门控共用，顺序即 UI 顺序
- `TOOL_FEATURE`：工具名 → 功能 key 映射（工具类功能的门控入口）
- `FeatureGate`：`scope_of(robot)` / `disabled_keys()` / `is_enabled()` / `set_enabled()` / `reset()`

**新增可开关功能时**需要：
1. `FEATURE_DEFS` 加一条（含中文 label、分类、描述）
2. 若走 AI 工具：`TOOL_FEATURE` 加 `工具名: key`（`_filter_tools` 会自动剔除）
3. 若有硬编码路径（非工具触发）：在对应入口加 `feature_gate.is_enabled(...)` 守卫
4. 定时推送类（如早间新闻）：在 scheduler 对应方法里按群过滤

**注意**：dashboard 管理端 API 不受群开关影响；版本/变更日志推送是管理员广播，不过滤。

- [ ] 在 GitHub 仓库页面确认提交已到达
- [ ] 检查 CI/CD（如有）是否通过
- [ ] 如需在生产服务器部署，执行 `git pull` + 重建容器
- [ ] **部署并测试完成后，推送新功能速递到所有群聊**：
  ```bash
  curl -X POST http://localhost:5000/api/digest/push
  ```
  此端点会汇总当前版本的所有新功能（feature 类型变更日志），生成格式化的速递消息并发送到所有活跃 QQ 群。
