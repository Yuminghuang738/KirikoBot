"""Crawled amp heads: provenance, dedupe, and the "never invent" rule.

The crawler is the one place where AI output enters a library that was
deliberately hand-curated for factual accuracy, so these tests concentrate on
the properties that keep that safe: the model only extracts, nothing is
overwritten, and every crawled row can be traced back to its source.
"""
from __future__ import annotations

import json

import pytest


class TestProvenance:
    def test_curated_rows_are_marked_manual(self, db):
        from amp_heads_data import AMP_HEADS

        assert db.count_amp_heads("manual") == len(AMP_HEADS)
        assert db.count_amp_heads("wikipedia") == 0

    def test_rows_carry_source_columns(self, db):
        head = db.get_amp_head_of_the_day()
        assert "source" in head and "source_url" in head and "fetched_at" in head

    def test_provenance_columns_are_migrated_onto_old_dbs(self, tmp_path):
        """Databases created before the crawl existed must gain the columns."""
        import sqlite3

        from database_manager import DatabaseManager

        path = str(tmp_path / "legacy.db")
        conn = sqlite3.connect(path)
        conn.execute(
            """CREATE TABLE amp_heads(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                brand TEXT NOT NULL, model TEXT NOT NULL,
                year TEXT DEFAULT '', origin TEXT DEFAULT '', kind TEXT DEFAULT '',
                power TEXT DEFAULT '', tubes TEXT DEFAULT '', tone TEXT DEFAULT '',
                price TEXT DEFAULT '', famous TEXT DEFAULT '', tip TEXT DEFAULT '',
                UNIQUE(brand, model))"""
        )
        conn.execute(
            "INSERT INTO amp_heads (brand, model, tone) VALUES ('Old','Amp','老数据')"
        )
        conn.commit()
        conn.close()

        db = DatabaseManager(path)
        cols = {r[1] for r in db.fetch_data("PRAGMA table_info(amp_heads)")}
        assert {"source", "source_url", "fetched_at"} <= cols
        keep = db.fetch_data("SELECT tone FROM amp_heads WHERE brand='Old'")
        assert keep[0][0] == "老数据", "migration must not lose existing rows"


class TestAddAndDelete:
    ROW = {"brand": "TestBrand", "model": "TB-1", "year": "1999",
           "tone": "测试音色", "price": "", "source": "ignored"}

    def test_inserts_a_crawled_row(self, db):
        assert db.add_amp_head(self.ROW, source="wikipedia",
                               source_url="https://example.org/tb1") is True
        rows = [h for h in db.get_amp_heads() if h["brand"] == "TestBrand"]
        assert len(rows) == 1
        assert rows[0]["source"] == "wikipedia"
        assert rows[0]["source_url"] == "https://example.org/tb1"
        assert rows[0]["fetched_at"], "a crawl must record when it happened"

    def test_the_same_amp_is_not_added_twice(self, db):
        assert db.add_amp_head(self.ROW, source="wikipedia") is True
        assert db.add_amp_head(self.ROW, source="wikipedia") is False
        assert len([h for h in db.get_amp_heads() if h["brand"] == "TestBrand"]) == 1

    def test_a_crawl_never_overwrites_a_curated_row(self, db):
        """The hand-written file is authoritative; a crawl must not clobber it."""
        before = db.fetch_data(
            "SELECT tone, source FROM amp_heads WHERE brand='Marshall' AND model='JCM800 2203'"
        )[0]
        assert db.add_amp_head(
            {"brand": "Marshall", "model": "JCM800 2203", "tone": "抓来的错误描述"},
            source="wikipedia", source_url="https://example.org/x",
        ) is False
        after = db.fetch_data(
            "SELECT tone, source FROM amp_heads WHERE brand='Marshall' AND model='JCM800 2203'"
        )[0]
        assert after == before

    def test_incomplete_rows_are_rejected(self, db):
        assert db.add_amp_head({"brand": "", "model": "X"}, source="wikipedia") is False
        assert db.add_amp_head({"brand": "X", "model": ""}, source="wikipedia") is False

    def test_re_seeding_reclaims_a_crawled_row(self, db, monkeypatch):
        """If an amp is later hand-written, it stops being credited to a crawl.

        The real sequence: a crawl adds a model the file doesn't have yet, and
        later someone adds that model to `amp_heads_data.py` by hand. The seed
        must win and re-label the row.
        """
        import amp_heads_data

        # 1. The crawl gets there first, for a model the curated file lacks.
        assert db.add_amp_head(
            {"brand": "Fender", "model": "Bandmaster", "tone": "抓来的旧内容"},
            source="wikipedia", source_url="https://x",
        ) is True
        assert db.count_amp_heads("wikipedia") == 1

        # 2. Someone now writes that same amp into the curated file.
        monkeypatch.setattr(amp_heads_data, "AMP_HEADS", amp_heads_data.AMP_HEADS + (
            ("Fender", "Bandmaster", "1953", "美国", "全电子管", "40W",
             "6L6×2 / 12AX7×3", "手写的新内容", "二手约 1–2 万", "", "手写建议"),
        ))
        db.seed_amp_heads()

        row = db.fetch_data(
            "SELECT source, tone FROM amp_heads WHERE brand='Fender' AND model='Bandmaster'"
        )[0]
        assert row[0] == "manual", "the curated file is authoritative"
        assert row[1] == "手写的新内容"
        assert db.count_amp_heads("wikipedia") == 0

    def test_delete_removes_one_row(self, db):
        db.add_amp_head(self.ROW, source="wikipedia")
        head = [h for h in db.get_amp_heads() if h["brand"] == "TestBrand"][0]
        db.delete_amp_head(head["id"])
        assert not [h for h in db.get_amp_heads() if h["brand"] == "TestBrand"]

    def test_library_lists_manual_rows_first(self, db):
        db.add_amp_head({"brand": "AAA", "model": "First"}, source="wikipedia")
        heads = db.get_amp_heads()
        sources = [h["source"] for h in heads]
        assert sources.index("manual") < sources.index("wikipedia")


