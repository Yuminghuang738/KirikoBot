"""AI usage instrumentation: usage parsing, recording, aggregation and cost."""
from __future__ import annotations

import pytest

from ai_metrics import extract_usage, record, set_sink, timed


@pytest.fixture(autouse=True)
def _clean_sink():
    """Never leak a sink between tests."""
    yield
    set_sink(None)


class TestExtractUsage:
    def test_reads_the_deepseek_shape(self):
        data = {"usage": {
            "prompt_tokens": 100, "completion_tokens": 40,
            "prompt_cache_hit_tokens": 80, "prompt_cache_miss_tokens": 20,
            "completion_tokens_details": {"reasoning_tokens": 25},
        }}
        assert extract_usage(data) == {
            "prompt_tokens": 100, "completion_tokens": 40, "reasoning_tokens": 25,
            "cache_hit_tokens": 80, "cache_miss_tokens": 20,
        }

    def test_falls_back_to_cached_tokens(self):
        """Older payloads only carry prompt_tokens_details.cached_tokens."""
        data = {"usage": {"prompt_tokens": 100, "completion_tokens": 5,
                          "prompt_tokens_details": {"cached_tokens": 70}}}
        usage = extract_usage(data)
        assert usage["cache_hit_tokens"] == 70
        assert usage["cache_miss_tokens"] == 30

    @pytest.mark.parametrize("data", [None, {}, {"usage": None}])
    def test_missing_usage_is_all_zero(self, data):
        assert all(v == 0 for v in extract_usage(data).values())


class TestRecord:
    def test_does_nothing_without_a_sink(self):
        record(source="chat")   # must not raise

    def test_forwards_a_normalised_entry(self):
        seen = []
        set_sink(seen.append)
        record(source="judge", kind="quick", latency_ms=123.7,
               usage={"usage": {"prompt_tokens": 10, "completion_tokens": 2}},
               success=False, error="boom")
        assert len(seen) == 1
        entry = seen[0]
        assert entry["source"] == "judge"
        assert entry["latency_ms"] == 123
        assert entry["prompt_tokens"] == 10
        assert entry["success"] == 0
        assert entry["error"] == "boom"
        assert 0 <= entry["utc_hour"] <= 23

    def test_a_failing_sink_never_propagates(self):
        def boom(_entry):
            raise RuntimeError("sink down")

        set_sink(boom)
        record(source="chat")   # metrics must never break a reply

    def test_error_text_is_truncated(self):
        seen = []
        set_sink(seen.append)
        record(source="chat", error="x" * 5000)
        assert len(seen[0]["error"]) <= 200


class TestTimed:
    def test_records_a_successful_call(self):
        seen = []
        set_sink(seen.append)
        with timed(source="profile") as t:
            t.usage = {"usage": {"prompt_tokens": 7, "completion_tokens": 3}}
        assert seen[0]["source"] == "profile"
        assert seen[0]["success"] == 1
        assert seen[0]["prompt_tokens"] == 7

    def test_records_and_reraises_failures(self):
        seen = []
        set_sink(seen.append)
        with pytest.raises(ValueError):
            with timed(source="news") as t:
                raise ValueError("nope")
        assert seen[0]["success"] == 0
        assert "nope" in seen[0]["error"]


class TestCostModel:
    def test_peak_is_double_off_peak(self, db):
        peak = db.estimate_cost_usd(cache_hit=1000, cache_miss=1000, output=1000,
                                    utc_hour=2, utc_weekday=1)
        off = db.estimate_cost_usd(cache_hit=1000, cache_miss=1000, output=1000,
                                   utc_hour=20, utc_weekday=1)
        assert peak == pytest.approx(off * 2)

    def test_weekends_are_always_off_peak(self, db):
        sat = db.estimate_cost_usd(cache_hit=0, cache_miss=1000, output=1000,
                                   utc_hour=2, utc_weekday=5)
        off = db.estimate_cost_usd(cache_hit=0, cache_miss=1000, output=1000,
                                   utc_hour=20, utc_weekday=2)
        assert sat == pytest.approx(off)

    def test_known_price_point(self, db):
        """1M cache-miss input at off-peak = $0.15 (half of the $0.30 peak rate)."""
        cost = db.estimate_cost_usd(cache_hit=0, cache_miss=1_000_000, output=0,
                                    utc_hour=23, utc_weekday=2)
        assert cost == pytest.approx(0.15)

    def test_garbage_hours_do_not_crash(self, db):
        assert db.estimate_cost_usd(cache_hit=1, cache_miss=1, output=1,
                                    utc_hour=None, utc_weekday=None) > 0


