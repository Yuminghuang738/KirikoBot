"""联网搜索：走 DeepSeek 官方的 Search / Fetch 接口。

历史：这里曾经是一套「自己爬搜索引擎」的实现 —— 依次尝试 360 / Bing / 维基百科 /
DuckDuckGo，再把结果页抓回来、从 Bing 的 base64 跳转壳和 360 的 JS 跳转壳里还原
真实地址、识别 CAPTCHA 页、按标题过滤维基百科的松散匹配……

那套东西存在的唯一原因是：当时没有可用的搜索 API。它有一半代码是在对抗反爬
（出口 IP 被标记时，搜索引擎会扔 CAPTCHA 或者喂随机垃圾），而且效果始终不稳定。

DeepSeek 上线了官方的 Search / Fetch 接口（自研搜索引擎），同样用 Bearer 鉴权、
同一个 API Key，直接返回结构化结果和网页正文。所以那一整套对抗逻辑全部删掉了 ——
不是「换了个源」，是这一类问题本身消失了。

    POST https://api.deepseek.com/v1/tools/search
    {query, count, need_content, ori_question}
    → {results: [{title, url, snippet, summary, content, date, authority, ...}]}

计费按请求次数（5 元 / 1000 次成功请求），与返回条数无关，所以 count 给够不额外
花钱；QPS 是账号级 5，超过返回 429。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

import requests

from config import Config

logger = logging.getLogger(__name__)


_RECENCY_RE = re.compile(r"最新|最近|今年|现在|当前|目前|刚刚|新出|新发|latest|recent|newest|current", re.I)
_YEAR_RE = re.compile(r"(?<!\d)(20\d\d)(?!\d)")


def normalize_years(query: str, now: datetime | None = None) -> str:
    """把查询里「明显过期」的年份换成当前年份。

    线上实测：主模型按自己的训练知识（截止 2025）写查询，「最近发售的游戏」会变成
    「2025年最近发售的游戏」，于是搜出来的是旧资料。只在查询带有时效词（最新/最近/
    今年…）时才改，历史类问题（「2020年的疫情」）原样保留。
    """
    now = now or datetime.now()
    if not _RECENCY_RE.search(query):
        return query
    stale = str(now.year - 1)

    def _fix(m: re.Match) -> str:
        return str(now.year) if m.group(1) == stale else m.group(1)

    return _YEAR_RE.sub(_fix, query)


class WebSearch:
    """DeepSeek-style RAG search: Search API → 正文 → 喂给 AI。"""

    MAX_RESULTS = 5          # 交给 AI 的条数
    MAX_PAGE_CHARS = 2000    # 每条结果最多取多少字正文
    TIMEOUT = 20

    # 失败原因 → 给用户的话。和「没搜到」区分开：前几种是**服务端的问题**，
    # 换关键词没用，得让用户知道该等、该充值、还是配置错了。
    REASON_MESSAGES = {
        "quota": "我这边搜索额度用完了，得等充了值才能再搜",
        "rate_limited": "搜太快了被限流了，过一会儿再试吧",
        "auth": "搜索的钥匙不对，得让开发者看一下配置",
        "error": "搜索服务暂时出问题了，待会儿再试试吧",
        "no_results": "没搜到相关的内容，换个说法试试吧",
    }

    def __init__(self) -> None:
        # ok / no_results / quota / rate_limited / auth / error
        self.last_reason = "ok"

    # ── 地址与鉴权 ───────────────────────────────────────

    @staticmethod
    def _base_url() -> str:
        """从 DEEPSEEK_API 推出 API 根地址。

        .env 里通常写的是完整的 chat/completions 地址，这里把它剥成根，
        这样换自建代理/网关时只需要改一个变量。
        """
        api = (Config.DEEPSEEK_API or "https://api.deepseek.com").strip()
        # 长的先剥："/chat/completions" 是 "/v1/chat/completions" 的后缀，
        # 顺序反了会只剥掉后半截，留下 "/v1"，拼出来变成 /v1/v1/tools/search
        for suffix in ("/v1/chat/completions", "/chat/completions"):
            if api.endswith(suffix):
                api = api[: -len(suffix)]
                break
        return api.rstrip("/")

    @classmethod
    def search_url(cls) -> str:
        return f"{cls._base_url()}/v1/tools/search"

    @staticmethod
    def _token() -> str | None:
        token = (Config.DEEPSEEK_TOKEN or "").strip()
        return token or None

    # ── 对外接口 ─────────────────────────────────────────

    def search_and_fetch(self, query: str) -> str:
        """搜一次，把结果正文拼成一段文本给 AI。

        一次请求就拿到正文（`need_content`），所以这里没有「再抓一次页面」那一步。
        """
        results = self._search(query)
        if not results:
            return ""

        chunks: list[str] = []
        for r in results[: self.MAX_RESULTS]:
            title = (r.get("title") or "").strip() or "(无标题)"
            # summary 是给模型读的长摘要（600~800 字）；content 是正文，可能很长。
            # 优先 content 并截断，没有就退回 summary / snippet。
            body = (
                (r.get("summary") or "").strip()
                or (r.get("content") or "").strip()
                or (r.get("snippet") or "").strip()
            )
            if not body:
                continue
            if len(body) > self.MAX_PAGE_CHARS:
                body = body[: self.MAX_PAGE_CHARS] + "…"
            url = (r.get("url") or "").strip()
            source = f"【来源：{title}】" + (f"\n{url}" if url else "")
            chunks.append(f"{source}\n{body}")

        return "\n\n---\n\n".join(chunks)

    def failure_message(self) -> str:
        """这次失败该怎么跟用户说（由调用方决定要不要用它）。"""
        return self.REASON_MESSAGES.get(self.last_reason, self.REASON_MESSAGES["error"])

    # ── 真正发请求 ───────────────────────────────────────

    def _search(self, query: str) -> list[dict[str, Any]]:
        token = self._token()
        if not token:
            self.last_reason = "auth"
            logger.warning("联网搜索没有配置 DEEPSEEK_TOKEN")
            return []

        # 不取正文（need_content=False），只用 summary：summary 本来就是给模型读的
        # 600~800 字长摘要。基准里和取正文的质量持平，但资料体积从 ~9000 字降到 ~5000 字。
        payload = {
            "query": normalize_years(query),
            "count": self.MAX_RESULTS,
            "need_content": False,
        }
        try:
            resp = requests.post(
                self.search_url(),
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self.TIMEOUT,
            )
        except requests.RequestException as exc:
            self.last_reason = "error"
            logger.warning("联网搜索请求失败: %s", type(exc).__name__)
            return []

        if resp.status_code != 200:
            self.last_reason = self._reason_for_status(resp.status_code)
            logger.warning(
                "联网搜索 HTTP %s → %s: %s",
                resp.status_code, self.last_reason, (resp.text or "")[:200],
            )
            return []

        try:
            data = resp.json()
        except ValueError:
            self.last_reason = "error"
            logger.warning("联网搜索返回的不是 JSON: %s", (resp.text or "")[:200])
            return []

        results = data.get("results") or []
        if not isinstance(results, list) or not results:
            # 空结果**不计费**，所以这不是「出问题」，就是真没搜到
            self.last_reason = "no_results"
            logger.info("Search '%s' → 0 results", query[:40])
            return []

        self.last_reason = "ok"
        logger.info("Search '%s' → %d results (deepseek)", query[:40], len(results))
        return results

    @staticmethod
    def _reason_for_status(status: int) -> str:
        if status == 402:
            return "quota"          # 余额不足
        if status == 429:
            return "rate_limited"   # QPS 超了
        if status in (401, 403):
            return "auth"
        return "error"
