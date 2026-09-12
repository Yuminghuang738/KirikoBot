"""Grow the amp-head library by crawling Wikipedia.

Why Wikipedia: it has a real API, stable URLs, no anti-bot wall, and — unlike
retailers — model-specific articles. Sweetwater answers 403 and shop pages
carry the *least* trustworthy data (prices move weekly), so there is nowhere
better to point this at.

The important rule in here is that the model is used to **extract**, never to
write. Every field must come from the fetched article text; anything the
article does not state is left empty rather than filled in from the model's
priors. That is the whole reason the hand-written library exists as a static
file instead of an AI prompt, and a crawler must not quietly undo it. Each row
therefore keeps its `source_url` so any claim can be checked or deleted.

Known limits, worth repeating because they shape expectations:

* Wikipedia's amp coverage is finite and skewed to vintage/classic gear.
  Fender/Marshall/Vox/Mesa are well covered; Diezel, Friedman, Revv and
  Two-Rock have no articles at all. So this roughly doubles the library once
  and then plateaus — it is not an endless supply.
* Street prices are essentially absent from Wikipedia, so crawled rows usually
  have no price and the push omits that line rather than inventing a number.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from typing import Any

import requests

from ai_server import quick_chat

logger = logging.getLogger(__name__)

WIKI_API = "https://en.wikipedia.org/w/api.php"
# Wikipedia throttles bursts hard: ten rapid category calls start returning
# empty bodies. A real UA plus a minimum gap keeps us within the polite lane.
USER_AGENT = "KirikoBot/1.0 (guitar amp library; +https://github.com/Yuminghuang738/KirikoBot)"

CATEGORIES = (
    "Category:Instrument amplifiers",
    "Category:Valve amplifiers",
    "Category:Fender amplifiers",
    "Category:Marshall amplifiers",
    "Category:Mesa/Boogie",
    "Category:Vox (company)",
    "Category:Peavey Electronics",
    "Category:Gibson amplifiers",
    "Category:Boss Corporation",
    "Category:Line 6 (company)",
)

# Categories hold companies and technology articles alongside actual amps.
# Matching is on the title's HEAD NOUN (its last word), not substrings: model
# names routinely contain generic words ("Epiphone Valve Junior") but a title
# *ending* in one is describing a category of thing, not a product. Substring
# matching got this wrong in both directions — it let "Dattorro industry
# scheme" through while a naive rule would have dropped the Valve Junior.
GENERIC_HEAD_NOUNS = {
    "amplifier", "amplifiers", "amplification", "scheme", "stack", "sound",
    "modeling", "modelling", "tube", "tubes", "valve", "valves", "company",
    "corporation", "businessman", "list", "history", "technology", "circuit",
    "design", "designs", "specification", "specifications", "transmitter",
    "transmitters", "industry", "equipment", "electronics", "scheme",
    "manufacturer", "manufacturers", "brand", "brands", "maker", "makers",
    "builder", "builders", "vendor", "vendors", "speaker", "speakers",
    "cabinet", "cabinets", "monitor", "monitors",
    # Electronics and styling articles that survive the category check:
    # "Single-ended triode" (a circuit), "Fender tweed" (a finish),
    # "Fender Reverb Unit" (an outboard tank).
    "triode", "pentode", "tetrode", "topology", "tweed", "unit",
}

NOT_A_MODEL_PREFIXES = ("list of", "index of", "outline of", "history of")

# An article is an amp only if Wikipedia files it under an amplifier category
# that is about the gear rather than about a company that makes gear.
AMP_CATEGORY_RE = re.compile(r"amplifier", re.I)
NON_AMP_CATEGORY_RE = re.compile(
    r"manufacturer|companies|company|corporation|business|brands|people|musicians",
    re.I,
)


def _norm(text: str) -> str:
    """Lowercase, punctuation-free, single-spaced — for loose model comparison."""
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _looks_like_model(title: str) -> bool:
    """True when a Wikipedia title plausibly names one amp model."""
    title = (title or "").strip()
    if not title or len(title.split()) < 2:
        return False
    low = title.lower()
    if low.startswith(NOT_A_MODEL_PREFIXES):
        return False
    # Strip punctuation so "Amp-in-case" and "Mark IIC+" behave.
    head = re.sub(r"[^\w\-]", "", low.split()[-1])
    return head not in GENERIC_HEAD_NOUNS


EXTRACT_FIELDS = ("year", "origin", "kind", "power", "tubes",
                  "tone", "price", "famous", "tip")

EXTRACT_SYSTEM = """你在把维基百科正文整理成结构化的吉他音箱资料。

铁律：只能抽取正文里**明确写到**的内容。
- 正文没有提到的字段，必须返回空字符串 ""
- 绝对不要用你自己的先验知识补全、推测或润色任何字段
- 不要写正文里没有的年份、价格、电子管型号、使用者
- 宁可留空，也不要编造。留空是可以接受的，编造是不可接受的

