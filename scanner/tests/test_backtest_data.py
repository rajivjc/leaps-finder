"""Backtest data-layer tests (SPEC-BACKTEST.md §2.4, §3, §10.1). No network."""

import random
from datetime import date

import pandas as pd
import pytest

from leaps_scanner import prices
from leaps_scanner.backtest import data
from leaps_scanner.backtest.membership import Membership, MembershipSpan

COLUMNS = [*data.PRICE_COLUMNS, data.DIVIDEND_COLUMN]


def frame(symbols, sessions=5, start="2020-01-06", with_dividends=True):
    """A response shaped the way yfinance returns a dated, actions=True batch."""
    index = pd.bdate_range(start=start, periods=sessions)
    columns = COLUMNS if with_dividends else data.PRICE_COLUMNS
    if len(symbols) == 1:
        return pd.DataFrame({column: [1.0] * sessions for column in columns}, index=index)

    multi = pd.MultiIndex.from_product([symbols, columns])
    return pd.DataFrame([[1.0] * len(multi)] * sessions, index=index, columns=multi)


def no_throttle():
    return prices.Throttle(rate=1e9, sleeper=lambda _: None, clock=lambda: 0.0)


class TestWindow:
    """§3.1's pinned window arithmetic."""

    def test_a_sunday_run_ends_at_the_friday_just_past(self):
        assert data.last_completed_friday(date(2026, 8, 16)) == date(2026, 8, 14)

    def test_a_friday_run_does_not_use_the_week_still_trading(self):
        # No-repaint: Friday's own week is still open when the run happens.
        assert data.last_completed_friday(date(2026, 8, 14)) == date(2026, 8, 7)

    def test_a_saturday_run_uses_the_week_that_just_closed(self):
        assert data.last_completed_friday(date(2026, 8, 15)) == date(2026, 8, 14)

    def test_a_midweek_run_walks_back_to_the_last_friday(self):
        assert data.last_completed_friday(date(2026, 8, 19)) == date(2026, 8, 14)

    def test_the_window_is_ten_years_long(self):
        window = data.evaluation_window(date(2026, 8, 16))

        assert window.end == date(2026, 8, 14)
        assert window.start == date(2016, 8, 14)

    def test_the_warm_up_is_exactly_550_days(self):
        window = data.evaluation_window(date(2026, 8, 16))

        assert (window.start - window.fetch_start).days == 550
        assert window.fetch_start == date(2015, 2, 11)

    def test_an_early_march_window_end_is_unremarkable(self):
        window = data.evaluation_window(date(2024, 3, 2))

        assert window.end == date(2024, 3, 1)
        assert window.start == date(2014, 3, 1)

    def test_a_leap_day_window_end_steps_back_to_the_28th(self):
        # Saturday 2008-03-01 closes the week ending Friday 2008-02-29, and 1998
        # is not a leap year — the one case where the ten-year subtraction has
        # no same-date answer. It must land on 02-28, not roll into March.
        window = data.evaluation_window(date(2008, 3, 1))

        assert window.end == date(2008, 2, 29)
        assert window.start == date(1998, 2, 28)
        assert (window.start - window.fetch_start).days == 550

    def test_week_endings_are_every_friday_in_the_range(self):
        fridays = data.week_endings(date(2020, 1, 1), date(2020, 1, 31))

        assert fridays[0] == date(2020, 1, 3)
        assert fridays[-1] == date(2020, 1, 31)
        assert len(fridays) == 5

    def test_a_session_belongs_to_the_friday_that_ends_its_week(self):
        assert data.week_ending(date(2020, 1, 6)) == date(2020, 1, 10)  # Monday
        assert data.week_ending(date(2020, 1, 10)) == date(2020, 1, 10)  # Friday itself


