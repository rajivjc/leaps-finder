"""Tests for the §6 subscore analysis.

The analysis is only worth reading if two things hold, so both are asserted here
rather than trusted:

* `score_trades` scores each trade **as of its signal date**, with no sight of
  any later bar. `TestNoLookAhead` proves it the only way that is convincing —
  by scoring against a frame that has been given a wildly different future and
  showing the scores do not move.
* the recomputed slowK equals the one the engine recorded. `TestAlignmentGate`
  shows the gate fires when it is violated, because a gate that cannot fail is
  not a gate.

The fixture universe is `test_backtest_engine`'s, so these run against the same
synthetic chains the engine's own parity tests use.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from test_backtest_engine import build_fixture

from leaps_scanner import indicators, scoring
from leaps_scanner.backtest import data, engine, metrics, subscore


@pytest.fixture
def scored(
    tmp_path: Path,
) -> tuple[subscore.ScoreResult, tuple[engine.Trade, ...], data.PriceCache]:
    membership, cache = build_fixture(tmp_path / "fixture")
    window = data.Window(
        start=date(2016, 1, 1), end=date(2020, 12, 31), fetch_start=date(2014, 1, 1)
    )
    result = engine.run(membership, cache, window)
    trades = result.trades[subscore.VARIANT]
    assert trades, "the fixture universe must produce trades for these tests to mean anything"
    benchmark = metrics.BenchmarkPrices(cache.load_benchmark())
    return subscore.score_trades(trades, cache, benchmark), trades, cache


class TestAlignmentGate:
    def test_every_fixture_trade_scores_and_agrees(self, scored) -> None:
        result, trades, _ = scored
        assert result.scored, "no trade scored"
        assert result.insufficient_history == 0
        assert result.unpriced_chains == 0
        assert len(result.scored) == len(trades)

    def test_scores_match_a_direct_call_to_the_reference_implementation(self, scored) -> None:
        """The module must not have re-derived §6 — it must be calling `scoring`."""
        result, _, cache = scored
        for row in result.scored:
            frame = cache.load(row.chain).sort_index()
            signals = indicators.evaluate(
                frame.loc[: pd.Timestamp(row.signal_date)],
                today=row.signal_date + timedelta(days=1),
            )
            assert row.s_trend == scoring.trend_score(signals)
            assert row.s_entry == scoring.entry_score(signals.stoch_k, signals.weeks_since_cross_up)

    def test_a_trade_carrying_the_wrong_slow_k_is_rejected(self, scored) -> None:
        """The gate's whole job. If this passes silently, the analysis is worthless."""
        result, trades, cache = scored
        tampered = dataclasses.replace(trades[0], entry_slow_k=trades[0].entry_slow_k + 0.5)
        benchmark = metrics.BenchmarkPrices(cache.load_benchmark())

        with pytest.raises(subscore.AlignmentError, match="as-of alignment is wrong"):
            subscore.score_trades([tampered], cache, benchmark)

    def test_a_trade_with_no_recorded_slow_k_is_rejected(self, scored) -> None:
        """NaN must not slip through `abs(nan) > tol` being False."""
        _, trades, cache = scored
        tampered = dataclasses.replace(trades[0], entry_slow_k=float("nan"))
        benchmark = metrics.BenchmarkPrices(cache.load_benchmark())

        with pytest.raises(subscore.AlignmentError, match="carries no entry_slow_k"):
            subscore.score_trades([tampered], cache, benchmark)