class TestAppState:
    def test_round_trips(self, db):
        db.set_state("k", "v")
        assert db.get_state("k") == "v"

    def test_missing_key_returns_the_default(self, db):
        assert db.get_state("nope", "fallback") == "fallback"

    def test_overwrites(self, db):
        db.set_state("k", "1")
        db.set_state("k", "2")
        assert db.get_state("k") == "2"


class TestTitleParsing:
    def test_splits_brand_and_model(self):
        from amp_head_crawler import split_title

        assert split_title("Marshall JCM800") == ("Marshall", "JCM800")
        assert split_title("Fender Twin Reverb") == ("Fender", "Twin Reverb")

    def test_normalises_mesa(self):
        from amp_head_crawler import split_title

        assert split_title("Mesa/Boogie Mark IIC+") == ("Mesa/Boogie", "Mark IIC+")

    def test_brand_only_title_yields_no_model(self):
        from amp_head_crawler import split_title

        assert split_title("Marshall") == ("Marshall", "")
        assert split_title("") == ("", "")


class TestTitleFilter:
    """Real titles observed from Wikipedia, including the ones that fooled v1."""

    KEEP = (
        "Marshall JCM800", "Fender Bandmaster", "Fender Bandmaster Reverb",
        "Fender Blues Junior", "Epiphone Valve Junior", "Danelectro Amp-in-case",
        "Deacy Amp", "Vox AC30", "Mesa/Boogie Mark IIC+", "Danelectro Commando",
    )
    DROP = (
        # A reverb algorithm that slipped through the first version and was
        # actually stored as an amp head — the regression this guards.
        "Dattorro industry scheme",
        "Instrument amplifier", "Bass amplifier", "Boutique amplifier",
        "Fender amplifier", "Marshall Amplification", "Tone stack",
        "Valve amplifier", "List of guitar amplifiers", "Vacuum tube",
        "Marshall", "Guitar amplifier manufacturers",
    )

    def test_real_models_are_kept(self):
        from amp_head_crawler import _looks_like_model

        for title in self.KEEP:
            assert _looks_like_model(title), f"{title} should be treated as a model"

    def test_categories_and_technology_are_dropped(self):
        from amp_head_crawler import _looks_like_model

        for title in self.DROP:
            assert not _looks_like_model(title), f"{title} should be filtered out"

    def test_generic_words_inside_a_model_name_are_fine(self):
        """"Epiphone Valve Junior" must survive a check for 'valve'."""
        from amp_head_crawler import _looks_like_model

        assert _looks_like_model("Epiphone Valve Junior")
        assert not _looks_like_model("Valve amplifier")


