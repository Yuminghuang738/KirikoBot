"""Daily amp-head recommendation (功能申请：每日箱头推荐).

The facts in this feature come from a curated table rather than from the
model, so these tests care about two things: that the dataset stays sane and
seeds idempotently, and that the push actually says what was asked for
(诞生时间 / 音色特点 / 市场价格 / 使用推荐).
"""
from __future__ import annotations


class TestDataset:
    def test_columns_match_every_row(self):
        from amp_heads_data import AMP_HEADS, COLUMNS

        assert len(COLUMNS) == 11
        for row in AMP_HEADS:
            assert len(row) == len(COLUMNS), f"{row[0]} {row[1]} has {len(row)} fields"

    def test_no_duplicate_brand_and_model(self):
        from amp_heads_data import AMP_HEADS

        keys = [(r[0], r[1]) for r in AMP_HEADS]
        assert len(keys) == len(set(keys)), "seeding keys on (brand, model)"

    def test_the_requested_details_are_present_for_every_entry(self):
        """The requester asked for 年代/音色/价格/推荐 specifically."""
        from amp_heads_data import AMP_HEADS, COLUMNS

        idx = {c: i for i, c in enumerate(COLUMNS)}
        for row in AMP_HEADS:
            label = f"{row[0]} {row[1]}"
            for field in ("year", "origin", "tone", "price", "tip"):
                assert row[idx[field]].strip(), f"{label} is missing {field}"

    def test_library_is_big_enough_to_rotate_for_a_while(self):
        from amp_heads_data import AMP_HEADS

        assert len(AMP_HEADS) >= 40


class TestSeeding:
    def test_rows_are_loaded_on_first_use(self, db):
        from amp_heads_data import AMP_HEADS

        assert db.count_amp_heads() == len(AMP_HEADS)

    def test_seeding_again_updates_instead_of_duplicating(self, db):
        before = db.count_amp_heads()
        db.seed_amp_heads()
        db.seed_amp_heads()
        assert db.count_amp_heads() == before

    def test_edits_to_the_dataset_are_picked_up(self, db):
        """The dataset is the source of truth: fix a price, restart, done."""
        db.execute_action(
            "UPDATE amp_heads SET price='改过的价格' WHERE brand='Marshall' AND model='JCM800 2203'"
        )
        db.seed_amp_heads()
        row = db.fetch_data(
            "SELECT price FROM amp_heads WHERE brand='Marshall' AND model='JCM800 2203'"
        )
        assert row[0][0] != "改过的价格", "re-seeding must overwrite the stale value"
        assert row[0][0], "the real price must be restored, not blanked"


class TestHeadOfTheDay:
    def test_returns_a_full_record(self, db):
        from amp_heads_data import COLUMNS

        head = db.get_amp_head_of_the_day()
        assert head is not None
        assert set(head) == set(COLUMNS)
        assert head["brand"] and head["model"]

    def test_same_day_is_stable_within_a_day(self, db):
        assert db.get_amp_head_of_the_day() == db.get_amp_head_of_the_day()

    def test_every_offset_selects_a_distinct_entry(self, db):
        """Pure randomness repeats back-to-back; a daily push would look broken.

        The rotation is an OFFSET over an ORDER BY id scan, so the mechanism
        to guard is that every offset in a cycle lands on a different row —
        regardless of whether ids happen to be contiguous.
        """
        total = db.count_amp_heads()
        seen = set()
        for offset in range(total):
            row = db.fetch_data(
                "SELECT id FROM amp_heads ORDER BY id LIMIT 1 OFFSET ?", (offset,)
            )
            assert row, f"offset {offset} returned nothing"
            seen.add(row[0][0])
        assert len(seen) == total, "a full cycle must hit every entry exactly once"

    def test_offset_is_driven_by_the_day_of_year(self, db):
        """With a counter but no clock the push would send the same amp forever."""
        import ast
        import inspect
        import textwrap

        from database_manager import DatabaseManager

        src = textwrap.dedent(inspect.getsource(DatabaseManager.get_amp_head_of_the_day))
        fn = ast.parse(src).body[0]
        # Only the executable string literals — the docstring deliberately
        # mentions RANDOM() while explaining why it is not used.
        stmts = [
            st for st in fn.body
            if not (isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant))
        ]
        sql = " ".join(
            n.value for st in stmts for n in ast.walk(st)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
        )
        assert "%j" in sql, "day-of-year must drive the rotation"
        assert "RANDOM()" not in sql, "random pick repeats back-to-back too easily"

    def test_empty_library_is_handled(self, db):
        db.execute_action("DELETE FROM amp_heads")
        assert db.get_amp_head_of_the_day() is None
        assert db.count_amp_heads() == 0


class TestPushMessage:
    class _Stub:
        def __init__(self, db):
            self.db = db

    def _render(self, db):
        from scheduler import BotScheduler

        return BotScheduler._build_amp_head(self._Stub(db))

    def test_includes_every_field_the_requester_asked_for(self, db):
        text = self._render(db)
        assert "🎸 今日箱头" in text
        assert "📅 诞生：" in text
        assert "🎵 音色特点：" in text
        assert "💰 市场价格：" in text
        assert "⭐ 使用推荐：" in text

    def test_shows_the_real_brand_and_model(self, db):
        head = db.get_amp_head_of_the_day()
        text = self._render(db)
        assert f"{head['brand']} {head['model']}" in text

    def test_prices_are_labelled_as_approximate(self, db):
        """Street prices drift, so the push must not present them as exact."""
        assert "仅供参考" in self._render(db)

    def test_empty_library_yields_no_message(self, db):
        """"" means the push is skipped rather than sending a broken card."""
        db.execute_action("DELETE FROM amp_heads")
        assert self._render(db) == ""


class TestWiring:
    def test_scheduler_knows_the_topic(self):
        import ast
        import os

        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "KirikoBot", "scheduler.py",
        )
        with open(path, encoding="utf-8") as fh:
            source = fh.read()
        assert 'topic == "amp_head"' in source

    def test_push_page_offers_the_topic_at_8am(self):
        """The request asked for 早上八点, so the default must not be 07:00."""
        import os

        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "KirikoBot", "static", "js", "app.js",
        )
        with open(path, encoding="utf-8") as fh:
            js = fh.read()
        assert "amp_head:'🎸 今日箱头'" in js
        assert "amp_head:'08:00'" in js

    def test_topic_is_accepted_by_the_subscription_api(self):
        """The POST route rejects unknown topics, so it must be allow-listed."""
        from database_manager import DatabaseManager

        assert "amp_head" in DatabaseManager.SUBSCRIPTION_TOPICS

    def test_saving_a_subscription_round_trips(self, db):
        db.set_subscription("g1", "amp_head", push_time="08:00", enabled=True)
        subs = {s["topic"]: s for s in db.get_subscriptions("g1")}
        assert subs["amp_head"]["push_time"] == "08:00"
        assert subs["amp_head"]["enabled"]

    def test_unknown_topics_are_still_rejected(self, db):
        import pytest

        with pytest.raises(ValueError):
            db.set_subscription("g1", "not_a_topic", push_time="08:00", enabled=True)
