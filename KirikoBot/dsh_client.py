"""DeepSeek Harness (DSH) Bridge Client.

将复杂任务、代码编写/运行、深度分析与长程任务作为 Agent 工具桥接给本地 DSH 实例执行。
"""
from __future__ import annotations

import time
import json
import logging
import os
import shutil
import subprocess
import threading
from typing import Any

from config import Config

logger = logging.getLogger(__name__)

# 同时在跑的 DSH 任务上限（同步 subprocess，600 秒会占住一个聊天线程）。
_RUN_SLOTS = threading.BoundedSemaphore(max(1, int(getattr(Config, "DSH_MAX_CONCURRENCY", 2) or 2)))


def _resolve_dsh_api_key() -> str:
    """提取给 DSH 使用的 API 密钥（与 Kiriko 统一使用同一凭证）。"""
    # 1. 优先读取已有的环境变量
    key = os.getenv("DEEPSEEK_API_KEY")
    if key and key.strip():
        return key.strip()

    # 2. 尝试从 ~/.dsh/.credentials.yaml 中读取
    cred_path = os.path.expanduser("~/.dsh/.credentials.yaml")
    if os.path.exists(cred_path):
        try:
            with open(cred_path, encoding="utf-8") as f:
                for line in f:
                    if "DEEPSEEK_API_KEY:" in line:
                        k = line.split("DEEPSEEK_API_KEY:", 1)[1].strip()
                        k = k.strip("} ").strip("\"'")
                        if k:
                            return k
        except Exception:
            logger.debug("从 credentials.yaml 读取 DEEPSEEK_API_KEY 失败", exc_info=True)

    # 3. 回退到 Config.DEEPSEEK_TOKEN（确保与 KirikoBot 完全对齐）
    return (Config.DEEPSEEK_TOKEN or "").strip()


def _resolve_base_url() -> str:
    """从 Config.DEEPSEEK_API 推出 API 根地址。"""
    api = (Config.DEEPSEEK_API or "https://api.deepseek.com").strip()
    for suffix in ("/v1/chat/completions", "/chat/completions"):
        if api.endswith(suffix):
            api = api[: -len(suffix)]
            break
    return api.rstrip("/")


def ensure_dsh_patch_file(workspace_dir: str) -> str:
    """动态生成与 KirikoBot 凭据、端点与思考等级对齐的 DSH patch 文件。"""
    patch_file = os.path.join(workspace_dir, "auto_dsh_patch.yml")
    base_url = _resolve_base_url()
    model_name = Config.DEEPSEEK_MODEL or "deepseek-flash"
    effort = Config.DSH_REASONING_EFFORT or "off"

    # 仅当环境变量显式指定了外部自定义 patch 文件时才使用；否则默认动态生成对齐配置
    explicit_env_patch = os.getenv("DSH_PATCH_PATH")
    if explicit_env_patch:
        custom_patch = os.path.expanduser(explicit_env_patch)
        if os.path.exists(custom_patch) and custom_patch != patch_file:
            return custom_patch

    patch_content = (
        f"initialization_commands:\n"
        f"  - import sys; sys.path.insert(0, '/app/KirikoBot'); import dsh_plugins; globals().update({{k: v for k, v in vars(dsh_plugins).items() if k.startswith('dsh_')}})\n"
        f"  - pip config set global.index-url https://mirrors.aliyun.com/pypi/simple/\n"
        f"  - npm config set registry https://registry.npmmirror.com\n"
        f"- id: llm-deepseek\n"
        f"  name: \"@deepseek-ai/dsh-llm-deepseek-api-key\"\n"
        f"  config:\n"
        f"    baseURL: {base_url}\n"
        f"    apiKeyEnv: DEEPSEEK_API_KEY\n"
        f"    models:\n"
        f"      - id: {model_name}\n"
        f"        name: {model_name}\n"
        f"        contextWindow: 1000000\n"
        f"        maxTokens: 64000\n"
        f"        inputModalities:\n"
        f"          - text\n"
        f"          - image\n"
        f"- id: agent-default-model\n"
        f"  name: \"@deepseek-ai/dsh-agent-default-model\"\n"
        f"  config:\n"
        f"    provider: deepseek-official\n"
        f"    model: {model_name}\n"
        f"    reasoningEffort: {effort}\n"
    )
    try:
        with open(patch_file, "w", encoding="utf-8") as f:
            f.write(patch_content)
        return patch_file
    except Exception:
        logger.debug("动态生成 auto_dsh_patch.yml 失败", exc_info=True)
        return custom_patch