class TestCategoryVerification:
    """Manufacturer categories are company-wide and drag in non-amps.

    `Category:Boss Corporation` lists pedals, drum machines and samplers;
    `Category:Vox (company)` lists guitars and organs. Their names look
    exactly like amp models, so the only reliable test is what Wikipedia
    files the article under.
    """

    def _crawler(self, db, pages):
        from amp_head_crawler import AmpHeadCrawler

        crawler = AmpHeadCrawler(db)

        def fake_api(params, retries=3):
            if params.get("prop") == "categories":
                return {"query": {"pages": pages}}
            return None

        monkeypatch_target = crawler
        crawler._api = fake_api  # type: ignore[method-assign]
        return monkeypatch_target

    def test_keeps_articles_filed_as_amplifiers(self, db):
        crawler = self._crawler(db, [{
            "title": "Marshall JCM800",
            "categories": [{"title": "Category:Instrument amplifiers"},
                           {"title": "Category:Marshall amplifiers"}],
        }])
        assert crawler._amplifier_titles(["Marshall JCM800"]) == {"Marshall JCM800"}

    def test_drops_pedals_guitars_and_organs(self, db):
        crawler = self._crawler(db, [
            {"title": "Boss DS-1", "categories": [{"title": "Category:Effects pedals"}]},
            {"title": "Vox Phantom", "categories": [{"title": "Category:Electric guitars"}]},
            {"title": "Vox Continental", "categories": [{"title": "Category:Electronic organs"}]},
            {"title": "Tone Bender", "categories": [{"title": "Category:Effects pedals"}]},
        ])
        assert crawler._amplifier_titles(
            ["Boss DS-1", "Vox Phantom", "Vox Continental", "Tone Bender"]
        ) == set()

    def test_a_manufacturer_category_alone_is_not_enough(self, db):
        """"Category:Guitar amplifier manufacturers" mentions amplifiers too."""
        crawler = self._crawler(db, [{
            "title": "Trace Elliot",
            "categories": [{"title": "Category:Guitar amplifier manufacturers"}],
        }])
        assert crawler._amplifier_titles(["Trace Elliot"]) == set()

    def test_missing_articles_are_dropped(self, db):
        crawler = self._crawler(db, [{"title": "Nothing", "missing": True}])
        assert crawler._amplifier_titles(["Nothing"]) == set()


class TestCandidateFiltering:
    def test_skips_titles_already_in_the_library(self, db, monkeypatch):
        """The curated 55 must not be re-crawled and duplicated."""
        from amp_head_crawler import AmpHeadCrawler

        crawler = AmpHeadCrawler(db)
        monkeypatch.setattr(crawler, "_category_members",
                            lambda cat: ["Marshall JCM800", "Fender Twin", "Some New Amp"])
        monkeypatch.setattr(crawler, "_amplifier_titles", lambda titles: set(titles))
        titles = AmpHeadCrawler.candidates(crawler)
        known = db.get_amp_heads()
        for t in titles:
            brand, model = t.split(" ", 1)
            assert not any(h["brand"] == brand and h["model"] == model for h in known)

    def test_near_duplicates_of_known_amps_are_skipped(self, db, monkeypatch):
        """Wikipedia names things at a different granularity than we do."""
        from amp_head_crawler import AmpHeadCrawler

        crawler = AmpHeadCrawler(db)
        monkeypatch.setattr(crawler, "_category_members", lambda cat: [
            "Marshall JCM800",      # we already have "JCM800 2203"
            "Fender Twin",          # we already have "Twin Reverb"
            "Kemper Profiler",      # we already have "Profiler Head"
            "Fender Super Reverb",  # genuinely new
        ])
        monkeypatch.setattr(crawler, "_amplifier_titles", lambda titles: set(titles))
        titles = AmpHeadCrawler.candidates(crawler)
        assert "Fender Super Reverb" in titles
        assert "Marshall JCM800" not in titles
        assert "Fender Twin" not in titles
        assert "Kemper Profiler" not in titles

    def test_candidates_never_reach_the_network_unmocked(self, db, monkeypatch):
        """Guards the suite itself: these tests must stay offline."""
        from amp_head_crawler import AmpHeadCrawler

        crawler = AmpHeadCrawler(db)
        monkeypatch.setattr(crawler, "_category_members", lambda cat: [])
        calls = []
        monkeypatch.setattr(crawler, "_api", lambda *a, **k: calls.append(1))
        AmpHeadCrawler.candidates(crawler)
        assert not calls