class TestSplitDatedFrame:
    """§3.3: the v1 splitter would drop the columns this fetch exists for."""

    def test_adj_close_survives_the_split(self):
        split = data.split_dated_frame(frame(["AAA", "BBB"]), ["AAA", "BBB"])

        assert data.ADJ_CLOSE in split["AAA"].columns

    def test_the_v1_splitter_would_have_dropped_it(self):
        # Guards the actual regression: reusing `prices.split_batch_frame`
        # verbatim silently loses Adj Close, and §6.3's benchmarks need it.
        split = prices.split_batch_frame(frame(["AAA", "BBB"]), ["AAA", "BBB"])

        assert data.ADJ_CLOSE not in split["AAA"].columns

    def test_dividends_survive_the_split(self):
        split = data.split_dated_frame(frame(["AAA"]), ["AAA"])

        assert data.DIVIDEND_COLUMN in split["AAA"].columns

    def test_a_symbol_that_never_paid_a_dividend_is_still_usable(self):
        without = frame(["AAA", "BBB"], with_dividends=False)

        split = data.split_dated_frame(without, ["AAA", "BBB"])

        assert set(split) == {"AAA", "BBB"}
        assert data.DIVIDEND_COLUMN not in split["AAA"].columns

    def test_a_symbol_missing_adj_close_is_dropped_not_half_kept(self):
        partial = frame(["AAA"], with_dividends=False).drop(columns=[data.ADJ_CLOSE])

        assert data.split_dated_frame(partial, ["AAA"]) == {}

    def test_an_empty_response_yields_nothing(self):
        assert data.split_dated_frame(pd.DataFrame(), ["AAA"]) == {}