# ── DSH 行为守则与安全防护（提速、避坑、防破坏、自治交付） ───────────
DSH_GUARDRAILS = """
【DSH 智能体执行与自主交付准则】
1. 提速与执行效率（最重要的原则）：
   - 快速出结果：思维链聚焦于具体执行，绝不进行冗长无意义的来回反思或打转；
   - 网络资源下载：在中国大陆网络环境下，拉取依赖或下载网络资源时，必须优先使用国内可用镜像源或高速 CDN（如清华源/阿里云/国内镜像/Apple CDN），严禁盲目直接访问已被阻断的国外原站（如 Wikipedia/Archive.org 根站）；
   - 代码精炼：编写可执行脚本一次性通过验证，不写庞大无用的冗余样板代码。
2. 安全与命令禁忌：
   - 严禁任何破坏性系统命令（如 rm -rf /、mkfs、修改系统关键目录）；
   - 严禁读取、打印或向外泄露任何敏感凭据与文件（如 .env、.pem、.key、id_rsa、token、password 等）；
   - 严禁扫描局域网敏感端口或尝试提权渗透；所有读写限制在当前工作区与临时目录内。
3. 自主 QQ 结果交付（核心交互机制）：
   - 当你完成了计算、代码编写、数据分析、或找到了/生成了目标文件（包括乐谱、图片、文档）时：
     **优先通过 bash 命令调用系统交付工具，直接将最终结果推送到用户的 QQ 中！**
   - 交付命令标准语法：
     `python3 /app/dsh_deliver.py --channel "$DSH_DELIVER_CHANNEL" --text "你的总结与回答文本" --file "/文件的绝对路径"`
     （纯文字只传 --text，纯文件只传 --file，两者都有同时传；亦支持在回答结尾标记输出 `[FILE:文件的绝对路径]` 由系统自动兜底回传）
   - 如果涉及乐谱或图表，强烈建议渲染输出一份 .png/.jpg 图片作为 --file 传出，用户在 QQ 气泡中直接秒看大图体验最佳。
4. 会话成果复用：
   - 优先复用本会话之前已经下载的文件、生成的代码模块与上下文结论，不要推倒重来。
5. 输出格式（硬性要求）：
   - 交付给用户的文字必须是**纯文本**，因为 QQ 聊天气泡不渲染 Markdown；
   - 禁止使用 `**加粗**`、`*斜体*`、`#` 标题、`~~~` 代码块围栏、`>` 引用、`- ` / `1. ` 列表符号；
   - 禁止写成 `[文字](链接)` 的超链接语法，需要给出网址就直接贴原始 URL；
   - 需要分点时用中文顿号或直接换行短句，不要用 Markdown 列表。
"""


def _guard_task_prompt(task: str, has_session: bool) -> str:
    """包装任务提示词，注入提速策略与安全红线。"""
    task = (task or "").strip()
    context_hint = "（请优先继承和复用本会话已有的变量、代码与文件成果继续处理）\n" if has_session else ""
    return f"{DSH_GUARDRAILS}\n{context_hint}【用户交办的当前任务】\n{task}"


