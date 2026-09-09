from __future__ import annotations

import json
import logging
import re
from typing import Any
from xml.etree import ElementTree as ET

import requests

from config import Config

logger = logging.getLogger(__name__)


class PoliticalNewsScraper:
    """Scrapes authoritative political/current-affairs news from BBC, Reuters, etc."""

    TIMEOUT = 12
    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/130.0.0.0 Safari/537.36"
        ),
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    MAX_ITEMS = 8

    SOURCES = [
        {
            "name": "BBC World",
            "url": "https://feeds.bbci.co.uk/news/world/rss.xml",
            "type": "rss",
        },
        {
            "name": "BBC China",
            "url": "https://feeds.bbci.co.uk/news/world/asia/china/rss.xml",
            "type": "rss",
        },
        {
            "name": "BBC News",
            "url": "https://feeds.bbci.co.uk/news/rss.xml",
            "type": "rss",
        },
    ]

    def fetch_all(self) -> list[dict[str, str]]:
        """Fetch political news from all sources, deduplicate, return top items."""
        items: list[dict[str, str]] = []

        for src in self.SOURCES:
            try:
                if src["type"] == "rss":
                    items.extend(self._fetch_rss(src["name"], src["url"]))
            except Exception:
                logger.debug("%s fetch failed", src["name"])

        # Deduplicate by title similarity
        seen: set[str] = set()
        unique: list[dict[str, str]] = []
        for item in items:
            key = item["title"][:40]
            if key not in seen:
                seen.add(key)
                unique.append(item)
            if len(unique) >= self.MAX_ITEMS:
                break

        return unique

    def _fetch_rss(self, source_name: str, url: str) -> list[dict[str, str]]:
        try:
            r = requests.get(url, headers=self.HEADERS, timeout=self.TIMEOUT)
            r.raise_for_status()
        except Exception:
            logger.debug("RSS fetch failed: %s", source_name)
            return []

        try:
            root = ET.fromstring(r.content)
        except ET.ParseError:
            logger.debug("RSS parse failed: %s", source_name)
            return []

        items = []
        for item_elem in root.iter("item"):
            title_elem = item_elem.find("title")
            desc_elem = item_elem.find("description")
            if title_elem is None:
                continue
            title = title_elem.text or ""
            if not title.strip():
                continue

            # Clean HTML from description
            desc = desc_elem.text if desc_elem is not None and desc_elem.text else ""
            desc = re.sub(r"<[^>]+>", "", desc)[:200]

            items.append({
                "title": title.strip(),
                "source": source_name,
                "desc": desc.strip(),
            })

        logger.info("%s: %d items", source_name, len(items))
        return items

    def translate_news(self, items: list[dict[str, str]]) -> list[dict[str, str]]:
        """Translate English news titles to Chinese via DeepSeek v4 flash."""
        if not items:
            return items

        titles = [item["title"] for item in items]
        titles_text = "\n".join(f"{i+1}. {t}" for i, t in enumerate(titles))
        prompt = (
            "将以下英文新闻标题翻译成简洁的中文，保持原意，每条一行，"
            "只返回序号+中文翻译，不要其他内容：\n\n" + titles_text
        )

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {Config.DEEPSEEK_TOKEN}",
        }
        body = {
            "messages": [
                {"role": "user", "content": prompt},
            ],
            "model": "deepseek-v4-flash",
            "max_tokens": 1024,
            "temperature": 0.3,
            "stream": False,
        }

        try:
            r = requests.post(
                Config.DEEPSEEK_API,
                headers=headers,
                json=body,
                timeout=30,
            )
            r.raise_for_status()
            result = r.json()["choices"][0]["message"]["content"].strip()
        except Exception:
            logger.exception("News translation failed, returning original")
            return items

        # Parse translated lines: "1. 中文标题"
        translated: dict[int, str] = {}
        for line in result.split("\n"):
            line = line.strip()
            if not line:
                continue
            m = re.match(r"(\d+)[\.\、\)）]\s*(.+)", line)
            if m:
                idx = int(m.group(1)) - 1
                translated[idx] = m.group(2).strip()

        for i, item in enumerate(items):
            if i in translated and translated[i]:
                item["title"] = translated[i]

        return items

    def fetch_for_greeting(self) -> list[dict[str, str]]:
        """Fetch news for the morning greeting. Returns up to 6 items."""
        return self.fetch_all()[:6]

    def fetch_for_tool(self) -> str:
        """Fetch news and format as text blob for AI summarization."""
        items = self.fetch_all()
        if not items:
            return ""

        lines = ["【时政新闻源】"]
        for i, n in enumerate(items, 1):
            lines.append(f"{i}. [{n['source']}] {n['title']}")
            if n.get("desc"):
                lines.append(f"   {n['desc'][:150]}")
        return "\n".join(lines)