class TestNoLookAhead:
    def test_scores_ignore_everything_after_the_signal_date(self, scored) -> None:
        """Replace the future with a crash and a spike; the scores must not move.

        This is the failure the module exists to prevent: `indicators.evaluate`
        reads the last bar of whatever frame it is handed, so an unsliced frame
        would score against the end of the series. That produces no error and no
        NaN — only a wrong answer — which is why it needs a test rather than a
        comment.
        """
        result, trades, cache = scored
        benchmark = metrics.BenchmarkPrices(cache.load_benchmark())
        baseline = {
            (row.chain, row.signal_date): (row.s_trend, row.s_entry) for row in result.scored
        }

        mutated = data.PriceCache(cache.root.parent / "mutated")
        mutations = 0
        for chain in sorted({row.chain for row in result.scored}):
            frame = cache.load(chain).sort_index()
            changed = frame.copy()
            # Each chain's own last signal is its cutoff: a global one would
            # leave the earlier-finishing chains untouched and quietly test
            # nothing for them.
            cutoff = max(row.signal_date for row in result.scored if row.chain == chain)
            after = np.asarray(frame.index > pd.Timestamp(cutoff))
            if after.any():
                mutations += 1
                # A 90% collapse alternating with a 10x spike: anything reading
                # past the cutoff moves, and by far more than a rounding
                # difference.
                factor = np.where(np.arange(int(after.sum())) % 2 == 0, 0.1, 10.0)
                for column in ("Open", "High", "Low", "Close", "Adj Close"):
                    if column in changed:
                        changed.loc[after, column] = changed.loc[after, column].to_numpy() * factor
            # Stored whether or not it changed, so no chain goes missing from
            # the mutated cache and turns a look-ahead check into a skip.
            mutated.store(chain, changed)

        assert mutations, "no chain had post-signal history: this test would prove nothing"

        rescored = subscore.score_trades(trades, cache=mutated, benchmark=benchmark)
        assert len(rescored.scored) == len(result.scored)
        assert rescored.unpriced_chains == 0
        for row in rescored.scored:
            assert (row.s_trend, row.s_entry) == baseline[(row.chain, row.signal_date)], (
                f"{row.chain} {row.signal_date} moved when only its future changed"
            )

    def test_the_slice_never_includes_a_later_session(self, scored) -> None:
        result, _, cache = scored
        for row in result.scored:
            frame = cache.load(row.chain).sort_index()
            sliced = frame.loc[: pd.Timestamp(row.signal_date)]
            assert sliced.index[-1].date() <= row.signal_date

    def test_a_frame_whose_slice_leaks_is_rejected(self) -> None:
        """`_evaluate_at` must reject rather than quietly score the wrong window.

        An unsorted frame is the case that matters: `loc[:x]` on a descending
        index keeps every row down to the *first* match, so a later session
        survives in the middle of the slice while the last row still looks
        legal. `evaluate` sorts before reading its last bar, so that session
        would silently become the as-of — which is why the guard tests the
        maximum rather than the final row.
        """
        index = pd.DatetimeIndex([pd.Timestamp("2020-01-07"), pd.Timestamp("2020-01-06")])
        frame = pd.DataFrame(
            {
                "Open": [1.0, 1.0],
                "High": [1.0, 1.0],
                "Low": [1.0, 1.0],
                "Close": [1.0, 1.0],
                "Volume": [1.0, 1.0],
            },
            index=index,
        )
        assert frame.loc[: pd.Timestamp("2020-01-06")].index[-1].date() == date(2020, 1, 6)

        with pytest.raises(subscore.AlignmentError, match="leaked session"):
            subscore._evaluate_at(frame, "X", date(2020, 1, 6))