class TestFillCache:
    def window(self):
        return data.Window(
            start=date(2020, 1, 6), end=date(2020, 1, 10), fetch_start=date(2019, 1, 6)
        )

    def fill(self, cache, symbols, downloader, **kwargs):
        return data.fill_cache(
            symbols,
            self.window(),
            cache,
            run_date=date(2020, 1, 12),
            downloader=downloader,
            throttle=no_throttle(),
            sleeper=lambda _: None,
            rng=random.Random(0),
            **kwargs,
        )

    def test_prices_and_dividends_land_in_the_cache(self, tmp_path):
        cache = data.PriceCache(tmp_path)
        paid = frame(["AAA"])
        paid[data.DIVIDEND_COLUMN] = [0.0, 0.25, 0.0, 0.0, 0.0]

        self.fill(cache, ["AAA"], lambda symbols, request: paid)

        assert data.ADJ_CLOSE in cache.load("AAA").columns
        assert len(cache.load_dividends("AAA")) == 1

    def test_the_fetch_asks_for_the_whole_warm_up_range(self, tmp_path):
        seen = []

        def downloader(symbols, request):
            seen.append(request)
            return frame(list(symbols))

        self.fill(data.PriceCache(tmp_path), ["AAA"], downloader)

        assert seen[0].start == date(2019, 1, 6)
        assert seen[0].end == date(2020, 1, 10)

    def test_an_empty_response_is_retried_then_recorded_as_no_data(self, tmp_path):
        # yfinance signals failure by returning an empty frame, not by raising.
        attempts = []

        def downloader(symbols, request):
            attempts.append(list(symbols))
            return pd.DataFrame()

        cache = data.PriceCache(tmp_path)
        summary = self.fill(cache, ["AAA"], downloader)

        assert attempts.count(["AAA"]) == prices.MAX_RETRIES
        assert summary.failed == ("AAA",)
        assert cache.load("AAA") is None
        # Recorded as attempted-and-empty, so §2.5 can tell it apart from
        # never-requested.
        assert cache.read_manifest()["symbols"]["AAA"]["rows"] == 0

    def test_an_empty_response_never_looks_like_a_successful_fetch(self, tmp_path):
        cache = data.PriceCache(tmp_path)

        summary = self.fill(cache, ["AAA"], lambda symbols, request: pd.DataFrame())

        assert summary.fetched == ()

    def test_a_transient_failure_is_retried_and_then_succeeds(self, tmp_path):
        attempts = []

        def downloader(symbols, request):
            attempts.append(1)
            if len(attempts) == 1:
                raise ConnectionError("rate limited")
            return frame(list(symbols))

        summary = self.fill(data.PriceCache(tmp_path), ["AAA"], downloader)

        assert summary.fetched == ("AAA",)

    def test_one_dead_symbol_does_not_take_the_rest_of_the_universe_with_it(self, tmp_path):
        def downloader(symbols, request):
            if "BAD" in symbols:
                return pd.DataFrame()
            return frame(list(symbols))

        summary = self.fill(
            data.PriceCache(tmp_path), ["AAA", "BAD", "CCC"], downloader, batch_size=1
        )

        assert summary.fetched == ("AAA", "CCC")
        assert summary.failed == ("BAD",)

    def test_a_cached_symbol_is_not_fetched_again(self, tmp_path):
        calls = []

        def downloader(symbols, request):
            calls.append(list(symbols))
            return frame(list(symbols))

        cache = data.PriceCache(tmp_path)
        self.fill(cache, ["AAA"], downloader)
        summary = self.fill(cache, ["AAA"], downloader)

        assert calls.count(["AAA"]) == 1
        assert summary.reused == ("AAA",)

    def test_an_interrupted_fetch_keeps_the_batches_that_already_landed(self, tmp_path):
        # The cold fetch runs for half an hour; a crash partway through must not
        # cost every batch that already succeeded.
        def downloader(symbols, request):
            if "BOOM" in symbols:
                raise KeyboardInterrupt("user gave up")
            return frame(list(symbols))

        cache = data.PriceCache(tmp_path)
        with pytest.raises(KeyboardInterrupt):
            self.fill(cache, ["AAA", "BOOM"], downloader, batch_size=1)

        assert cache.load("AAA") is not None
        assert cache.read_manifest()["symbols"]["AAA"]["rows"] == 5

    def test_an_empty_result_is_asked_again_on_the_next_run(self, tmp_path):
        # yfinance reports rate limiting by returning nothing, so a symbol
        # recorded with no rows must not be trusted as "delisted" — otherwise a
        # transient outage becomes a permanent hole that §2.4's coverage ratio
        # blames on survivorship.
        healthy = [False]

        def downloader(symbols, request):
            return frame(list(symbols)) if healthy[0] else pd.DataFrame()

        cache = data.PriceCache(tmp_path)
        self.fill(cache, ["AAA"], downloader)
        assert cache.load("AAA") is None

        healthy[0] = True
        summary = self.fill(cache, ["AAA"], downloader)

        assert summary.fetched == ("AAA",)
        assert cache.load("AAA") is not None
        assert cache.read_manifest()["failed"] == []

    def test_a_symbol_that_recovers_stops_being_listed_as_failed(self, tmp_path):
        # Yahoo goes quiet for a name and later returns it; the cache must not
        # remember it as dead once it has data.
        attempts = []

        def downloader(symbols, request):
            attempts.append(list(symbols))
            if "AAA" in symbols and attempts.count(["AAA"]) <= prices.MAX_RETRIES:
                return pd.DataFrame()
            return frame(list(symbols))

        cache = data.PriceCache(tmp_path)
        self.fill(cache, ["AAA"], downloader)
        assert cache.read_manifest()["failed"] == ["AAA"]

        self.fill(cache, ["AAA"], downloader, refresh=True)

        assert cache.read_manifest()["failed"] == []

    def test_refresh_re_fetches_everything(self, tmp_path):
        calls = []

        def downloader(symbols, request):
            calls.append(list(symbols))
            return frame(list(symbols))

        cache = data.PriceCache(tmp_path)
        self.fill(cache, ["AAA"], downloader)
        self.fill(cache, ["AAA"], downloader, refresh=True)

        assert calls.count(["AAA"]) == 2

    def test_the_rate_series_is_cached_alongside(self, tmp_path):
        cache = data.PriceCache(tmp_path)

        summary = self.fill(cache, ["AAA"], lambda symbols, request: frame(list(symbols)))

        assert summary.rates_ok
        assert cache.load_rates() is not None

    def test_a_missing_rate_series_is_reported_not_fatal(self, tmp_path):
        def downloader(symbols, request):
            if data.RATE_SYMBOL in symbols:
                return pd.DataFrame()
            return frame(list(symbols))

        summary = self.fill(data.PriceCache(tmp_path), ["AAA"], downloader)

        assert summary.fetched == ("AAA",)
        assert not summary.rates_ok


