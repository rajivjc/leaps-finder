"""Fetch-layer tests (SPEC.md §3.2, §9). No network: the downloader is injected."""

import random

import pandas as pd
import pytest

from leaps_scanner import prices
from leaps_scanner.prices import Throttle


def ohlcv(symbol_count: int, symbols, sessions: int = 3) -> pd.DataFrame:
    """A frame shaped the way yfinance returns it for `symbol_count` symbols."""
    index = pd.bdate_range(end="2026-08-14", periods=sessions)
    if symbol_count == 1:
        return pd.DataFrame({c: [1.0] * sessions for c in prices.OHLCV_COLUMNS}, index=index)

    columns = pd.MultiIndex.from_product([symbols, prices.OHLCV_COLUMNS])
    return pd.DataFrame([[1.0] * len(columns)] * sessions, index=index, columns=columns)


class TestBatching:
    def test_splits_into_full_batches_and_a_remainder(self):
        assert prices.batched(list("abcde"), 2) == [["a", "b"], ["c", "d"], ["e"]]

    def test_empty_input_produces_no_batches(self):
        assert prices.batched([], 25) == []

    def test_zero_batch_size_is_rejected(self):
        with pytest.raises(ValueError):
            prices.batched(["a"], 0)


class TestThrottle:
    def test_spaces_calls_to_the_configured_rate(self):
        now = [0.0]
        slept = []

        def sleeper(seconds):
            slept.append(seconds)
            now[0] += seconds

        limiter = Throttle(rate=5.0, sleeper=sleeper, clock=lambda: now[0])
        limiter.wait()  # first call is free
        limiter.wait()

        assert slept == [pytest.approx(0.2)]

    def test_does_not_sleep_when_enough_time_already_passed(self):
        now = [0.0]
        slept = []
        limiter = Throttle(rate=5.0, sleeper=slept.append, clock=lambda: now[0])

        limiter.wait()
        now[0] = 10.0
        limiter.wait()

        assert slept == []

    def test_five_requests_per_second_is_the_ceiling(self):
        # SPEC.md §9 pins the rate; this fails if the default is ever loosened.
        assert prices.MAX_REQUESTS_PER_SECOND == 5.0

    def test_non_positive_rate_is_rejected(self):
        with pytest.raises(ValueError):
            Throttle(rate=0)


class TestSplitBatchFrame:
    def test_splits_a_multi_symbol_frame(self):
        frame = ohlcv(2, ["AAPL", "MSFT"])

        result = prices.split_batch_frame(frame, ["AAPL", "MSFT"])

        assert sorted(result) == ["AAPL", "MSFT"]
        assert list(result["AAPL"].columns) == prices.OHLCV_COLUMNS

    def test_handles_the_flat_single_symbol_shape(self):
        result = prices.split_batch_frame(ohlcv(1, ["AAPL"]), ["AAPL"])

        assert list(result) == ["AAPL"]

    def test_symbol_absent_from_the_response_is_omitted(self):
        frame = ohlcv(2, ["AAPL", "MSFT"])

        result = prices.split_batch_frame(frame, ["AAPL", "MSFT", "NVDA"])

        assert "NVDA" not in result

    def test_rows_without_a_close_are_dropped(self):
        frame = ohlcv(2, ["AAPL", "MSFT"])
        frame.loc[frame.index[0], ("AAPL", "Close")] = None

        result = prices.split_batch_frame(frame, ["AAPL"])

        assert len(result["AAPL"]) == 2

    def test_symbol_with_no_usable_rows_is_omitted_not_returned_empty(self):
        frame = ohlcv(2, ["AAPL", "MSFT"])
        frame[("AAPL", "Close")] = None

        assert "AAPL" not in prices.split_batch_frame(frame, ["AAPL", "MSFT"])

    def test_empty_response_yields_nothing(self):
        assert prices.split_batch_frame(pd.DataFrame(), ["AAPL"]) == {}