class TestAiMetricsQuery:
    def _call(self, db, **over):
        entry = dict(source="chat", kind="chat", model="deepseek-flash",
                     group_id="g1", prompt_tokens=100, completion_tokens=20,
                     reasoning_tokens=10, cache_hit_tokens=50, cache_miss_tokens=50,
                     latency_ms=500, success=1, error="", utc_hour=20, utc_weekday=2)
        entry.update(over)
        db.record_ai_call(entry)

    def test_empty_state(self, db):
        m = db.get_ai_metrics(24)
        assert m["totals"]["calls"] == 0
        assert m["totals"]["success_rate"] == 100.0
        assert m["by_source"] == []
        assert m["recent_errors"] == []
        assert len(m["hourly"]) == 24

    def test_aggregates_totals_and_latency(self, db):
        for latency in (100, 200, 300, 4000):
            self._call(db, latency_ms=latency)
        m = db.get_ai_metrics(24)
        assert m["totals"]["calls"] == 4
        assert m["totals"]["tokens"] == 480
        assert m["latency"]["avg_ms"] == 1150   # (100+200+300+4000)/4
        assert m["latency"]["max_ms"] == 4000
        assert m["latency"]["p50_ms"] <= m["latency"]["p95_ms"]

    def test_failures_are_counted_and_listed(self, db):
        self._call(db)
        self._call(db, success=0, error="ReadTimeout", source="vision")
        m = db.get_ai_metrics(24)
        assert m["totals"]["failed"] == 1
        assert m["totals"]["success_rate"] == 50.0
        assert m["recent_errors"][0]["source"] == "vision"
        # failed calls must not drag the latency percentiles down
        assert m["latency"]["max_ms"] == 500

    def test_by_source_breakdown(self, db):
        self._call(db, source="chat")
        self._call(db, source="chat")
        self._call(db, source="judge", prompt_tokens=10, completion_tokens=5)
        m = db.get_ai_metrics(24)
        by = {b["source"]: b for b in m["by_source"]}
        assert by["chat"]["calls"] == 2
        assert by["judge"]["calls"] == 1
        assert by["chat"]["avg_ms"] == 500

    def test_hourly_buckets_use_local_time(self, db):
        self._call(db)
        m = db.get_ai_metrics(24)
        assert sum(h["calls"] for h in m["hourly"]) == 1

    def test_window_excludes_old_calls(self, db):
        db.execute_action(
            "INSERT INTO ai_calls (source, latency_ms, success, timestamp) "
            "VALUES ('chat', 100, 1, datetime('now','localtime','-3 days'))"
        )
        self._call(db)
        assert db.get_ai_metrics(24)["totals"]["calls"] == 1
        assert db.get_ai_metrics(24 * 7)["totals"]["calls"] == 2

    def test_hours_are_clamped(self, db):
        assert db.get_ai_metrics(0)["hours"] == 1
        assert db.get_ai_metrics(99999)["hours"] == 24 * 30


class TestRetentionCoversAiCalls:
    def test_ai_calls_are_pruned(self, db):
        from maintenance_service import prune_old_data

        db.execute_action(
            "INSERT INTO ai_calls (source, timestamp) "
            "VALUES ('chat', datetime('now','localtime','-400 days'))"
        )
        db.execute_action(
            "INSERT INTO ai_calls (source, timestamp) VALUES ('chat', datetime('now','localtime'))"
        )
        prune_old_data(db, days=180)
        assert db.fetch_data("SELECT COUNT(*) FROM ai_calls")[0][0] == 1