class TestWeeklyRerun:
    """A run a week later must top the cache up, not rebuild it (§3.4)."""

    def fill(self, cache, symbols, downloader, run_date, window, **kwargs):
        return data.fill_cache(
            symbols,
            window,
            cache,
            run_date=run_date,
            downloader=downloader,
            throttle=no_throttle(),
            sleeper=lambda _: None,
            rng=random.Random(0),
            **kwargs,
        )

    def recorder(self):
        asked = []

        def downloader(symbols, request):
            asked.append((tuple(symbols), request.start, request.end))
            sessions = len(pd.bdate_range(start=request.start, end=request.end))
            return frame(list(symbols), sessions=sessions, start=request.start.isoformat())

        return downloader, asked

    def test_only_the_missing_tail_is_fetched_when_the_window_advances(self, tmp_path):
        # The cold fetch is 30-45 minutes; five new sessions must not cost it
        # again. Without this the cache is useless outside a single week.
        downloader, asked = self.recorder()
        cache = data.PriceCache(tmp_path)
        first = data.evaluation_window(date(2026, 8, 16))
        second = data.evaluation_window(date(2026, 8, 23))

        self.fill(cache, ["AAA", "BBB"], downloader, date(2026, 8, 16), first)
        summary = self.fill(cache, ["AAA", "BBB"], downloader, date(2026, 8, 23), second)

        assert summary.fetched == ()  # nothing needed the full 11.5-year range
        assert summary.topped_up == ("AAA", "BBB")
        tail = [call for call in asked if call[0] == ("AAA", "BBB")][-1]
        assert tail[1] >= first.end  # starts at the cached edge, not in 2015
        assert tail[2] == second.end

    def test_a_top_up_extends_the_cached_series_rather_than_replacing_it(self, tmp_path):
        downloader, _ = self.recorder()
        cache = data.PriceCache(tmp_path)
        first = data.evaluation_window(date(2026, 8, 16))
        second = data.evaluation_window(date(2026, 8, 23))

        self.fill(cache, ["AAA"], downloader, date(2026, 8, 16), first)
        before = cache.load("AAA")
        self.fill(cache, ["AAA"], downloader, date(2026, 8, 23), second)
        after = cache.load("AAA")

        assert after.index[0] == before.index[0]  # the warm-up history survived
        assert after.index[-1] > before.index[-1]
        assert after.index.is_unique  # overlapping sessions resolved, not duplicated
        assert after.index.is_monotonic_increasing

    def test_an_unchanged_window_reuses_everything(self, tmp_path):
        downloader, asked = self.recorder()
        cache = data.PriceCache(tmp_path)
        window = data.evaluation_window(date(2026, 8, 16))

        self.fill(cache, ["AAA"], downloader, date(2026, 8, 16), window)
        summary = self.fill(cache, ["AAA"], downloader, date(2026, 8, 16), window)

        assert summary.reused == ("AAA",)
        assert [call for call in asked if call[0] == ("AAA",)] == asked[:1]

    def test_a_symbol_that_stopped_trading_is_not_re_asked_every_run(self, tmp_path):
        # A name delisted mid-window has no tail to give. Recording that we asked
        # over the new range stops it being chased again next week, while a
        # symbol with no rows at all is still retried (that is the ambiguous one).
        cache = data.PriceCache(tmp_path)
        first = data.evaluation_window(date(2026, 8, 16))
        second = data.evaluation_window(date(2026, 8, 23))

        def downloader(symbols, request):
            if request.start >= first.end:  # the tail request: nothing left
                return pd.DataFrame()
            sessions = len(pd.bdate_range(start=request.start, end=request.end))
            return frame(list(symbols), sessions=sessions, start=request.start.isoformat())

        self.fill(cache, ["AAA"], downloader, date(2026, 8, 16), first)
        rows = len(cache.load("AAA"))
        self.fill(cache, ["AAA"], downloader, date(2026, 8, 23), second)

        assert cache.read_manifest()["failed"] == []  # an empty tail is not a failure
        assert len(cache.load("AAA")) == rows  # and it did not lose its history
        summary = self.fill(cache, ["AAA"], downloader, date(2026, 8, 23), second)
        assert summary.reused == ("AAA",)