class TestExtraction:
    """The model must copy from the article, never fill gaps from memory."""

    def test_empty_fields_stay_empty(self, db, monkeypatch):
        import amp_head_crawler

        article = "x" * 500
        monkeypatch.setattr(amp_head_crawler, "quick_chat", lambda *a, **k: json.dumps({
            "year": "1981", "origin": "", "kind": "", "power": "", "tubes": "",
            "tone": "文中提到的音色", "price": "", "famous": "", "tip": "",
        }))
        result = amp_head_crawler.AmpHeadCrawler(db).extract("Marshall JCM800", article)
        assert result["year"] == "1981"
        assert result["price"] == "", "an absent price must stay absent, not be guessed"
        assert result["origin"] == ""

    def test_json_wrapped_in_prose_is_still_parsed(self, db, monkeypatch):
        import amp_head_crawler

        monkeypatch.setattr(amp_head_crawler, "quick_chat",
                            lambda *a, **k: '好的：\n```json\n{"year":"1981"}\n```')
        result = amp_head_crawler.AmpHeadCrawler(db).extract("X Y", "y" * 500)
        assert result["year"] == "1981"
        assert set(result) == set(amp_head_crawler.EXTRACT_FIELDS)

    def test_non_json_reply_is_rejected(self, db, monkeypatch):
        import amp_head_crawler

        monkeypatch.setattr(amp_head_crawler, "quick_chat", lambda *a, **k: "我不知道")
        assert amp_head_crawler.AmpHeadCrawler(db).extract("X Y", "y" * 500) is None

    def test_failed_call_is_rejected(self, db, monkeypatch):
        import amp_head_crawler

        monkeypatch.setattr(amp_head_crawler, "quick_chat", lambda *a, **k: None)
        assert amp_head_crawler.AmpHeadCrawler(db).extract("X Y", "y" * 500) is None

    def test_stub_articles_are_not_sent_to_the_model(self, db, monkeypatch):
        """A near-empty page would only invite the model to fill in blanks."""
        import amp_head_crawler

        called = []
        monkeypatch.setattr(amp_head_crawler, "quick_chat",
                            lambda *a, **k: called.append(1))
        assert amp_head_crawler.AmpHeadCrawler(db).extract("X Y", "太短了") is None
        assert not called


class TestCrawlRun:
    def _crawler(self, db, monkeypatch, article="a" * 600):
        import amp_head_crawler

        crawler = amp_head_crawler.AmpHeadCrawler(db)
        monkeypatch.setattr(crawler, "_category_members",
                            lambda cat: ["Brandnew Model One", "Another Fresh Amp"])
        monkeypatch.setattr(crawler, "_amplifier_titles", lambda titles: set(titles))
        monkeypatch.setattr(crawler, "_article_text", lambda t: article)
        monkeypatch.setattr(amp_head_crawler, "quick_chat", lambda *a, **k: json.dumps({
            "year": "2020", "origin": "美国", "kind": "全电子管", "power": "50W",
            "tubes": "6L6×2", "tone": "文中说的音色", "price": "",
            "famous": "", "tip": "文中说的建议",
        }))
        return crawler

    def test_adds_rows_with_their_source_url(self, db, monkeypatch):
        crawler = self._crawler(db, monkeypatch)
        summary = crawler.crawl(limit=2)
        assert summary["added"] == 2
        rows = [h for h in db.get_amp_heads() if h["source"] == "wikipedia"]
        assert len(rows) == 2
        assert all(h["source_url"].startswith("https://en.wikipedia.org/wiki/") for h in rows)

    def test_respects_the_per_run_limit(self, db, monkeypatch):
        crawler = self._crawler(db, monkeypatch)
        assert crawler.crawl(limit=1)["added"] == 1

    def test_a_model_with_no_substance_is_skipped(self, db, monkeypatch):
        """Without tone or year the card would be empty — don't store it."""
        import amp_head_crawler

        crawler = self._crawler(db, monkeypatch)
        monkeypatch.setattr(amp_head_crawler, "quick_chat",
                            lambda *a, **k: json.dumps({"tone": "", "year": ""}))
        assert crawler.crawl(limit=2)["added"] == 0

    def test_crawl_survives_a_broken_article(self, db, monkeypatch):
        crawler = self._crawler(db, monkeypatch)

        def boom(title):
            raise RuntimeError("network died")

        monkeypatch.setattr(crawler, "_article_text", boom)
        summary = crawler.crawl(limit=2)
        assert summary["added"] == 0, "a crash must not take the scheduler down"

    def test_crawl_survives_a_broken_candidate_lookup(self, db, monkeypatch):
        import amp_head_crawler

        crawler = amp_head_crawler.AmpHeadCrawler(db)

        def boom(cat):
            raise RuntimeError("wikipedia down")

        monkeypatch.setattr(crawler, "_category_members", boom)
        assert crawler.crawl()["added"] == 0


