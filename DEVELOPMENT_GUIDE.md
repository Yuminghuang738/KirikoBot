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
| 4 | `main.py` | 决定工具是否自己完成回复（自回复工具加入 `SELF_CONTAINED_TOOLS`） |

**工具分类规则**：
- 自回复工具：工具自己完成回复（发送消息/图片/语音），不需要 AI 二次回复，加入 `SELF_CONTAINED_TOOLS`。如：`tarot`, `sticker`, `music_search`, `web_search`
- 其余工具：处理器只需设置 `ai.tool_result_text` 返回数据，AI 会自动根据结果生成二次回复。如：`weather`, `dice`, `set_reminder`

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

图像识别使用 DeepSeek V4.1 Flash（`deepseek-flash`）——该模型原生支持多模态，与聊天模型共用同一 API 地址、密钥和模型名（`DEEPSEEK_API` / `DEEPSEEK_TOKEN` / `DEEPSEEK_MODEL`），无需额外申请。

如需更换模型或关闭图像识别，可在 `.env` 中调整：
```ini
# 关闭图像识别（回退为上下文推断）
VISION_ENABLED="0"
# 单独指定视觉模型（默认沿用 DEEPSEEK_MODEL，即 deepseek-flash）
VISION_MODEL="deepseek-flash"
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
2. 若走 AI 工具：`TOOL_FEATURE` 加 `工具名: key`（`_enabled_tools` 会自动剔除）
3. 若有硬编码路径（非工具触发）：在对应入口加 `feature_gate.is_enabled(...)` 守卫
4. 定时推送类（如早间新闻）：在 scheduler 对应方法里按群过滤

**注意**：dashboard 管理端 API 不受群开关影响；版本/变更日志推送是管理员广播，不过滤。

### 10.7 管理面板结构与 LLBot 整合

**前端已拆分为三块**（2026-09 视觉重做）：

| 文件 | 职责 |
|------|------|
| `templates/dashboard.html` | 外壳：侧边栏导航、顶栏、`#mainContent` 容器；通过 `?v={{ asset_v }}` 做缓存失效 |
| `static/css/app.css` | 设计系统：全部 token、日夜主题、组件样式 |
| `static/js/app.js` | 全部页面逻辑：`loadPage(name)` 分发到各 `xxxHTML()` 渲染函数 |

**新增一个页面的步骤**：
1. `dashboard.html` 的 `#sidenav` 加 `<a data-page="xxx">`
2. `app.js` 的 `PAGE_META` 加标题（顶栏面包屑用）
3. 写 `async function xxxHTML()` 返回 HTML 字符串；需要绑事件再写 `bindXxx()` 并在 `loadPage` 的 `switch` 里加分支

**布局原语（设计系统 v2，`static/css/app.css`）**：

| 类 | 用途 |
|----|------|
| `.bento` + `.b-3/.b-4/.b-6/.b-8/.b-12` | 12 栅格自适应布局，窄屏自动塌成单列 |
| `.hero` | 页面主视觉横幅（头像 + 状态 + 操作），带旋转渐变描边 |
| `.tile.c1~c6` | 指标磁贴（图标气泡 + 大数字），颜色由 `cN` 决定 |
| `.panel` / `.card` + `.panel-header`（含 `.hicon`） | 内容卡片 |
| `.item`（`.iava/.imain/.ititle/.isub/.imeta/.iact`） | 富列表行，替代表格行 |
| `.timeline` + `.tl-item` | 时间线（自学习页在用） |
| `.chip` / `.chips` | 胶囊筛选按钮（配 `on` 状态） |
| `.reveal` + `style="--i:N"` | 入场错峰动画，N 是序号 |
| `.pill` | 小状态胶囊 |

**动效层**：极光背景 `.aurora`、侧栏滑块 `#navPill`（`moveNavPill()` 定位）、
指针跟随高光（委托 `pointermove` 写 `--mx/--my`）、悬停抬升、进度条流光、
数字滚动（`animateCounters()`）、页面切换与错峰入场。
全部动效都受 `prefers-reduced-motion` 约束。

