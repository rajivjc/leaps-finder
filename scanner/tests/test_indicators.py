"""Signal tests (SPEC.md §4, §10).

The stochastic fixture is built so every number can be checked by hand. Bars 3
and 11 carry a high of 200; bars 4 and 12 carry a low of 100. Every 10-bar
window ending at bar 9 or later therefore spans exactly 100 to 200, which makes
the range 100 and collapses

    rawK = 100 * (Close - 100) / (200 - 100)

to `Close - 100`.
"""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from leaps_scanner import indicators
from leaps_scanner.indicators import InsufficientHistory, Signals

# (Close, High, Low) per weekly bar. Bars 0-8 are filler that stays inside the
# 100-200 envelope; bars 9-13 produce rawK of 30, 50, 70, 60, 40.
FIXTURE_BARS = [
    (150, 160, 140),  # 0
    (150, 160, 140),  # 1
    (150, 160, 140),  # 2
    (150, 200, 140),  # 3  <- window high
    (150, 160, 100),  # 4  <- window low
    (150, 160, 140),  # 5
    (150, 160, 140),  # 6
    (150, 160, 140),  # 7
    (150, 160, 140),  # 8
    (130, 140, 120),  # 9   rawK = 30
    (150, 160, 140),  # 10  rawK = 50
    (170, 200, 160),  # 11  rawK = 70
    (160, 170, 100),  # 12  rawK = 60
    (140, 150, 130),  # 13  rawK = 40
]


def weekly_frame(bars=FIXTURE_BARS) -> pd.DataFrame:
    index = pd.date_range("2026-01-02", periods=len(bars), freq="W-FRI")
    return pd.DataFrame(
        {
            "Open": [c for c, _, _ in bars],
            "High": [h for _, h, _ in bars],
            "Low": [low for _, _, low in bars],
            "Close": [c for c, _, _ in bars],
            "Volume": [1_000_000] * len(bars),
        },
        index=index,
    )


class TestSlowStochastic:
    def test_raw_k_matches_hand_computation(self):
        raw_k = indicators.slow_stochastic(weekly_frame())["raw_k"]

        assert raw_k.iloc[:9].isna().all()  # no 10-bar window yet
        assert list(raw_k.iloc[9:]) == pytest.approx([30.0, 50.0, 70.0, 60.0, 40.0])

    def test_slow_k_is_the_three_period_average_of_raw_k(self):
        slow_k = indicators.slow_stochastic(weekly_frame())["slow_k"]

        # rawK is undefined before bar 9, so the first full 3-bar average is at
        # bar 11 — smoothing does not get to start early on partial windows.
        assert slow_k.iloc[:11].isna().all()
        assert slow_k.iloc[11].item() == pytest.approx(50.0)  # (30 + 50 + 70) / 3
        assert slow_k.iloc[12].item() == pytest.approx(60.0)  # (50 + 70 + 60) / 3
        assert slow_k.iloc[13].item() == pytest.approx(56.666667)  # (70 + 60 + 40) / 3

    def test_d_is_the_three_period_average_of_slow_k(self):
        d = indicators.slow_stochastic(weekly_frame())["d"]

        # (50 + 60 + 56.666667) / 3
        assert d.iloc[13].item() == pytest.approx(55.555556)
        assert d.iloc[:13].isna().all()

    def test_smoothing_is_applied_in_order_not_transposed(self):
        # SMA_3(SMA_3(rawK)) differs from SMA_3(rawK) at the final bar; this
        # pins that D is smoothed from slowK and not from rawK directly.
        stoch = indicators.slow_stochastic(weekly_frame())

        assert stoch["d"].iloc[13].item() != pytest.approx(stoch["slow_k"].iloc[13].item())

    def test_flat_window_yields_nan_rather_than_an_invented_midpoint(self):
        flat = [(150, 150, 150)] * 14

        raw_k = indicators.slow_stochastic(weekly_frame(flat))["raw_k"]

        assert raw_k.iloc[9:].isna().all()


class TestCrossingSemantics:
    """Crossing is a transition, not a state (SPEC.md §10)."""

    def test_crossing_below_requires_the_previous_bar_to_be_at_or_above(self):
        assert indicators.crosses_below(pd.Series([25.0, 18.0]), 20.0) is True

    def test_sitting_below_the_level_is_not_crossing_it(self):
        assert indicators.crosses_below(pd.Series([15.0, 12.0]), 20.0) is False

    def test_touching_the_level_from_above_is_not_yet_a_cross(self):
        assert indicators.crosses_below(pd.Series([25.0, 20.0]), 20.0) is False

    def test_leaving_the_level_downward_is_a_cross(self):
        assert indicators.crosses_below(pd.Series([20.0, 19.9]), 20.0) is True

    def test_crossing_above_mirrors_it(self):
        assert indicators.crosses_above(pd.Series([15.0, 25.0]), 20.0) is True
        assert indicators.crosses_above(pd.Series([25.0, 30.0]), 20.0) is False

    def test_nans_are_skipped_not_treated_as_a_crossing(self):
        assert indicators.crosses_below(pd.Series([25.0, np.nan]), 20.0) is False

    def test_a_single_observation_cannot_cross(self):
        assert indicators.crosses_below(pd.Series([10.0]), 20.0) is False