各字段要求（全部用中文）：
- year: 诞生/发布年份，只写数字和"年"，如 "1981 年"
- origin: 产地国家，如 "英国"
- kind: 类型，如 "全电子管" / "晶体管" / "数字建模"
- power: 输出功率，如 "100W"
- tubes: 电子管配置，如 "EL34×4 / ECC83×3"
- tone: 音色特点，1-2 句，忠于原文描述
- price: **仅当正文明确给出价格时才填**，否则空字符串。
  注意区分：若正文给的是产品**当年发布时的原始售价**（常见写法如
  "originally sold for $315"、"list price of $67.95"），必须写成
  "当年售价 $315（非当前市价）" 这样的形式，不要直接写成市价，
  否则会被误读成现在的二手行情
- famous: 知名使用者或代表作，仅当正文写了
- tip: 使用/选购建议，1 句，基于正文内容

只输出 JSON，不要任何解释、不要 markdown 代码块：
{"year":"","origin":"","kind":"","power":"","tubes":"","tone":"","price":"","famous":"","tip":""}"""


def split_title(title: str) -> tuple[str, str]:
    """'Marshall JCM800' -> ('Marshall', 'JCM800')."""
    title = (title or "").strip()
    if not title:
        return "", ""
    parts = title.split(" ", 1)
    brand = parts[0]
    model = parts[1] if len(parts) > 1 else ""
    if brand.lower() in ("mesa/boogie", "mesa"):
        return "Mesa/Boogie", model
    return brand, model


class AmpHeadCrawler:
    """Crawl Wikipedia and add new amp heads to the library."""

    MIN_INTERVAL = 1.5      # seconds between API calls
    MAX_PER_RUN = 8         # bounded: be polite, and bound the AI spend

    def __init__(self, db: Any, max_per_run: int | None = None) -> None:
        self.db = db
        self.max_per_run = max_per_run or self.MAX_PER_RUN
        self._last_call = 0.0
        self._known: dict[str, list[str]] = {}
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})

    def _load_known(self) -> None:
        """Existing models per brand, for near-duplicate rejection."""
        known: dict[str, list[str]] = {}
        try:
            for head in self.db.get_amp_heads():
                known.setdefault(_norm(head["brand"]), []).append(_norm(head["model"]))
        except Exception:
            logger.exception("Amp crawl: could not read the existing library")
        self._known = known

    def _is_near_duplicate(self, brand: str, model: str) -> bool:
        """Skip a candidate that is a looser/narrower version of an existing amp.

        Wikipedia names things at a different granularity than the curated
        file: it has "Marshall JCM800" where we have "JCM800 2203", "Fender
        Twin" where we have "Twin Reverb". Both would otherwise be pushed on
        different days and read as duplicates.

        The comparison is token containment, which errs toward *skipping*.
        That is the deliberate trade: losing a marginal entry is better than
        the same amp showing up twice, and the curated file can always be
        extended by hand when a skip was wrong.
        """
        target = _norm(model)
        if not target:
            return True
        for other in self._known.get(_norm(brand), []):
            if not other:
                continue
            if target == other or target in other or other in target:
                return True
        return False

    # ── HTTP ───────────────────────────────────────────────
    def _api(self, params: dict[str, Any], retries: int = 3) -> dict[str, Any] | None:
        """One throttled, retried API call. Returns None if it never worked."""
        params = {"format": "json", "formatversion": "2", **params}
        for attempt in range(retries):
            gap = time.time() - self._last_call
            if gap < self.MIN_INTERVAL:
                time.sleep(self.MIN_INTERVAL - gap)
            self._last_call = time.time()
            try:
                r = self.session.get(WIKI_API, params=params, timeout=20)
                if r.status_code == 200:
                    try:
                        return r.json()
                    except ValueError:
                        logger.warning("Wikipedia returned non-JSON for %s", params)
                else:
                    logger.info("Wikipedia HTTP %d for %s", r.status_code, params.get("list") or params.get("prop"))
            except requests.RequestException as exc:
                logger.info("Wikipedia request failed (%s)", type(exc).__name__)
            time.sleep(2 * (attempt + 1))
        return None

    def _category_members(self, category: str) -> list[str]:
        data = self._api({
            "action": "query", "list": "categorymembers",
            "cmtitle": category, "cmlimit": "500",
        })
        members = ((data or {}).get("query", {}) or {}).get("categorymembers") or []
        return [m["title"] for m in members if not m.get("title", "").startswith("Category:")]

    def _amplifier_titles(self, titles: list[str]) -> set[str]:
        """Which of these titles actually sit in an amplifier category.

        The manufacturer categories ("Category:Boss Corporation") are
        company-wide: they list the brand's entire output, so they drag in
        guitars, organs, drum machines and stomp boxes — Boss DS-1, Vox
        Phantom, Vox Continental, Tone Bender. No word-list can safely
        separate those from amp models, because pedal and amp names look
        identical. Asking Wikipedia what the article *is* is both cheaper and
        far more accurate.

        Batched 50 titles per request, so this costs one call per crawl
        instead of one per candidate.
        """
        found: set[str] = set()
        for i in range(0, len(titles), 50):
            batch = titles[i:i + 50]
            data = self._api({
                "action": "query", "prop": "categories", "cllimit": "max",
                "titles": "|".join(batch),
            })
            pages = ((data or {}).get("query", {}) or {}).get("pages") or []
            for page in pages:
                name = page.get("title") or ""
                if page.get("missing"):
                    continue
                cats = [c.get("title", "") for c in (page.get("categories") or [])]
                amp_like = [c for c in cats if AMP_CATEGORY_RE.search(c)]
                # A manufacturer category also contains "amplifier", so require
                # at least one that is about the gear itself.
                if any(not NON_AMP_CATEGORY_RE.search(c) for c in amp_like):
                    found.add(name)
        return found

    def _article_text(self, title: str) -> str:
        """Plain-text article body — no HTML scraping needed."""
        data = self._api({
            "action": "query", "prop": "extracts", "explaintext": "1",
            "redirects": "1", "titles": title,
        })
        pages = ((data or {}).get("query", {}) or {}).get("pages") or []
        if not pages:
            return ""
        return (pages[0].get("extract") or "").strip()

    # ── Candidates ─────────────────────────────────────────
    def candidates(self) -> list[str]:
        """Article titles that plausibly describe one amp model."""
        self._load_known()
        titles: list[str] = []
        seen: set[str] = set()
        for cat in CATEGORIES:
            for title in self._category_members(cat):
                if title in seen or not _looks_like_model(title):
                    continue
                seen.add(title)
                titles.append(title)
        pre = []
        for title in titles:
            brand, model = split_title(title)
            if not brand or not model:
                continue
            if self.db.amp_head_exists(brand, model):
                continue
            if self._is_near_duplicate(brand, model):
                continue
            pre.append(title)

        confirmed = self._amplifier_titles(pre)
        out = [t for t in pre if t in confirmed]
        skipped = len(pre) - len(out)
        if skipped:
            logger.info("Amp crawl: dropped %d titles not filed as amplifiers", skipped)
        return out

    # ── Extraction ─────────────────────────────────────────
    def extract(self, title: str, article: str) -> dict[str, str] | None:
        """Ask the model to extract fields FROM THE ARTICLE ONLY."""
        if len(article) < 200:
            return None
        # Keep the prompt bounded; the lead + specs sections carry the facts.
        body = article[:6000]
        reply = quick_chat(
            EXTRACT_SYSTEM,
            f"条目标题：{title}\n\n正文：\n{body}",
            max_tokens=700, temperature=0.0, thinking=False,
            source="amp_crawl",
        )
        if not reply:
            return None
        match = re.search(r"\{.*\}", reply, re.S)
        if not match:
            logger.info("Amp extraction returned no JSON for %s", title)
            return None
        try:
            data = json.loads(match.group(0))
        except ValueError:
            logger.info("Amp extraction returned invalid JSON for %s", title)
            return None
        if not isinstance(data, dict):
            return None
        return {k: str(data.get(k) or "").strip() for k in EXTRACT_FIELDS}

    # ── Entry point ────────────────────────────────────────
    def crawl(self, limit: int | None = None) -> dict[str, Any]:
        """Add up to `limit` new amp heads. Never raises."""
        limit = self.max_per_run if limit is None else limit
        summary = {"found": 0, "added": 0, "skipped": 0, "titles": []}
        try:
            titles = self.candidates()
        except Exception:
            logger.exception("Amp crawl: candidate lookup failed")
            return summary
        summary["found"] = len(titles)
        if not titles:
            return summary

        for title in titles:
            if summary["added"] >= limit:
                break
            try:
                article = self._article_text(title)
                fields = self.extract(title, article)
                if not fields:
                    summary["skipped"] += 1
                    continue
                brand, model = split_title(title)
                # A model with no substance would produce an empty card.
                if not fields.get("tone") and not fields.get("year"):
                    summary["skipped"] += 1
                    continue
                fields["brand"], fields["model"] = brand, model
                url = "https://en.wikipedia.org/wiki/" + title.replace(" ", "_")
                if self.db.add_amp_head(fields, source="wikipedia", source_url=url):
                    summary["added"] += 1
                    summary["titles"].append(f"{brand} {model}")
                else:
                    summary["skipped"] += 1
            except Exception:
                logger.exception("Amp crawl failed for %s", title)
                summary["skipped"] += 1
        logger.info("Amp crawl: %d found, %d added, %d skipped",
                    summary["found"], summary["added"], summary["skipped"])
        return summary


def run_weekly(db: Any, days: int = 7) -> dict[str, Any] | None:
    """Crawl at most once every `days`, tracked in app_state.

    Persisted rather than a module global so a restart cannot trigger a second
    crawl (or silently skip a week).
    """
    last = db.get_state("amp_crawl_last") or ""
    if last:
        try:
            elapsed = (datetime.now() - datetime.strptime(last, "%Y-%m-%d %H:%M:%S")).days
            if elapsed < days:
                return None
        except ValueError:
            logger.info("Unparseable amp_crawl_last=%r, crawling anyway", last)
    summary = AmpHeadCrawler(db).crawl()
    db.set_state("amp_crawl_last", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    return summary
