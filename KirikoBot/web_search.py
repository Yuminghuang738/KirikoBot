from __future__ import annotations

import base64
import binascii
import logging
import re
from typing import Any
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


class WebSearch:
    """DeepSeek-style RAG search: search → fetch page content → feed to AI."""

    # 先用谁、后用谁。这个顺序是**按实测结果**排的，不是按名气。
    #
    # Bing 排第一：换到住宅出口之后它是唯一又稳又相关的源。但必须用
    # **cn.bing.com** —— 用 www.bing.com 时，出口在哪个国家就返回哪个国家的
    # 结果（实测韩国出口返回一整页韩语），加 `mkt=zh-CN`/`cc=CN` 反而会让它
    # 返回 0 条。cn.bing.com 不受出口位置影响，稳定给中文结果。
    #
    # 维基百科往后放：它的全文搜索匹配极松，问「IPv6 普及率」会返回
    # 「杜威十进分类法」（那篇只是文中提了一次 IPv6），而且一旦排在前面就会
    # 用这种结果把好结果挤出名额。现在加了标题相关性过滤兜着。
    #
    # 360 在住宅出口下实测 0 条，DDG 对非住宅 IP 之外的很多段也仍会扔
    # CAPTCHA，两者都只作兜底。
    PROVIDERS = ("bing", "so360", "wikipedia", "duckduckgo")

    BING_URL = "https://cn.bing.com/search"

    WIKI_APIS = ("https://zh.wikipedia.org/w/api.php",
                 "https://en.wikipedia.org/w/api.php")

    SO360_URL = "https://www.so.com/s"
    DDG_URL = "https://lite.duckduckgo.com/lite/"

    TIMEOUT = 10
    MAX_SEARCH_RESULTS = 5
    MAX_FETCH_PAGES = 3
    MAX_PAGE_CHARS = 2000  # max chars per page to extract

    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/130.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }

    # 反爬页面的特征句。整页 HTML 里出现这些词**不算**（脚本里就有），
    # 只看标题和开头可见正文。
    BLOCK_TITLE_MARKERS = ("captcha", "challenge", "just a moment",
                           "attention required", "验证", "安全验证")
    BLOCK_BODY_MARKERS = ("complete the following challenge", "select all squares",
                          "confirm this search was made by a human")

    def __init__(self) -> None:
        # 上一次 _search 的结果分类，给调用方用来**如实**告诉用户发生了什么：
        #   ok          找到结果了
        #   no_results  搜索引擎正常，就是没搜到
        #   blocked     被反爬拦了（和「没搜到」是两件事，不能混为一谈）
        self.last_reason = "ok"

    def search_and_fetch(self, query: str) -> str:
        """Search and fetch page content. Returns a text blob for the AI to read."""
        from datetime import datetime
        # Append current year to query for time-sensitive searches
        current_year = datetime.now().strftime("%Y")
        if current_year not in query:
            query = f"{query} {current_year}"
        results = self._search(query)
        if not results:
            return ""

        pages_text: list[str] = []
        fetch_count = 0
        for r in results:
            if fetch_count >= self.MAX_FETCH_PAGES:
                break
            content = self._fetch_page(r["link"])
            if content:
                pages_text.append(
                    f"【来源：{r['title']}】\n{content}"
                )
                fetch_count += 1
            elif r.get("snippet"):
                # 抓不到正文时，至少把搜索结果自带的摘要给 AI —— 总比什么都不给强，
                # 而且维基百科返回的摘要本身信息量就够回答「XX 是什么」。
                pages_text.append(
                    f"【来源：{r['title']}】\n{r['snippet']}"
                )
                fetch_count += 1

        return "\n\n---\n\n".join(pages_text)

    @staticmethod
    def _query_tokens(query: str) -> tuple[list[str], list[str]]:
        """把查询拆成「值得用来判断相关性的词」。

        返回 (ASCII 词, 中文二字组)。中文没法像英文那样按空格切词，用二字组
        近似：只要标题里出现查询的任意一个二字组，就认为沾边。
        """
        ascii_words = [w.lower() for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9._+-]+", query)]
        bigrams: list[str] = []
        for run in re.findall(r"[\u4e00-\u9fff]{2,}", query):
            bigrams.extend(run[i:i + 2] for i in range(len(run) - 1))
        return ascii_words, bigrams

    @classmethod
    def _title_matches_query(cls, title: str, query: str) -> bool:
        """标题是否真的和查询沾边。

        维基百科的全文搜索匹配非常松：问「IPv6 普及率 2026 最新」，它会把
        「杜威十进分类法」排在第一位 —— 那篇只是正文里提过一次 IPv6。这种
        结果排在前面会占掉名额，把真正相关的挤出去，所以按**标题**过滤一道。
        """
        ascii_words, bigrams = cls._query_tokens(query)
        if not ascii_words and not bigrams:
            return True
        low = title.lower()
        return (any(w in low for w in ascii_words)
                or any(b in title for b in bigrams))

    def _search_wikipedia(self, query: str) -> tuple[list[dict[str, str]], bool]:
        """维基百科的搜索 API（先中文，没命中再英文）。

        它是官方给程序用的接口，不会扔 CAPTCHA 也不喂垃圾结果，而且返回体里
        自带摘要 —— 但**全文搜索的排序很松**，所以结果要过一道标题相关性过滤
        （见 `_title_matches_query`）。
        """
        for api in self.WIKI_APIS:
            try:
                r = requests.get(
                    api, headers=self.HEADERS, timeout=self.TIMEOUT,
                    params={"action": "query", "list": "search", "srsearch": query,
                            "format": "json", "srlimit": self.MAX_SEARCH_RESULTS},
                )
                data = r.json()
            except Exception:
                logger.debug("维基百科搜索失败: %s", api, exc_info=True)
                continue
            lang = "zh" if "//zh." in api else "en"
            results: list[dict[str, str]] = []
            for hit in (data.get("query", {}).get("search") or []):
                title = (hit.get("title") or "").strip()
                if not title or not self._title_matches_query(title, query):
                    continue
                snippet = re.sub(r"<[^>]+>", "", hit.get("snippet") or "").strip()
                results.append({
                    "title": title,
                    "link": f"https://{lang}.wikipedia.org/wiki/{quote(title.replace(' ', '_'))}",
                    "snippet": snippet,
                })
            if results:
                return results, False
        return [], False

    # ── 搜索：多引擎按顺序回退 ────────────────────────────

    def _search(self, query: str) -> list[dict[str, str]]:
        """从多个来源**一起**取结果，去重后交给 AI。

        为什么不是「第一个有结果的就用」：本机出口是机房 IP，scraping 系引擎会
        **喂随机垃圾**（实测「什么是 Docker」连查三次，Bing 依次给出
        `MAC Address Vendor Lookup`、`中电科技（南京）电子信息发展有限公司`）。
        垃圾结果「非空」，first-wins 的链式回退会被它堵死，永远轮不到可靠的源。

        改成多源合并之后，单个源抽风也还有别的源兜着；反正下一步是让 AI 综合
        这些搜索结果，多给几份候选反而更容易命中。
        """
        collected: list[dict[str, str]] = []
        seen: set[str] = set()
        blocked_any = False

        for name in self.PROVIDERS:
            if len(collected) >= self.MAX_SEARCH_RESULTS:
                break
            try:
                results, blocked = getattr(self, f"_search_{name}")(query)
            except Exception:
                logger.debug("搜索源 %s 出错", name, exc_info=True)
                continue
            blocked_any = blocked_any or blocked
            # 每个源只取前几条，把名额留给别的源，避免一家独吞
            for r in results[:2]:
                key = (r.get("title") or "")[:40] or r.get("link", "")
                if key in seen:
                    continue
                seen.add(key)
                collected.append(r)

        if collected:
            self.last_reason = "ok"
            logger.info("Search '%s' → %d results", query[:40], len(collected))
        else:
            self.last_reason = "blocked" if blocked_any else "no_results"
            logger.info("Search '%s' → 0 results (%s)", query[:40], self.last_reason)
        return collected

    def _looks_blocked(self, resp: Any, soup: BeautifulSoup) -> bool:
        """是不是反爬页 —— 只在**没解析出结果时**才问这个问题。"""
        if resp.status_code in (202, 403, 429):
            return True
        title = (soup.title.get_text(strip=True) if soup.title else "").lower()
        if any(m in title for m in self.BLOCK_TITLE_MARKERS):
            return True
        head = soup.get_text(" ", strip=True)[:600].lower()
        return any(m in head for m in self.BLOCK_BODY_MARKERS)

    @staticmethod
    def _unwrap_link(url: str) -> str:
        """把搜索引擎的**跳转链接**还原成真实地址。

        Bing 的结果链接长这样：

            https://www.bing.com/ck/a?!&&p=...&u=a1aHR0cHM6Ly96aC53aWtpcGVkaWEu...

        直接抓它会拿到一个只有「Please click here if the page does not redirect
        automatically」的中转页 —— 正文一个字都没有，等于没抓。`u=` 是
        base64url（前面多一个 `a1` 前缀），解出来才是真地址。
        """
        if "bing.com/ck/a" not in url:
            return url
        m = re.search(r"[?&]u=([^&]+)", url)
        if not m:
            return url
        raw = m.group(1)
        if raw.startswith("a1"):
            raw = raw[2:]
        raw = raw.replace("-", "+").replace("_", "/")
        raw += "=" * (-len(raw) % 4)
        try:
            decoded = base64.b64decode(raw).decode("utf-8", "replace")
        except (binascii.Error, ValueError):
            return url
        return decoded if decoded.startswith(("http://", "https://")) else url

    def _search_bing(self, query: str) -> tuple[list[dict[str, str]], bool]:
        r = requests.get(self.BING_URL, headers=self.HEADERS,
                         params={"q": query, "setlang": "zh-CN"},
                         timeout=self.TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        results: list[dict[str, str]] = []
        for li in soup.select("li.b_algo"):
            heading = li.select_one("h2") or li.select_one("h3")
            if heading is None:
                continue
            link_el = heading.select_one("a")
            link = self._unwrap_link((link_el.get("href") if link_el else "") or "")
            # Bing 会把域名和面包屑塞进标题节点里，去掉 cite 才拿到干净的标题
            clean = BeautifulSoup(str(heading), "lxml")
            for junk in clean.select("cite, .b_attribution, .citeurl"):
                junk.decompose()
            title = clean.get_text(" ", strip=True)
            if not link or len(title) < 3:
                continue
            snippet_el = li.select_one(".b_caption p") or li.select_one("p")
            snippet = snippet_el.get_text(" ", strip=True) if snippet_el else ""
            results.append({"title": title, "link": link, "snippet": snippet})
            if len(results) >= self.MAX_SEARCH_RESULTS:
                break
        return results, (not results and self._looks_blocked(r, soup))

    def _search_so360(self, query: str) -> tuple[list[dict[str, str]], bool]:
        r = requests.get(self.SO360_URL, headers=self.HEADERS,
                         params={"q": query}, timeout=self.TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        results: list[dict[str, str]] = []
        for li in soup.select("li.res-list"):
            link_el = li.select_one("h3 a")
            if link_el is None:
                continue
            link = link_el.get("href") or ""
            title = link_el.get_text(" ", strip=True)
            if not link or len(title) < 3:
                continue
            snippet_el = li.select_one(".res-desc, .res-rich")
            snippet = snippet_el.get_text(" ", strip=True) if snippet_el else ""
            results.append({"title": title, "link": link, "snippet": snippet})
            if len(results) >= self.MAX_SEARCH_RESULTS:
                break
        return results, (not results and self._looks_blocked(r, soup))

    def _search_duckduckgo(self, query: str) -> tuple[list[dict[str, str]], bool]:
        r = requests.post(
            self.DDG_URL,
            headers=self.HEADERS,
            data={"q": query, "kl": "cn-zh"},
            timeout=self.TIMEOUT,
        )
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        results: list[dict[str, str]] = []

        for row in soup.select("tr"):
            try:
                links = row.select("a")
                if not links:
                    continue
                title = links[0].get_text(strip=True)
                link = links[0].get("href", "")
                if not link or not title or len(title) < 3:
                    continue
                if "duckduckgo" in link.lower():
                    continue
                desc_span = row.select_one("td.result-snippet")
                snippet = desc_span.get_text(strip=True) if desc_span else ""
                results.append({"title": title, "link": link, "snippet": snippet})
                if len(results) >= self.MAX_SEARCH_RESULTS:
                    break
            except Exception:
                logger.debug("web_search._search 忽略了异常", exc_info=True)
                continue

        return results, (not results and self._looks_blocked(r, soup))

    @staticmethod
    def _real_url_from_shell(html: str) -> str:
        """从搜索引擎的**跳转壳**里挖出真实地址。

        360 的结果链接是 `so.com/link?m=...`，直接 GET 只会得到一个 280 字节的
        空壳，正文里没有任何内容：

            <script>window.location.replace("https://www.runoob.com/...")</script>

        requests 不执行 JS，所以它停在壳上。真实地址就在这段脚本（或 noscript
        里的 meta refresh）里，抠出来再抓一次。
        """
        m = re.search(r"""window\.location\.replace\(\s*["']([^"']+)["']""", html)
        if not m:
            m = re.search(
                r"""http-equiv=["']refresh["'][^>]*?URL=['"]?([^'"\s>]+)""",
                html, re.IGNORECASE,
            )
        url = (m.group(1) if m else "").strip()
        return url if url.startswith("http") else ""

    def _fetch_page(self, url: str, _depth: int = 0) -> str:
        """Fetch a page and extract plain text."""
        try:
            r = requests.get(
                url,
                headers=self.HEADERS,
                timeout=8,
                allow_redirects=True,
            )
            r.raise_for_status()
            # Detect encoding properly
            if r.encoding and r.encoding.lower() in ("iso-8859-1", "latin-1"):
                r.encoding = r.apparent_encoding or "utf-8"
        except Exception:
            logger.debug("Failed to fetch page: %s", url[:60])
            return ""

        # 跳转壳：内容为空、真地址在脚本里。剥一层再抓（只递归一次，防死循环）。
        if _depth == 0:
            real = self._real_url_from_shell(r.text)
            if real and real != url:
                return self._fetch_page(real, _depth=1)

        try:
            soup = BeautifulSoup(r.content, "lxml", from_encoding=r.encoding or "utf-8")

            # Remove non-content elements
            for tag in soup.select(
                "script, style, nav, footer, header, .sidebar, .ad, .advertisement, "
                ".nav, .footer, .header, .comment, .comments, noscript, iframe"
            ):
                tag.decompose()

            # Try to find main content
            main = (
                soup.select_one("main")
                or soup.select_one("article")
                or soup.select_one(".content")
                or soup.select_one("#content")
                or soup.select_one(".post-content")
                or soup.select_one(".article-content")
                or soup.select_one(".entry-content")
                or soup.body
            )

            if main:
                # 优先取**段落**。整块 get_text 会把表格、信息框、导航条全卷进来 ——
                # 维基百科身上尤其明显：开头的发音表会挤掉正文，喂给 AI 的
                # 就是「Tson 平 kueh 入 湘语…」这种谁也读不懂的东西。
                paras = [
                    p.get_text(" ", strip=True) for p in main.select("p")
                ]
                paras = [p for p in paras if len(p) >= 20]
                text = "\n".join(paras) if paras else main.get_text(separator="\n", strip=True)
            else:
                text = soup.get_text(separator="\n", strip=True)

            # Clean up whitespace
            text = re.sub(r"\n{3,}", "\n\n", text)
            text = re.sub(r"[ \t]{2,}", " ", text)

            # 抓到的可能是搜索引擎的**跳转中转页**（只有一句「请点这里如果页面
            # 没有自动跳转」），拿去喂 AI 等于给了满篇废话。这种直接丢掉。
            if self._is_redirect_stub(text):
                logger.debug("跳过跳转中转页: %s", url[:60])
                return ""

            if len(text) > self.MAX_PAGE_CHARS:
                text = text[:self.MAX_PAGE_CHARS] + "..."

            return text
        except Exception:
            logger.debug("Failed to parse page: %s", url[:60])
            return ""

    @staticmethod
    def _is_redirect_stub(text: str) -> bool:
        """是不是「页面即将跳转」那种空壳页。"""
        head = text[:200].lower()
        return (
            "redirect automatically" in head
            or "正在跳转" in head
            or "页面跳转" in head
        ) or len(text.strip()) < 40