**CSS 契约**：`app.js` 里有内联 `style="color:var(--muted)"` 等用法，
所以 **CSS 变量名属于接口**（`--bg/--card/--border/--text/--muted/--accent/--blue/--green/--yellow/--red/--purple/--tint/--mono`），
改名必须同步改 JS。

**改完怎么自查**：Flask 模板默认被进程缓存，已在 `main.py` 打开
`TEMPLATES_AUTO_RELOAD`，改模板不用重启。视觉回归可以用 Playwright 截图：

```bash
mkdir -p /tmp/shot && cd /tmp/shot && npm i playwright && npx playwright install chromium
# 脚本见开发记录：登录 http://localhost:5000 → 逐页 click #sidenav a[data-page] → screenshot
```

#### LLBot WebUI 同源反代

`llbot_webui.py` 提供 `/llbot-api/*`，把请求转发到 `LLBOT_WEBUI_URL`（默认 `http://llbot:3080`）。

- LLBot 每个 `/api/*` 都要请求头 `x-webui-token: sha256(明文密码)`；
  明文密码在 `llbot_config/webui_token.txt`，反代在**服务端**读取并哈希后注入，浏览器拿不到密码。
- `docker-compose.yml` 中 `my-robot` 必须挂载 `./llbot_config:/app/llbot_config:ro`，否则读不到密码。
- `/llbot-api`（无子路径）是桥接健康检查，返回 `{ok, reason, message, data}`。
- SSE 端点（`logs/stream`、`webqq/events`）按流式转发；**注意不要设置 `Connection` 等逐跳响应头**，
  WSGI 会直接抛 `AssertionError` 导致 500。
- 只读原则：面板只做状态展示与日志，不做写操作；需要改 LLBot 配置时走「WebQQ」页内嵌的原版 WebUI。

**排查**：面板提示读不到密码时，先 `docker exec kiriko_robot cat /app/llbot_config/webui_token.txt`，
再 `docker exec kiriko_robot curl -s -o /dev/null -w '%{http_code}' http://llbot:3080/`。
注意 LLBot 有防爆破：**连续密码错误会锁定 WebUI 一小时**（状态在内存里，重启 llbot 容器即可解除）。

### 10.8 表情包自动分类

`StickerCollector.collect()` 保存新表情包后，会把 `_auto_categorize(fname, url)` 丢进
`executor`（12 线程）后台执行：

1. 优先用**已下载的本地文件**做视觉分析（源 URL 常会过期），
   `AiServer.vision_analyze_with_category()` 一次调用返回 `description / emotion / category`；
2. 分类不在 `STICKER_CATEGORIES` 里就归入「其他」，视觉不可用时写「未分类」；
3. 结果写回 `stickers` 表（`category / content_desc / emotion / categorized_at`）。

`ai_server` 与 `sticker_collector` 互相引用（前者要 `STICKER_CATEGORIES`），
所以 `_auto_categorize` 内部**延迟导入** `AiServer`，不要提到模块顶层。

`main.py::_background_sticker_categorize` 是给「没被收集到的图片」补分类的老路径，
现在会先查库，**已分类的直接跳过**，避免同一张图跑两次视觉调用（省一半费用）。

### 10.9 删除群聊与数据清理