class TestFetchDailyOhlcv:
    def _fetch(self, downloader, symbols, **kwargs):
        return prices.fetch_daily_ohlcv(
            symbols,
            downloader=downloader,
            throttle=Throttle(rate=1e9, sleeper=lambda _: None, clock=lambda: 0.0),
            sleeper=lambda _: None,
            rng=random.Random(0),
            **kwargs,
        )

    def test_fetches_every_symbol_across_batches(self):
        calls = []

        def downloader(symbols, period):
            calls.append(list(symbols))
            return ohlcv(len(symbols), symbols)

        outcome = self._fetch(downloader, ["A", "B", "C"], batch_size=2)

        assert calls == [["A", "B"], ["C"]]
        assert sorted(outcome.frames) == ["A", "B", "C"]
        assert outcome.failed == ()

    def test_duplicate_symbols_are_requested_once(self):
        calls = []

        def downloader(symbols, period):
            calls.append(list(symbols))
            return ohlcv(len(symbols), symbols)

        self._fetch(downloader, ["A", "B", "A"], batch_size=10)

        assert calls == [["A", "B"]]

    def test_transient_failure_is_retried_then_succeeds(self):
        attempts = []

        def downloader(symbols, period):
            attempts.append(list(symbols))
            if len(attempts) == 1:
                raise ConnectionError("rate limited")
            return ohlcv(len(symbols), symbols)

        outcome = self._fetch(downloader, ["A", "B"], batch_size=2)

        assert len(attempts) == 2
        assert sorted(outcome.frames) == ["A", "B"]
        assert outcome.failed == ()

    def test_empty_response_is_retried_like_a_transport_error(self):
        # yfinance signals failure (rate limiting included) by returning an
        # empty frame rather than raising. Retrying only on exceptions would
        # leave the failure mode that actually happens unretried.
        attempts = []

        def downloader(symbols, period):
            attempts.append(list(symbols))
            if len(attempts) < 3:
                return pd.DataFrame()
            return ohlcv(len(symbols), symbols)

        outcome = self._fetch(downloader, ["A", "B"], batch_size=2)

        assert len(attempts) == 3
        assert sorted(outcome.frames) == ["A", "B"]
        assert outcome.failed == ()

    def test_persistently_empty_batch_is_reported_failed(self):
        def downloader(symbols, period):
            return pd.DataFrame()

        outcome = self._fetch(downloader, ["A", "B"], batch_size=2)

        assert outcome.frames == {}
        assert outcome.failed == ("A", "B")

    def test_partial_batch_is_accepted_without_retrying_the_whole_batch(self):
        # A symbol missing from an otherwise good response is far more likely
        # delisted than throttled; chasing it would cost more than it saves.
        attempts = []

        def downloader(symbols, period):
            attempts.append(list(symbols))
            return ohlcv(2, ["A", "B"])  # "C" never appears

        outcome = self._fetch(downloader, ["A", "B", "C"], batch_size=3)

        assert len(attempts) == 1
        assert sorted(outcome.frames) == ["A", "B"]
        assert outcome.failed == ("C",)

    def test_gives_up_after_three_attempts_and_reports_the_symbols(self):
        attempts = []

        def downloader(symbols, period):
            attempts.append(list(symbols))
            raise ConnectionError("down")

        outcome = self._fetch(downloader, ["A", "B"], batch_size=2)

        assert len(attempts) == prices.MAX_RETRIES
        assert outcome.frames == {}
        assert outcome.failed == ("A", "B")

    def test_one_bad_batch_does_not_abort_the_others(self):
        def downloader(symbols, period):
            if "B" in symbols:
                raise ConnectionError("down")
            return ohlcv(len(symbols), symbols)

        outcome = self._fetch(downloader, ["A", "B", "C"], batch_size=1)

        assert sorted(outcome.frames) == ["A", "C"]
        assert outcome.failed == ("B",)
        assert outcome.failure_rate == pytest.approx(1 / 3)

    def test_backoff_grows_exponentially_between_attempts(self):
        slept = []

        def downloader(symbols, period):
            raise ConnectionError("down")

        prices.fetch_daily_ohlcv(
            ["A"],
            downloader=downloader,
            throttle=Throttle(rate=1e9, sleeper=lambda _: None, clock=lambda: 0.0),
            sleeper=slept.append,
            rng=random.Random(0),
        )

        # Two waits for three attempts, each in its jittered window and growing.
        assert len(slept) == prices.MAX_RETRIES - 1
        assert 1.0 <= slept[0] <= 1.5
        assert 2.0 <= slept[1] <= 2.5

    def test_failure_rate_is_zero_when_nothing_was_requested(self):
        assert prices.FetchOutcome(frames={}, failed=()).failure_rate == 0.0


class TestRetryFetch:
    def throttle(self):
        return Throttle(rate=1e9, sleeper=lambda _: None, clock=lambda: 0.0)

    def test_returns_the_first_usable_result(self):
        result = prices.retry_fetch(
            lambda: {"ok": True},
            describe="thing",
            throttle=self.throttle(),
            sleeper=lambda _: None,
        )

        assert result == {"ok": True}

    def test_empty_results_are_retried_like_errors(self):
        attempts = []

        def fetch():
            attempts.append(1)
            return [] if len(attempts) == 1 else ["row"]

        result = prices.retry_fetch(
            fetch,
            describe="thing",
            throttle=self.throttle(),
            sleeper=lambda _: None,
            is_empty=lambda listed: len(listed) == 0,
        )

        assert result == ["row"]
        assert len(attempts) == 2

    def test_a_raising_emptiness_probe_is_retried_not_fatal(self):
        # A malformed result that makes the probe itself raise must count as
        # a failed attempt, never escape and abort the caller's whole run.
        attempts = []

        def fetch():
            attempts.append(1)
            return 42 if len(attempts) == 1 else {"calls": "data"}

        result = prices.retry_fetch(
            fetch,
            describe="thing",
            throttle=self.throttle(),
            sleeper=lambda _: None,
            # Probe assumes a sized payload; the malformed 42 makes it raise.
            is_empty=lambda payload: len(payload) == 0,
        )

        assert result == {"calls": "data"}
        assert len(attempts) == 2

    def test_exhausted_retries_return_none(self):
        result = prices.retry_fetch(
            lambda: (_ for _ in ()).throw(ConnectionError("down")),
            describe="thing",
            throttle=self.throttle(),
            sleeper=lambda _: None,
            max_retries=2,
        )

        assert result is None