class TestCoverage:
    """§2.4's accounting, and the floor that fails a run rather than fake it."""

    window = data.Window(
        start=date(2020, 1, 6), end=date(2020, 1, 31), fetch_start=date(2019, 1, 6)
    )

    def seed(self, tmp_path, symbols_with_data, sessions=20):
        cache = data.PriceCache(tmp_path)
        for symbol in symbols_with_data:
            cache.store(symbol, frame([symbol], sessions=sessions, start="2020-01-06"))
        cache.write_manifest({"schema": data.CACHE_SCHEMA, "snapshot_date": "2020-02-01"})
        return cache

    def membership(self, symbols):
        return Membership(
            spans=tuple(MembershipSpan(symbol, date(2019, 1, 1), None) for symbol in symbols)
        )

    def test_full_coverage_reports_a_ratio_of_one(self, tmp_path):
        cache = self.seed(tmp_path, ["AAA", "BBB"])

        coverage = data.compute_coverage(
            self.membership(["AAA", "BBB"]), cache, self.window, run_date=date(2020, 2, 1)
        )

        assert coverage.ratio == 1.0
        assert coverage.status == "ok"
        assert coverage.uncovered == ()

    def test_a_member_with_no_data_is_counted_and_listed_not_dropped(self, tmp_path):
        cache = self.seed(tmp_path, ["AAA"])

        coverage = data.compute_coverage(
            self.membership(["AAA", "GONE"]), cache, self.window, run_date=date(2020, 2, 1)
        )

        assert coverage.members == 2  # the delisted name stays in the denominator
        assert coverage.no_data_members == 1
        assert [item.symbol for item in coverage.uncovered] == ["GONE"]
        assert coverage.uncovered[0].spans[0].added == date(2019, 1, 1)

    def test_coverage_below_eighty_percent_fails_the_run(self, tmp_path):
        cache = self.seed(tmp_path, ["AAA"])

        coverage = data.compute_coverage(
            self.membership(["AAA", "GONE1", "GONE2"]),
            cache,
            self.window,
            run_date=date(2020, 2, 1),
        )

        assert coverage.ratio == pytest.approx(1 / 3)
        assert coverage.status == "failed"

    def test_coverage_between_the_floors_warns_but_runs(self, tmp_path):
        # 5 of 6 member-weeks: above the 80% floor, below the 85% warning line.
        cache = self.seed(tmp_path, [f"S{i}" for i in range(5)])
        symbols = [f"S{i}" for i in range(6)]

        coverage = data.compute_coverage(
            self.membership(symbols), cache, self.window, run_date=date(2020, 2, 1)
        )

        assert coverage.ratio == pytest.approx(5 / 6)
        assert coverage.status == "ok"
        assert coverage.low_coverage_warning

    def test_partial_history_is_partial_coverage(self, tmp_path):
        # Two weeks of sessions against four member-weeks in the window.
        cache = self.seed(tmp_path, ["AAA"], sessions=10)

        coverage = data.compute_coverage(
            self.membership(["AAA"]), cache, self.window, run_date=date(2020, 2, 1)
        )

        assert coverage.covered_member_weeks == 2
        assert coverage.total_member_weeks == 4
        assert coverage.uncovered[0].has_data  # had data, just not enough of it

    def test_a_symbol_only_counts_for_the_weeks_it_was_a_member(self, tmp_path):
        cache = self.seed(tmp_path, ["AAA"])
        membership = Membership(spans=(MembershipSpan("AAA", date(2020, 1, 20), None),))

        coverage = data.compute_coverage(membership, cache, self.window, run_date=date(2020, 2, 1))

        assert coverage.total_member_weeks == 2  # the 24th and the 31st
        assert coverage.ratio == 1.0

    def test_the_report_records_the_cache_snapshot_date(self, tmp_path):
        cache = self.seed(tmp_path, ["AAA"])

        coverage = data.compute_coverage(
            self.membership(["AAA"]), cache, self.window, run_date=date(2020, 2, 1)
        )

        assert coverage.as_dict()["snapshot_date"] == "2020-02-01"