接口（`main.py`）：

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/groups/<gid>/purge-preview` | 返回各表将删除的条数，弹窗展示用（只读） |
| DELETE | `/api/groups/<gid>` | 执行清理；body `{"leave": true}` 时额外调用 `set_group_leave` 退群 |

清理逻辑在 `DatabaseManager.purge_group()`：

- **群维度表**（有 `group_id` 列，整表按群删）：`group_messages`、`history`、
  `reminders`、`tool_usage`、`user_profiles`、`user_affection`、
  `user_affection_log`、`feature_requests`；
- **功能开关**：`feature_settings` 里 `scope_type='group'` 该群的行；
- **按用户的表**（schema 里没有 group_id，用户可能同时活跃在多个群，需注意）：
  该群出现过的 user_id 对应的 `learning_log` 与 `scope_type='user'` 的 `feature_settings`；
- 同时清掉 `DatabaseManager._member_cache[gid]`（内存成员缓存）；
- **不动 `stickers`** —— 表情包是全局图库，且按内容去重，删了会影响其他群。

前端在 `app.js` 的 `deleteGroup()` / `confirmDeleteGroup()`：先拉 preview 列出条数，
要求输入完整群号才能提交（防误点），退群是可选勾选项。

**改这里要小心**：`purge_group` 是不可逆的破坏性操作。改动后请用
`/tmp/test_purge.py` 那种做法——**先 `sqlite3` backup 复制一份 robot.db 再测**，
不要直接拿生产库试。

### 10.10 人设与口吻（唯一来源：`prompt_builder.PERSONA`）

**Kiriko 的全部人物设定都在 `prompt_builder.py` 的 `PERSONA` 里**——身份、生活背景、
与群友的关系、说话方式、立场、傲娇、禁止的 AI 腔、以及「别演过头」。改人设只改这一处。

**为什么不放 `.env`**：

1. `.env` 被 gitignore，设定会随部署漂移，仓库里看不到真正的设定；
2. 它和风格约束天然打架——旧文案写的是「你是聊天**小助手**」「**可以使用**颜文字」，
   而约束里是「你不是助手」「颜文字克制使用」，**直接矛盾**，模型收到的是自相矛盾的指令。

`.env` 里的 `GROUP_ROLE` / `PRIVATE_ROLE` / `TAROT_ROLE` 现在**只是可选补充**，
由 `build_role_prompt()` 追加在 `PERSONA` **之后**，并显式声明「冲突时以上面为准」，
所以残留的旧文案再也无法改写人设。它们也**不再是必填项**（`Config.validate` 已放开）。

**改动人设时注意**：

- 最后那段「别演过头」很重要：把傲娇/可爱当固定表演反而更假，这正是要避免的
- 人设变长会直接增加每条消息的 token（现在 PERSONA 约 1144 字），
  加内容前先想想值不值
- **改完 `.env` 必须重建容器**（`docker compose up -d --force-recreate my-robot`）：
  `env_file` 只在创建时读取，`restart` 不会重读。这个坑这次真的踩到了 ——
  改完 `.env` 后容器里跑的还是旧文案，直到重建才生效

**验证人设是否生效**：在容器里 `build_role_prompt(Config.GROUP_ROLE)`，
确认返回以 `PERSONA` 开头、且不含「部署方补充设定」段（说明 `.env` 已清空）。

### 10.11 安全模型（必读）

面板能删数据、往所有群推消息、并经 LLBot 反代操作 QQ 账号，所以**必须当作高权限后台**对待。

| 面 | 保护方式 |
|----|----------|
| 控制台 + 全部 `/api/*` | HTTP Basic（`dashboard_auth.init_app`）；口令来自 `DASHBOARD_PASSWORD`，留空则首次启动生成到 `KirikoBot/.dashboard_password` |
| OneBot webhook | LLBot 的 `x-signature`（HMAC-SHA1 over 原始 body），见 `webhook_auth.py` |
| 日志 / 画像 / 消息等回显 | 服务端 `html.escape` + 前端 `esc()` |

**LLBot 侧必须同步配置**：`llbot_config/config_*.json` 里 `ob11` → `http-post`
那一条的 `token` 要和 `.env` 的 `ONEBOT_TOKEN`（或 `WEBHOOK_TOKEN`）一致。
注意 **LLBot 发的是 `x-signature` 签名头，不是 `Authorization`** ——
`OB11HttpPost.emitEvent` 里写得很清楚，签名是对 `JSON.stringify(event)` 做的。
改完要重启 llbot 容器。

**踩过的坑**：
- 签名必须对**原始字节**校验（`request.get_data(cache=True)`），
  不能拿解析后再序列化的 JSON 去算，否则永远对不上。
- Basic auth 在纯 HTTP 下只等于"网络有多私密就有多安全"。要暴露到公网请套 HTTPS。
- SSE（`/stream`、`/llbot-api/logs/stream`）能正常带 Basic 凭据；
  `EventSource` 用同源凭据即可，不需要额外传 token。

### 10.12 数据保留、备份与画像表

- **保留策略**：`maintenance_service.prune_old_data()` 按 `RETENTION_DAYS`（默认 180）
  清理 `group_messages` / `history` / `user_affection_log`；
  **画像、好感度分值、工具调用统计等聚合数据不受影响**。设为 0 关闭。
- **自动备份**：`maintenance_service.backup_database()` 用 SQLite 在线备份 API
  （不是 `cp`，WAL 下直接复制可能丢已提交页），每天一次，保留 `BACKUP_KEEP` 份，
  默认写在应用目录的 `backups/`（容器内 `/app/backups`，随 compose 挂载持久化）。
- 两者都由 `scheduler._loop` 每天触发一次（`run_daily` 自带日期去重）。

**`user_profiles` 已改为主键 `(user_id, group_id)`**：原先是 `user_id UNIQUE` +
单个 `group_id`，一个用户只能存一份画像，换群就被覆盖，`get_group_profiles()`
在其他群里会凭空少人。SQLite 不能删 UNIQUE 约束，所以
`DatabaseManager._migrate_user_profiles()` 走的是**建新表 → 拷数据 → 换名**，
且必须在建索引前 `DROP INDEX`（表改名时索引会跟着走，不删的话
后面的 `CREATE INDEX IF NOT EXISTS` 会变成空操作）。

### 10.13 测试与 CI

```bash
pip install -r KirikoBot/requirements-dev.txt
python -m pytest tests/ -q          # 38 个用例，秒级
```

`tests/conftest.py` 把 `KirikoBot/` 加进 `sys.path` 并预置 `Config.validate()` 需要的环境变量。
**测试里不要 `import main`** —— 导入即启动调度器、LLBot 客户端和线程池。
需要读 `main.py` 的常量时用 `ast` 解析（`tests/test_prompt_and_frontend.py` 有例子）。

CI 在 `.github/workflows/ci.yml`：跑 pytest + `compileall`。
`.gitignore` **不再屏蔽** `tests/`，且 `docker-compose.yml` 已纳入版本管理
（否则 clone 下来跑不起来，LLBot 挂载也丢了）。

### 10.14 引用感知（群聊语境）

**要解决的问题**：群友 B 引用了机器人回复给 A 的那句话来说事，机器人看不到引用内容，
于是把 B 的话当成全新话题回答——答非所问。

**实现**：`IncomingMessage._extract_reply()` 解析 `reply` 段，
`prompt_builder.describe_reply()` 生成说明，`main._reply_note()` 把它拼到**用户消息前面**
（不是 system prompt —— 挨着原话放，模型权重更高）。

**两个关键事实（不做功课就会踩）**：

1. **LLBot 的 `reply` 段自带被引用消息的完整内容**（`message_seq` / `sender_id` /
   `sender_name` / `segments`），所以解析引用**不需要额外调用 `get_msg`**，零成本。
2. **`message_id` 和 `message_seq` 不是一回事**：
   - 事件里的 `message_id` 是 LLBot 的**短 id**（`createMsgShortId`），`delete_msg` / `get_msg` 用它；
   - `reply` 段引用的是 QQ 的 **`message_seq`**。
   - 所以 `group_messages` **两个字段都存**。想连"谁在回谁"的引用链，必须用
     `reply_to_seq` ↔ `message_seq` 关联，拿 message_id 去比永远对不上。

**"引用的是不是我自己"怎么判断**：`LLBotClient._remember_sent()` 在每次发送成功后
记下 `message_id` 和正文（`_post` 以前把响应体丢了，所以根本不知道哪些消息是自己发的）。
检测优先比 id，**其次比正文，且要求完全相等 + 至少 6 个字**——
早期版本用子串匹配，结果别人引用的"好的"会被误判成机器人的话。

**已知限制**：`_recent_sent` 是**内存环形缓冲（200 条）**，重启即清空、被大量消息挤出后
就认不出旧引用。后果只是退化成"某某说过的话"（仍然正确，只是不点明是机器人自己）。
要彻底解决需要把发出的消息也落库——那是「撤回」功能的前置，届时一并做。

**自测方法**：构造一条带 `reply` 段的签名事件投递到 `/webhook`，然后看 `think` 日志里
模型的思维链有没有出现"quoted my message"之类的表述——这是端到端最直接的证据。

### 10.15 群活跃统计、聊天回看、撤回与语境工具

#### 会话记录存在两张表里

| 表 | 内容 | 谁写 |
|----|------|------|
| `group_messages` | 群友消息 | webhook 收到即写（在判断是否 @bot **之前**，所以是全量） |
| `bot_messages` | **机器人自己发的**消息 | `LLBotClient` 发送成功后回调 `db.record_bot_message` |

机器人自己的消息不在 `group_messages` 里，因为 LLBot 的 http-post 配置是
`reportSelfMessage: false`。所以"完整对话"要靠查询时 **UNION 两张表**
（见 `get_recent_group_context` / `get_group_message_page`）。
这样不用改 LLBot 配置，也就没有"机器人回应自己消息"的回声风险。

#### 排序必须用 `ts_exact`

`timestamp` 只有**秒**精度，而机器人回复常常和用户消息落在同一秒。
一开始直接用 `ORDER BY timestamp`，同秒内两张表的 `id` 互不可比，
结果就是机器人那句话排到了用户提问**前面**。

所以两张表都有 `ts_exact REAL`（写入时取 `time.time()`），排序键是
`IFNULL(ts_exact, (julianday(timestamp) - 2440587.5) * 86400.0)`
——后半段是给加列之前的老数据兜底的。

#### 三个工具

| 工具 | feature key | 说明 |
|------|-------------|------|
| `recall_message` | `recall` | 撤回自己刚发的消息。`get_last_bot_message` 只返回 `RECALL_WINDOW`（110 秒）内的，因为 QQ 的撤回窗口约 2 分钟，超时的 id 拿给 API 也是白跑 |
| `group_stats` | `group_stats` | 单群单日统计，支持 `today` / `yesterday` / `YYYY-MM-DD` |
| `read_context` | `context_read` | 让模型自己决定是否读群聊记录 |

**`read_context` 为什么是工具而不是默认注入**：每条消息都附一段转写会让 token 成本翻倍，
而且大多数消息根本不需要。系统提示里写明了三类该调用的情况（指代不明 / 像在接别人的话 /
提到你没参与过的讨论）。实测模型判断得很准：
`"this is referring to something I don't know about. I should read context."`

#### 面板

- 「群活跃」：`GET /api/groups/<gid>/stats?date=` — 总条数 / 活跃人数 / 图片数 /
  发言排行 / 24 小时分布
- 「聊天回看」：`GET /api/groups/<gid>/messages?date=&q=&user=&page=&size=`
  —— 分页转写，含机器人自己的行；同一页内会把 `reply_to_seq` 解析成被引用的原文
- `GET /api/groups/<gid>/days` 给日期下拉用

**注意**：`get_group_message_page` 的 UNION 子查询里占位符是**按出现顺序**绑定的
（member 参数 → bot 显示名 → bot 参数）。改 SQL 时务必盯住这个顺序，写反了不会报错，
只会查到错的数据。

### 10.16 AI 调用可观测性

每次 DeepSeek 调用都会记一条 `ai_calls`，面板「AI 用量」页展示
调用量 / 成功率 / P50·P95·最慢延迟 / token 消耗 / **成本估算** / 按来源拆分 / 失败列表。

**埋点位置**（`ai_server.py`，四处，都属于 `ai_metrics.record`）：

| 位置 | source 示例 | 说明 |
|------|------------|------|
| `AiServer.ai_request` | `chat` | 主对话 |
| `AiServer.follow_up_request` | `chat`（kind=followup） | 工具回环的第二次请求 |
| `quick_chat` | `judge` / `profile` / `news` / `learning` | 后台任务，由调用方传 `source=` |
| `vision_analyze` | `vision` | 图像理解 |

`AiServer` 有 `source` / `group_id` 两个属性用于归因（默认 `chat` / 空），
复用它跑工具流程的地方（塔罗、@群友、新闻翻译）应显式覆盖 `ai.source`。

**为什么不直接在 `ai_server` 里连数据库**：`ai_metrics` 用可插拔 sink
（`main.py` 里 `ai_metrics.set_sink(db.record_ai_call)`），所以 `ai_server`
不依赖 `database_manager`，脚本里单独用也不会因为没数据库而挂。
**没配 sink 时 `record()` 直接返回**，且所有异常都被吞掉——埋点绝不能影响回复。

**成本怎么算的**：官方按峰谷计价，高峰是 UTC 周一至周五的 `01:00-04:00` 和
`06:00-10:00`，低谷减半。所以记录时就把 `utc_hour` / `utc_weekday` 存下来，
避免查询时再算时区。单价来自 `Config.AI_PRICE_*`（默认对应当前官方价，
调价改 `.env` 即可，不用动代码）。

**几个容易踩的点**：

- `timestamp` 存的是本地时间，**成本判定必须用存下来的 UTC 字段**，不能拿本地时间推。
- 使用量里 `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens` 才是计费口径；
  旧版只有 `prompt_tokens_details.cached_tokens`，`extract_usage` 两种都兼容。
- **失败调用不计入延迟分位数**（否则超时会污染 P95），但计入失败数与错误列表。
- `ai_calls` 增长很快，已纳入保留策略（`RETENTION_DAYS`）一起清理。

- [ ] 在 GitHub 仓库页面确认提交已到达
- [ ] 检查 CI/CD（如有）是否通过
- [ ] 如需在生产服务器部署，执行 `git pull` + 重建容器
- [ ] **部署并测试完成后，推送新功能速递到所有群聊**：
  ```bash
  curl -X POST http://localhost:5000/api/digest/push
  ```
  此端点会汇总当前版本的所有新功能（feature 类型变更日志），生成格式化的速递消息并发送到所有活跃 QQ 群。


### 10.17 话题线程化 / 群推送订阅 / 长期记忆

**话题线程化**（`DatabaseManager.get_group_threads`）：群聊里多条话题并行，
平铺时间线既难读、模型也难推理。聚类规则是两条——**这条消息引用的是不是当前话题里的消息**，
以及**距离上一条是否超过 `max_gap_minutes`（默认 10 分钟）**，两者同时成立才开新话题。
排序同样依赖 `ts_exact`（见 10.15）。

**群推送订阅**（`group_subscriptions` 表 + `scheduler._check_subscriptions`）：

- 每个 (群, 话题) 一行，字段 `push_time` / `enabled` / `last_fired_date`
- 调度器每个 tick 查 `due_subscriptions(now, today)` = 已启用 + 时间已到 + 今天没发过
- **先 mark_fired 再发送**：否则发送失败会在每个 tick 重试，把群刷爆
- **迟到超过 `MAX_LATE_MINUTES`（120）直接跳过并标记**：否则机器人半夜重启，
  第二天早上会把积压的早报全套发出去
- 话题处理函数返回**字符串**（普通文本）或**消息段列表**（发言榜要 @ 人），
  `_push_topic` 两种都要能处理

**长期记忆**（`profile_history` 表）：`save_user_profile` 覆盖前先把旧画像存一份，
`build_context_prompt` 在印象发生变化时补一句"你以前觉得 TA 是 X，现在是 Y"。
只记变化，避免每个用户都背一串历史。

**相似表情**（`SimilarStickerTool`）：复用表情包去重用的 pHash 索引，
下载用户发的图 → 算 pHash → 找汉明距离最小的一张 → 距离 ≤ `PHASH_THRESHOLD` 才发。

**执行回放**（`ExplainSelfTool`）：`tool_usage` 表加了 `arguments` / `result` / `reasoning`
三列（老库自动迁移），在**工具处理函数跑完之后**记录，这样才拿得到结果。
用户问"你刚才干嘛了"时，工具把这些回放给模型，由模型用第一人称说出来。