class TestStatistics:
    def test_bootstrap_is_deterministic_and_brackets_the_mean(self) -> None:
        values = [0.05, -0.02, 0.11, -0.07, 0.03, 0.09, -0.01, 0.04]
        blocks = [
            "2020-01",
            "2020-01",
            "2020-02",
            "2020-02",
            "2020-03",
            "2020-03",
            "2020-04",
            "2020-04",
        ]

        first = subscore.block_bootstrap_ci(values, blocks, reps=500)
        second = subscore.block_bootstrap_ci(values, blocks, reps=500)
        assert first == second, "a moving interval is indistinguishable from a moving finding"
        assert first is not None
        low, high = first
        assert low <= float(np.mean(values)) <= high

    def test_bootstrap_returns_none_rather_than_inventing_an_interval(self) -> None:
        assert subscore.block_bootstrap_ci([], []) is None
        assert subscore.block_bootstrap_ci([0.1, 0.2], ["2020-01"]) is None

    def test_profit_factor_is_none_when_nothing_lost(self) -> None:
        assert subscore._profit_factor(np.array([0.1, 0.2])) is None
        assert subscore._profit_factor(np.array([0.2, -0.1])) == pytest.approx(2.0)

    def test_buckets_do_not_split_identical_scores(self) -> None:
        """Entry piles a third of its mass on 100; that must not be forced apart."""
        values = np.array([1.0] * 8 + [2.0, 3.0], dtype="float64")
        assigned = subscore._quantile_buckets(values, 10)
        assert len(set(assigned[:8])) == 1

    def test_tail_spread_reports_none_without_priced_trades(self) -> None:
        assert subscore.tail_spread([], "s_trend", 0.1)["observed"] is None

    def test_a_missing_benchmark_only_blanks_the_benchmark_correlation(self, scored) -> None:
        """§3.5 lets a run continue with no SPY cached.

        `r_trade` and `holding_days` need no benchmark, so they must survive
        that — otherwise a thin SPY cache silently erases the duration
        relationship while the report still renders as a complete result.
        """
        _, trades, cache = scored
        without = subscore.score_trades(trades, cache, metrics.BenchmarkPrices(None))
        assert without.unpriced_benchmark == len(trades), "benchmark should price nothing here"

        correlation = subscore.correlations(without.scored, "s_trend")
        assert correlation["market_delta"]["spearman"] is None
        assert correlation["market_delta"]["trades"] == 0
        for name in ("r_trade", "holding_days"):
            assert correlation[name]["spearman"] is not None, f"{name} needs no benchmark"
            assert correlation[name]["trades"] == len(without.scored)

    def test_each_correlation_reports_its_own_denominator(self, scored) -> None:
        result, _, _ = scored
        correlation = subscore.correlations(result.scored, "s_trend")
        priced = sum(1 for row in result.scored if row.market_delta is not None)
        overlaid = sum(1 for row in result.scored if row.r_overlay is not None)

        assert correlation["market_delta"]["trades"] == priced
        assert correlation["r_overlay"]["trades"] == overlaid
        assert correlation["r_trade"]["trades"] == len(result.scored)


class TestArtifacts:
    def test_payload_round_trips_and_is_byte_stable(self, scored) -> None:
        result, _, _ = scored
        payload = subscore.build_results(result, run_date=date(2026, 8, 17), variant="base")

        first = subscore.results_json(payload)
        assert first == subscore.results_json(payload)
        assert json.loads(first)["gate"]["scored"] == len(result.scored)

    def test_non_finite_floats_are_refused_rather_than_written(self) -> None:
        with pytest.raises(ValueError, match="non-finite"):
            subscore.results_json({"x": float("nan")})

    def test_markdown_carries_the_gate_and_the_caveats(self, scored) -> None:
        result, _, _ = scored
        payload = subscore.build_results(result, run_date=date(2026, 8, 17), variant="base")
        text = subscore.build_markdown(payload)

        assert "Analysis, not a milestone" in text
        assert "alignment gate" in text.lower()
        assert "Market delta is the quantity that decides it" in text
        # The untested 60% is the single most important caveat on the page.
        assert "Quality, Option economics, Valuation" in text
        assert "Not financial advice." in text

    def test_write_report_creates_both_files(self, scored, tmp_path: Path) -> None:
        result, _, _ = scored
        payload = subscore.build_results(result, run_date=date(2026, 8, 17), variant="base")
        written = subscore.write_report(payload, tmp_path / "out")

        assert {path.name for path in written} == {"results.json", "report.md"}
        assert all(path.exists() and path.stat().st_size > 0 for path in written)