def daily_frame(sessions: int, closes=None, end="2026-08-14") -> pd.DataFrame:
    """Business-daily OHLCV ending on `end` (a Friday by default)."""
    index = pd.bdate_range(end=end, periods=sessions)
    values = np.linspace(100.0, 200.0, sessions) if closes is None else np.asarray(closes)
    return pd.DataFrame(
        {
            "Open": values,
            "High": values + 1.0,
            "Low": values - 1.0,
            "Close": values,
            "Volume": np.full(sessions, 1_000_000.0),
        },
        index=index,
    )


class TestCompletedWeeklyBars:
    def test_in_progress_week_is_dropped(self):
        # Wednesday: this week is still trading.
        daily = daily_frame(400, end="2026-08-12")

        weekly = indicators.completed_weekly_bars(daily)

        assert weekly.index[-1].date().isoformat() == "2026-08-07"
        assert len(weekly) == len(indicators.weekly_bars(daily)) - 1

    def test_friday_close_completes_the_week(self):
        daily = daily_frame(400, end="2026-08-14")

        # Read the following day: Friday's session is over.
        weekly = indicators.completed_weekly_bars(daily, today=date(2026, 8, 15))

        assert weekly.index[-1].date().isoformat() == "2026-08-14"
        assert len(weekly) == len(indicators.weekly_bars(daily))

    def test_friday_mid_session_is_not_yet_complete(self):
        # Running at lunchtime on Friday: today's bar is still being written,
        # so the week must not count yet or the numbers repaint at the close.
        daily = daily_frame(400, end="2026-08-14")

        weekly = indicators.completed_weekly_bars(daily, today=date(2026, 8, 14))

        assert weekly.index[-1].date().isoformat() == "2026-08-07"

    def test_friday_run_and_saturday_run_agree_once_the_session_closed(self):
        daily = daily_frame(400, end="2026-08-14")

        friday_lunchtime = indicators.completed_weekly_bars(daily, today=date(2026, 8, 14))
        saturday = indicators.completed_weekly_bars(daily, today=date(2026, 8, 15))

        # The mid-session run is a week behind rather than showing a half-week.
        assert friday_lunchtime.index[-1] < saturday.index[-1]

    def test_weekly_bar_aggregates_the_week_not_the_last_session(self):
        daily = daily_frame(400, end="2026-08-14")

        weekly = indicators.completed_weekly_bars(daily)
        last_week = daily.loc["2026-08-10":"2026-08-14"]

        assert weekly["High"].iloc[-1] == last_week["High"].max()
        assert weekly["Low"].iloc[-1] == last_week["Low"].min()
        assert weekly["Open"].iloc[-1] == last_week["Open"].iloc[0]
        assert weekly["Close"].iloc[-1] == last_week["Close"].iloc[-1]


class TestEvaluate:
    def test_uptrend_passes_the_trend_filter(self):
        signals = indicators.evaluate(daily_frame(500))

        assert isinstance(signals, Signals)
        assert signals.trend_pass is True
        assert signals.sma50 > signals.sma200
        assert signals.as_of_date.isoformat() == "2026-08-14"

    def test_downtrend_fails_the_trend_filter(self):
        falling = daily_frame(500, closes=np.linspace(200.0, 100.0, 500))

        assert indicators.evaluate(falling).trend_pass is False

    def test_price_below_the_averages_fails_even_with_a_golden_cross(self):
        # Long uptrend, then a fresh drop: SMA50 is still above SMA200, but the
        # close is under both. All three conditions are required.
        closes = np.concatenate([np.linspace(100.0, 200.0, 495), np.full(5, 90.0)])

        signals = indicators.evaluate(daily_frame(500, closes=closes))

        assert signals.sma50 > signals.sma200
        assert signals.spot < signals.sma50
        assert signals.trend_pass is False

    def test_pct_off_52w_high_is_zero_at_the_high_and_negative_below(self):
        # Closes run to 200, so the 52-week high is that session's high of 201.
        at_high = indicators.evaluate(daily_frame(500))
        assert at_high.pct_off_52w_high == pytest.approx(200.0 / 201.0 - 1.0)

        closes = np.concatenate([np.linspace(100.0, 200.0, 480), np.full(20, 150.0)])
        off_high = indicators.evaluate(daily_frame(500, closes=closes))
        assert off_high.pct_off_52w_high == pytest.approx(150.0 / 201.0 - 1.0)

    def test_midweek_rerun_reproduces_the_friday_numbers(self):
        # One history, read on its Friday and again the following Wednesday.
        # The extra sessions belong to an in-progress week, so every signal must
        # come out identical — this is the no-repaint guarantee.
        full = daily_frame(503, end="2026-08-19")
        friday = full.loc[:"2026-08-14"]

        assert indicators.evaluate(friday) == indicators.evaluate(full)

    def test_short_history_is_rejected_rather_than_guessed_at(self):
        with pytest.raises(InsufficientHistory):
            indicators.evaluate(daily_frame(120))

    def test_empty_history_is_rejected(self):
        with pytest.raises(InsufficientHistory):
            indicators.evaluate(daily_frame(0))