class TestDeterminism:
    """§10.1: two runs from the same cache produce byte-identical output."""

    window = data.Window(
        start=date(2020, 1, 6), end=date(2020, 1, 31), fetch_start=date(2019, 1, 6)
    )

    def test_two_runs_from_one_cache_are_byte_identical(self, tmp_path):
        cache = data.PriceCache(tmp_path)
        for symbol in ("AAA", "BBB", "CCC"):
            cache.store(symbol, frame([symbol], sessions=20, start="2020-01-06"))
        cache.write_manifest({"schema": data.CACHE_SCHEMA, "snapshot_date": "2020-02-01"})
        membership = Membership(
            spans=(
                MembershipSpan("AAA", date(2019, 1, 1), None),
                MembershipSpan("BBB", date(2020, 1, 20), None),
                MembershipSpan("GONE", date(2019, 1, 1), date(2020, 1, 15)),
            )
        )

        first = data.compute_coverage(membership, cache, self.window, run_date=date(2020, 2, 1))
        second = data.compute_coverage(membership, cache, self.window, run_date=date(2020, 2, 1))

        assert first.to_json() == second.to_json()

    def test_the_report_does_not_depend_on_wall_clock_time(self, tmp_path):
        # Everything time-varying is passed in (run date) or read from the cache
        # (snapshot date), so the same inputs must give the same bytes.
        cache = data.PriceCache(tmp_path)
        cache.store("AAA", frame(["AAA"], sessions=20, start="2020-01-06"))
        cache.write_manifest({"schema": data.CACHE_SCHEMA, "snapshot_date": "2020-02-01"})
        membership = Membership(spans=(MembershipSpan("AAA", date(2019, 1, 1), None),))

        report = data.compute_coverage(
            membership, cache, self.window, run_date=date(2020, 2, 1)
        ).to_json()

        assert "2020-02-01" in report
        assert report.endswith("\n")

    def test_a_reloaded_cache_frame_round_trips(self, tmp_path):
        cache = data.PriceCache(tmp_path)
        original = frame(["AAA"], sessions=8, start="2020-01-06")

        cache.store("AAA", original)

        pd.testing.assert_frame_equal(
            cache.load("AAA"), original[data.PRICE_COLUMNS], check_freq=False
        )


class TestEligibility:
    """§3.1: eligible only once SMA200, RV252 and the weekly stochastic exist."""

    def series(self, sessions):
        return frame(["AAA"], sessions=sessions, start="2015-02-11")

    def test_too_little_history_is_never_eligible(self):
        assert data.first_eligible_date(self.series(100)) is None

    def test_none_for_an_empty_frame(self):
        assert data.first_eligible_date(pd.DataFrame()) is None

    def test_rv252_is_the_binding_constraint_at_253_closes(self):
        # 253 closes give the 252nd log return; the SMA200 arrived 52 sessions
        # earlier and 14 weekly bars long before that.
        history = self.series(253)

        eligible = data.first_eligible_date(history)

        assert eligible == history.index[252].date()

    def test_one_session_short_of_rv252_is_not_eligible(self):
        assert data.first_eligible_date(self.series(252)) is None

    def test_the_550_day_warm_up_covers_a_full_history(self):
        # The pinned warm-up must make a continuously-traded symbol eligible by
        # the window start — that is what the 550 days are for.
        window = data.evaluation_window(date(2026, 8, 16))
        history = frame(["AAA"], sessions=400, start=window.fetch_start.isoformat())

        assert data.first_eligible_date(history) <= window.start