class TestWeeklyGuard:
    def test_runs_when_never_run(self, db, monkeypatch):
        import amp_head_crawler

        calls = []
        monkeypatch.setattr(amp_head_crawler.AmpHeadCrawler, "crawl",
                            lambda self, limit=None: calls.append(1) or {"added": 0})
        assert amp_head_crawler.run_weekly(db) is not None
        assert calls

    def test_does_not_run_again_within_the_window(self, db, monkeypatch):
        import amp_head_crawler

        db.set_state("amp_crawl_last", __import__("datetime").datetime.now()
                     .strftime("%Y-%m-%d %H:%M:%S"))
        calls = []
        monkeypatch.setattr(amp_head_crawler.AmpHeadCrawler, "crawl",
                            lambda self, limit=None: calls.append(1) or {"added": 0})
        assert amp_head_crawler.run_weekly(db) is None
        assert not calls

    def test_runs_again_after_the_window(self, db, monkeypatch):
        import amp_head_crawler
        from datetime import datetime, timedelta

        old = (datetime.now() - timedelta(days=8)).strftime("%Y-%m-%d %H:%M:%S")
        db.set_state("amp_crawl_last", old)
        monkeypatch.setattr(amp_head_crawler.AmpHeadCrawler, "crawl",
                            lambda self, limit=None: {"added": 0})
        assert amp_head_crawler.run_weekly(db) is not None

    def test_a_garbled_timestamp_does_not_block_forever(self, db, monkeypatch):
        import amp_head_crawler

        db.set_state("amp_crawl_last", "not a date")
        monkeypatch.setattr(amp_head_crawler.AmpHeadCrawler, "crawl",
                            lambda self, limit=None: {"added": 0})
        assert amp_head_crawler.run_weekly(db) is not None


class TestPushAttribution:
    class _Stub:
        def __init__(self, db):
            self.db = db

    def _render(self, db):
        from scheduler import BotScheduler

        return BotScheduler._build_amp_head(self._Stub(db))

    def test_manual_entry_omits_a_source_block(self, db, monkeypatch):
        monkeypatch.setattr(db, "get_amp_head_of_the_day", lambda: {
            "brand": "Marshall", "model": "JCM800", "year": "1981", "origin": "英国",
            "kind": "全电子管", "power": "100W", "tubes": "", "tone": "经典",
            "price": "二手约 1.2–2 万", "famous": "", "tip": "买它",
            "source": "manual", "source_url": "", "fetched_at": "",
        })
        text = self._render(db)
        assert "💰 市场价格：" in text
        assert "仅供参考" in text
        assert "维基百科" not in text

    def test_crawled_entry_links_its_source(self, db, monkeypatch):
        monkeypatch.setattr(db, "get_amp_head_of_the_day", lambda: {
            "brand": "Fender", "model": "Bandmaster", "year": "1953", "origin": "美国",
            "kind": "", "power": "", "tubes": "", "tone": "文中说的",
            "price": "", "famous": "", "tip": "",
            "source": "wikipedia",
            "source_url": "https://en.wikipedia.org/wiki/Fender_Bandmaster",
            "fetched_at": "2026-09-12 11:00:00",
        })
        text = self._render(db)
        assert "https://en.wikipedia.org/wiki/Fender_Bandmaster" in text
        assert "维基百科" in text
        assert "自动抽取" in text

    def test_a_crawled_entry_without_a_price_omits_the_line(self, db, monkeypatch):
        """Guessing a price would defeat the point of the whole static library."""
        monkeypatch.setattr(db, "get_amp_head_of_the_day", lambda: {
            "brand": "Vox", "model": "AC50", "year": "1964", "origin": "英国",
            "kind": "", "power": "", "tubes": "", "tone": "经典",
            "price": "", "famous": "", "tip": "",
            "source": "wikipedia", "source_url": "https://x", "fetched_at": "",
        })
        assert "💰" not in self._render(db)
