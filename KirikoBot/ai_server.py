from __future__ import annotations

import json
import logging
import re
from typing import Any

import markdown
import requests
from requests.adapters import HTTPAdapter, Retry

from config import Config

logger = logging.getLogger(__name__)


class AiServer:
    def __init__(
        self,
        system_text: str,
        user_text: str,
        history_list: list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        model_type: str = "deepseek-v4-flash",
        thinking_type: str = "disabled",
    ) -> None:
        if history_list is None:
            history_list = []
        if tools is None:
            tools = []

        self.system_text = system_text
        self.user_text = user_text
        self.model_type = model_type
        self.thinking_type = thinking_type
        self.history_list = history_list
        self.tools = tools

        self.ai_message: dict[str, Any] = {}
        self.ai_text: str = ""
        self.airesponse_tool_id: str = ""
        self.airesponse_tool_calls: list[dict[str, Any]] = []
        self.tool_results: list[dict[str, Any]] = []
        self.reasoning_content: str = ""

    @staticmethod
    def _create_session() -> requests.Session:
        session = requests.Session()
        retry_strategy = Retry(
            total=Config.MAX_RETRIES,
            backoff_factor=1.0,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods={"POST"},
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def ai_request(self) -> None:
        # Reset to prevent stale tool_calls from leaking into history on error
        self.ai_message = {}
        self.ai_text = ""
        self.airesponse_tool_calls = []
        self.airesponse_tool_id = ""

        request_dict: dict[str, Any] = {
            "messages": [
                {"content": self.system_text, "role": "system"},
                *self.history_list,
                {"content": self.user_text, "role": "user"},
            ],
            "model": self.model_type,
            "thinking": {"type": self.thinking_type},
            "tools": self.tools,
            "tool_choice": "auto",
            "response_format": {"type": "text"},
            "max_tokens": 6000,
            "frequency_penalty": 0,
            "presence_penalty": 0,
            "temperature": 0,
            "top_p": 0.9,
            "stop": None,
            "stream": False,
            "stream_options": None,
            "logprobs": False,
            "top_logprobs": None,
        }

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {Config.DEEPSEEK_TOKEN}",
        }

        session = self._create_session()

        try:
            logger.debug("DeepSeek request: %s", json.dumps(request_dict, ensure_ascii=False))
            response = session.post(
                Config.DEEPSEEK_API,
                headers=headers,
                data=json.dumps(request_dict),
                timeout=Config.REQUEST_TIMEOUT,
            )
            response.raise_for_status()
        except requests.exceptions.Timeout:
            logger.error("DeepSeek API request timed out after %ss", Config.REQUEST_TIMEOUT)
            self.ai_text = "抱歉，AI思考时间有点长，请稍后再试~"
            return
        except requests.exceptions.ConnectionError:
            logger.exception("DeepSeek API connection error")
            self.ai_text = "抱歉，网络连接出现了问题，请稍后再试~"
            return
        except requests.exceptions.HTTPError as e:
            logger.error("DeepSeek API HTTP error: %s", e)
            status_code = e.response.status_code if e.response is not None else None
            # Log response body for debugging
            if e.response is not None:
                try:
                    logger.error("DeepSeek error body: %s", e.response.text[:500])
                except Exception:
                    pass
            if status_code == 401:
                self.ai_text = "抱歉，AI服务配置有问题，请联系管理员~"
            elif status_code == 429:
                self.ai_text = "抱歉，AI服务请求太频繁了，请稍后再试~"
            elif status_code in (500, 502, 503, 504):
                self.ai_text = "抱歉，AI服务暂时不可用，请稍后再试~"
            else:
                self.ai_text = f"抱歉，AI服务返回了异常状态({status_code})，请稍后再试~"
            return
        except Exception:
            logger.exception("Unexpected error during DeepSeek API request")
            self.ai_text = "抱歉，处理请求时遇到了问题，请稍后再试~"
            return

        try:
            response_data = response.json()
        except (json.JSONDecodeError, ValueError):
            logger.error("DeepSeek API returned non-JSON response: %s", response.text[:500])
            self.ai_text = "抱歉，AI服务返回了异常数据，请稍后再试~"
            return

        try:
            self.ai_message = response_data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            logger.error("Unexpected DeepSeek response structure: %s", json.dumps(response_data, ensure_ascii=False)[:500])
            if "error" in response_data:
                logger.error("DeepSeek API error: %s", response_data["error"])
            self.ai_text = "抱歉，AI服务返回了意外的数据格式，请稍后再试~"
            return

        # Capture thinking chain for logging/frontend display
        self.reasoning_content = self.ai_message.get("reasoning_content", "") or ""

        raw_content = self.ai_message.get("content", "")
        try:
            self.ai_text = self._format_for_qq(raw_content)
        except Exception:
            logger.exception("Failed to process markdown content")
            self.ai_text = raw_content

    def add_tool_result(self, tool_call_id: str, result: str) -> None:
        self.tool_results.append({"tool_call_id": tool_call_id, "content": result})

    def follow_up_request(self, original_tool_calls: list[dict[str, Any]]) -> None:
        """Send a follow-up request with tool call results injected into messages.
        This enables multi-turn conversation where the AI incorporates tool results."""
        # Capture original message fields before resetting. reasoning_content MUST be
        # passed back to the API when thinking mode is enabled, or DeepSeek returns 400.
        original_content = self.ai_message.get("content", "")
        original_reasoning = self.ai_message.get("reasoning_content", "")
        self.ai_message = {}
        self.ai_text = ""

        # Build messages: system + history + user + assistant(tool_calls) + tool_results
        assistant_msg: dict[str, Any] = {"role": "assistant", "tool_calls": original_tool_calls}
        if original_reasoning:
            assistant_msg["reasoning_content"] = original_reasoning
        if original_content:
            assistant_msg["content"] = original_content
        follow_up_messages: list[dict[str, Any]] = [
            {"content": self.system_text, "role": "system"},
            *self.history_list,
            {"content": self.user_text, "role": "user"},
            assistant_msg,
        ]
        for tr in self.tool_results:
            follow_up_messages.append({"role": "tool", **tr})

        request_dict: dict[str, Any] = {
            "messages": follow_up_messages,
            "model": self.model_type,
            "thinking": {"type": self.thinking_type},
            "max_tokens": 6000,
            "frequency_penalty": 0,
            "presence_penalty": 0,
            "temperature": 0,
            "top_p": 0.9,
            "stream": False,
        }

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {Config.DEEPSEEK_TOKEN}",
        }

        session = self._create_session()

        try:
            logger.debug("Follow-up request: %s", json.dumps(request_dict, ensure_ascii=False))
            response = session.post(
                Config.DEEPSEEK_API,
                headers=headers,
                data=json.dumps(request_dict),
                timeout=Config.REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            response_data = response.json()
            self.ai_message = response_data["choices"][0]["message"]
            self.reasoning_content = self.ai_message.get("reasoning_content", "") or ""
            raw_content = self.ai_message.get("content", "")
            self.ai_text = self._format_for_qq(raw_content)
        except requests.exceptions.HTTPError as e:
            error_body = ""
            if e.response is not None:
                try:
                    error_body = e.response.text[:1000]
                except Exception:
                    pass
            logger.error("Follow-up HTTP %s: %s", e.response.status_code if e.response is not None else "?", error_body)
            self.ai_text = ""
        except Exception:
            logger.exception("Follow-up AI request failed")
            self.ai_text = ""

    @staticmethod
    def vision_analyze(image_url_or_path: str, prompt: str = "", response_format: str = "text", max_tokens: int = 300) -> str | None:
        """Analyze an image via DeepSeek's vision model.

        Uses the same endpoint and token as the chat model
        (Config.DEEPSEEK_API + Config.DEEPSEEK_TOKEN) with Config.VISION_MODEL.
        Supports HTTP URLs and local file paths (via base64 data URI).
        Downloads remote URLs locally to avoid CDN access issues.
        Returns the API response content string, or None if vision is unavailable.
        """
        if not Config.VISION_ENABLED:
            logger.debug("Vision disabled (set VISION_ENABLED=1 in .env)")
            return None

        import base64
        import os as _os
        import tempfile
        import requests as _requests

        # Determine content: if it's a remote URL, try downloading first
        local_path = None
        if image_url_or_path.startswith(("http://", "https://")):
            try:
                r = _requests.get(image_url_or_path, timeout=10, stream=True)
                r.raise_for_status()
                ext = _os.path.splitext(image_url_or_path.split("?")[0])[1].lower() or ".png"
                if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
                    ext = ".png"
                fd, local_path = tempfile.mkstemp(suffix=ext)
                with _os.fdopen(fd, "wb") as f:
                    for chunk in r.iter_content(8192):
                        f.write(chunk)
                image_url_or_path = local_path
            except Exception:
                pass  # Download failed, try passing URL directly

        # Strip file:// prefix if present (defense-in-depth)
        if image_url_or_path.startswith("file://"):
            image_url_or_path = image_url_or_path[7:]

        # Build image content
        if image_url_or_path.startswith(("http://", "https://", "data:")):
            image_content = {"type": "image_url", "image_url": {"url": image_url_or_path}}
        else:
            try:
                with open(image_url_or_path, "rb") as f:
                    data = f.read()
                ext = _os.path.splitext(image_url_or_path)[1].lower().lstrip(".")
                if ext == "jpg":
                    ext = "jpeg"
                elif ext not in ("png", "jpeg", "gif", "webp"):
                    ext = "png"
                b64 = base64.b64encode(data).decode()
                image_content = {"type": "image_url", "image_url": {"url": f"data:image/{ext};base64,{b64}"}}
            except Exception:
                if local_path:
                    try:
                        _os.unlink(local_path)
                    except Exception:
                        pass
                logger.exception("Unable to read image for vision analysis")
                return None

        system_prompt = prompt if prompt else "描述此图片的内容和情绪，30字以内"
        if response_format == "json":
            system_prompt += " 输出必须是纯JSON格式，不要markdown代码块。"

        payload: dict[str, Any] = {
            "model": Config.VISION_MODEL,
            "messages": [
                {"role": "user", "content": [
                    {"type": "text", "text": system_prompt},
                    image_content,
                ]},
            ],
            "max_tokens": max_tokens,
            "temperature": 0,
        }

        session = AiServer._create_session()
        try:
            resp = session.post(
                Config.DEEPSEEK_API,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {Config.DEEPSEEK_TOKEN}",
                },
                json=payload,
                timeout=45,
            )
            if not resp.ok:
                try:
                    err_body = resp.text[:500]
                except Exception:
                    err_body = "(unable to read)"
                logger.error("Vision API HTTP %s: %s", resp.status_code, err_body)
            resp.raise_for_status()
            result = resp.json()["choices"][0]["message"]["content"]
        except Exception:
            logger.exception("Vision API call failed")
            result = None
        finally:
            if local_path:
                try:
                    _os.unlink(local_path)
                except Exception:
                    pass

        return result

    @staticmethod
    def vision_analyze_with_category(image_url_or_path: str) -> dict | None:
        """Single vision API call → description + emotion + category in JSON.

        Replaces the old two-step (vision → DeepSeek categorize) with one
        unified call. The vision model directly outputs structured JSON.

        Returns dict with keys: description, emotion, category.
        Returns None if vision is unavailable or the call fails.
        """
        from sticker_collector import STICKER_CATEGORIES

        cats = "、".join(sorted(c for c in STICKER_CATEGORIES if c != "未分类"))
        prompt = (
            "请详细描述这张图片/表情包的内容（主体、动作、场景、文字），以及传达的情绪。\n"
            f"从以下类别中选择最合适的分类：{cats}。\n"
            "以JSON格式输出，不要markdown代码块：\n"
            '{"description":"详细描述（30-50字）","emotion":"情绪","category":"分类"}'
        )
        result = AiServer.vision_analyze(image_url_or_path, prompt, response_format="json")
        if not result:
            return None
        try:
            data = json.loads(result)
        except (json.JSONDecodeError, ValueError):
            # Try stripping markdown fences
            cleaned = result.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[-1].rsplit("\n", 1)[0]
            try:
                data = json.loads(cleaned)
            except (json.JSONDecodeError, ValueError):
                logger.warning("Vision API returned non-JSON for categorize: %s", result[:100])
                return None
        cat = data.get("category", "其他")
        if cat not in STICKER_CATEGORIES:
            cat = "其他"
        return {
            "description": data.get("description", ""),
            "emotion": data.get("emotion", ""),
            "category": cat,
        }

    @staticmethod
    def vision_chat_reply(
        image_url_or_path: str,
        role_prompt: str = "",
        user_name: str = "",
        user_text: str = "",
    ) -> str | None:
        """Vision API end-to-end: understand image + generate Kiriko-style reply.

        Single vision call replaces the old two-step (vision → DeepSeek) pipeline.
        Embeds Kiriko's personality (GROUP_ROLE/PRIVATE_ROLE) into the prompt so
        the vision model responds in character directly.

        Returns the reply text, or None if vision is unavailable/fails.
        """
        parts: list[str] = []
        if role_prompt:
            parts.append(role_prompt)
        parts.append(f"用户 {user_name} 给你发了一张图片/表情包。")
        if user_text.strip():
            parts.append(f"用户同时说：{user_text.strip()}")
        parts.append(
            "请根据图片内容，用可爱自然的语气做出回应。"
            "直接像聊天一样回复即可，30-80字，适当使用颜文字。"
            "不需要描述图片内容。"
        )
        instruction = "\n".join(parts)
        return AiServer.vision_analyze(image_url_or_path, prompt=instruction, max_tokens=500)

    @staticmethod
    def vision_sticker_battle(
        image_url_or_path: str,
        role_prompt: str = "",
        user_name: str = "",
        round_num: int = 1,
    ) -> dict | None:
        """Vision API for sticker battle: rate opponent's sticker + generate counter-attack.

        Returns dict: {score: int, comment: str, comeback: str} or None on failure.
        score: 1-10 rating of the sticker's quality/humor/impact
        comment: short witty remark about the sticker (≤10 chars)
        comeback: Kiriko's counter-attack line (≤20 chars, cute/funny style)
        """
        parts: list[str] = []
        if role_prompt:
            parts.append(role_prompt)
        parts.append(f"【斗图模式 第{round_num}回合】")
        parts.append(f"用户 {user_name} 发了一张斗图表情包。")
        parts.append(
            "请做两件事：\n"
            "1. 给这张表情包打分（1-10分），简短点评（10字以内）\n"
            "2. 用Kiriko的风格给出反击回复（20字以内），可爱但有战斗力，可以带颜文字\n\n"
            "以JSON格式输出（不要markdown代码块）：\n"
            '{"score": 7, "comment": "你的点评", "comeback": "你的反击"}'
        )
        instruction = "\n".join(parts)
        result = AiServer.vision_analyze(image_url_or_path, prompt=instruction, response_format="json", max_tokens=400)
        if not result:
            return None
        try:
            data = json.loads(result)
        except (json.JSONDecodeError, ValueError):
            # Try stripping markdown fences
            cleaned = result.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[-1].rsplit("\n", 1)[0]
            try:
                data = json.loads(cleaned)
            except (json.JSONDecodeError, ValueError):
                logger.warning("Battle vision returned non-JSON: %s", result[:100])
                return None
        return {
            "score": max(0, min(10, int(data.get("score", 5)))),
            "comment": str(data.get("comment", "") or "")[:20],
            "comeback": str(data.get("comeback", "") or "")[:20],
        }

    @staticmethod
    def _format_for_qq(text: str) -> str:
        """Convert markdown to QQ-friendly plain text."""
        # Convert markdown to HTML then strip tags
        html = markdown.markdown(text, extensions=["extra"])
        plain = re.sub(r"<[^>]+>", "", html)

        # Replace markdown tables with simple list format
        plain = re.sub(r"^\|.*\|$", lambda m: m.group().replace("|", " · "), plain, flags=re.MULTILINE)
        # Collapse excessive newlines
        plain = re.sub(r"\n{3,}", "\n\n", plain)
        # Remove horizontal rules
        plain = re.sub(r"^[-*_]{3,}\s*$", "", plain, flags=re.MULTILINE)

        # Truncate if still too long for QQ
        if len(plain) > 2500:
            plain = plain[:2400] + "\n\n...（内容过长已截断）"

        return plain.strip()