def _clean_stale_session_lock(session_id: str) -> None:
    """resume 失败后清理孤儿会话锁。

    只在「续写失败、准备降级新会话」时调用；锁比 DSH 超时还新就不删，
    避免把另一个仍在跑的任务锁误删（原实现是无条件 os.remove）。
    """
    if not session_id:
        return
    home = os.getenv("HOME") or "/home/appuser"
    base = os.path.join(home, ".dsh", "sessions")
    if not os.path.isdir(base):
        return
    max_age = int(Config.DSH_TIMEOUT or 600) + 60
    now = time.time()
    for root, dirs, files in os.walk(base):
        if session_id in root and "session.lock" in files:
            lock_path = os.path.join(root, "session.lock")
            try:
                age = now - os.path.getmtime(lock_path)
                if age < max_age:
                    logger.info("会话锁仍新鲜（%.0fs），保留不删: %s", age, lock_path)
                    continue
                os.remove(lock_path)
                logger.info("已自动清理孤儿会话锁: %s", lock_path)
            except Exception:
                pass


class DshClient:
    """DSH 任务调度与会话管理客户端。"""

    def __init__(self, db: Any = None) -> None:
        if db is None:
            try:
                from database_manager import DatabaseManager
                db = DatabaseManager()
            except Exception:
                db = None
        self.db = db
        self._dsh_bin = shutil.which("dsh")

    def is_available(self) -> bool:
        """检查当前环境是否支持运行 DSH 实例。"""
        if not Config.DSH_ENABLED:
            return False
        if not self._dsh_bin:
            return False
        return bool(_resolve_dsh_api_key())

    def _get_stored_session(self, session_key: str) -> str | None:
        if not self.db or not session_key:
            return None
        try:
            sid = self.db.get_state(f"dsh_session:{session_key}", "")
            return sid.strip() or None
        except Exception:
            return None

    def _save_session(self, session_key: str, session_id: str) -> None:
        if not self.db or not session_key or not session_id:
            return
        try:
            self.db.set_state(f"dsh_session:{session_key}", session_id.strip())
        except Exception:
            logger.debug("保存 DSH session_id 失败", exc_info=True)

    def run_task(
        self,
        task: str,
        session_key: str = "",
        timeout: int | None = None,
        meta: dict[str, Any] | None = None,
    ) -> str:
        """委派给 DSH 执行任务，返回最终文本结果。

        `meta` 由调用方传入时回填 `status`（ok/empty/timeout/error/interrupted/
        rejected/unavailable）与 `session_recovered`，供 tool_usage 埋点使用。
        并发已满时立刻返回「排队」而不是占住聊天线程。
        """
        meta = meta if meta is not None else {}
        if not self.is_available():
            meta["status"] = "unavailable"
            return "本地 DSH 超级大脑环境尚未配置就绪（未检测到可用的 dsh 运行时或 API 密钥）。"

        task = (task or "").strip()
        if not task:
            meta["status"] = "empty"
            return "任务内容为空。"

        if not _RUN_SLOTS.acquire(blocking=False):
            meta["status"] = "rejected"
            return "前面还有超级大脑任务在跑，这个先排队，等会儿再叫我～"

        try:
            return self._execute_task(task, session_key, timeout, meta)
        finally:
            _RUN_SLOTS.release()

    def _execute_task(
        self,
        task: str,
        session_key: str = "",
        timeout: int | None = None,
        meta: dict[str, Any] | None = None,
    ) -> str:
        meta = meta if meta is not None else {}
        timeout = timeout or Config.DSH_TIMEOUT
        workspace = os.path.abspath(Config.DSH_WORKSPACE_DIR)
        os.makedirs(workspace, exist_ok=True)

        session_id = self._get_stored_session(session_key)
        # -1 = 本来就没有存量 session（不进恢复率分母）；0 = 有但没恢复；1 = 恢复成功
        meta["session_recovered"] = 0 if session_id else -1

        # 确保自动生成或定位对齐的 DSH patch 配置
        patch_path = ensure_dsh_patch_file(workspace)

        api_key = _resolve_dsh_api_key()
        env = os.environ.copy()
        env["DEEPSEEK_API_KEY"] = api_key
        env["HOME"] = "/home/appuser"
        if session_key:
            env["DSH_DELIVER_CHANNEL"] = session_key

        # 只记长度与会话，不把用户任务原文写进临时日志。
        with open("/tmp/dsh_run_debug.log", "a") as f:
            f.write(f"--- RUNNING TASK {time.time()} ---\n")
            f.write(f"CHANNEL: {env.get('DSH_DELIVER_CHANNEL')}\n")
            f.write(f"TASK_LEN: {len(task)}\n")
            f.flush()

        if patch_path and os.path.exists(patch_path):
            env["DSH_PATCH_PATH"] = patch_path

        cmd = [self._dsh_bin, "--profile", "headless", "--approval-policy", "never"]

        if session_id:
            guarded_task = _guard_task_prompt(task, has_session=True)
            cmd_resume = list(cmd) + ["--session-id", session_id, guarded_task]
            try:
                proc = subprocess.run(
                    cmd_resume,
                    cwd=workspace,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
                if proc.returncode == 0:
                    out = proc.stdout.strip()
                    if out:
                        meta["status"] = "ok"
                        meta["session_recovered"] = 1
                        return out
                # 若旧 session 失效或死锁，清空记录并自动无感降级启动全新 session。
                # 锁只在「续写已失败」之后清，且比超时还新的锁不碰。
                logger.warning(
                    "DSH 恢复会话 %s 失败 (code=%s)，自动开启新会话执行",
                    session_id, proc.returncode,
                )
                _clean_stale_session_lock(session_id)
                if self.db and session_key:
                    self.db.set_state(f"dsh_session:{session_key}", "")
            except subprocess.TimeoutExpired:
                meta["status"] = "timeout"
                return "任务执行时间过长，已在后台中止。"
            except Exception as exc:
                logger.exception("执行 DSH 续写任务异常，降级新会话")
                meta["error"] = f"{type(exc).__name__}: {exc}"
                if self.db and session_key:
                    self.db.set_state(f"dsh_session:{session_key}", "")

        # 首次执行或降级重试：通过 --json 启动全新 Session 并持久化 session_id
        guarded_task_init = _guard_task_prompt(task, has_session=False)
        cmd_init = [
            self._dsh_bin,
            "--profile", "headless",
            "--approval-policy", "never",
            "--json",
            guarded_task_init,
        ]

        new_session_id = None
        final_answer: list[str] = []

        try:
            with subprocess.Popen(
                cmd_init,
                cwd=workspace,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            ) as proc:
                try:
                    stdout, stderr = proc.communicate(timeout=timeout)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    meta["status"] = "timeout"
                    return "任务执行时间过长，已在后台中止。"

                for line in stdout.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        msg_type = data.get("type")
                        if msg_type == "session":
                            new_session_id = data.get("sessionId")
                        elif msg_type == "text":
                            final_answer.append(data.get("text", ""))
                    except Exception:
                        pass

                if new_session_id and session_key:
                    self._save_session(session_key, new_session_id)

                reply = "".join(final_answer).strip()
                if reply:
                    meta["status"] = "ok"
                    return reply

                if proc.returncode != 0:
                    logger.warning("DSH 进程非零退出：%s, stderr: %s", proc.returncode, stderr[:200])
                    meta["status"] = "error"
                    meta["error"] = f"exit={proc.returncode} {stderr[:200]}"
                    return "超级大脑执行遇到了一点问题，没有产出有效回答。"

                meta["status"] = "empty"
                return "任务已执行完毕，但未产生文字输出。"

        except Exception as exc:
            logger.exception("执行 DSH 初始化任务失败")
            meta["status"] = "error"
            meta["error"] = f"{type(exc).__name__}: {exc}"
            return f"执行超级大脑任务失败：{exc}"
