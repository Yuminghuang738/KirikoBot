from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


class WebSearch:
    """DeepSeek-style RAG search: search → fetch page content → feed to AI."""

    SEARCH_URL = "https://lite.duckduckgo.com/lite/"
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

        return "\n\n---\n\n".join(pages_text)

    def _search(self, query: str) -> list[dict[str, str]]:
        try:
            r = requests.post(
                self.SEARCH_URL,
                headers=self.HEADERS,
                data={"q": query, "kl": "cn-zh"},
                timeout=self.TIMEOUT,
            )
            r.raise_for_status()
        except Exception:
            logger.exception("Search failed for: %s", query)
            return []

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

        logger.info("Search '%s' → %d results", query[:40], len(results))
        return results

    def _fetch_page(self, url: str) -> str:
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
                text = main.get_text(separator="\n", strip=True)
            else:
                text = soup.get_text(separator="\n", strip=True)

            # Clean up whitespace
            text = re.sub(r"\n{3,}", "\n\n", text)
            text = re.sub(r"[ \t]{2,}", " ", text)

            if len(text) > self.MAX_PAGE_CHARS:
                text = text[:self.MAX_PAGE_CHARS] + "..."

            return text
        except Exception:
            logger.debug("Failed to parse page: %s", url[:60])
            return ""
