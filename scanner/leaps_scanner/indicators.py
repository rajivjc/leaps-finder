"""Signals, exactly as pinned in SPEC.md §4.

Pure functions over price frames — no network, no database. Everything the
scanner decides with is computed here so it can be tested against hand-computed
fixtures.

The load-bearing rule: signals only ever use *completed* weekly bars. A weekly
value that can still change before Friday's close would repaint the screener,
so the in-progress week is dropped before anything is computed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

# Weekly slow stochastic (10, 3, 3).
STOCH_K_PERIOD = 10
STOCH_K_SMOOTH = 3
STOCH_D_SMOOTH = 3

# Entry zone and the exit threshold (SPEC.md §4).
ZONE_LOW = 20.0
ZONE_HIGH = 70.0
EXIT_LEVEL = 20.0

SMA_FAST = 50
SMA_SLOW = 200
SESSIONS_52W = 252

# Bars needed before the last stochastic value is fully defined: the %K window,
# then two more for each 3-period average.
MIN_WEEKLY_BARS = STOCH_K_PERIOD + (STOCH_K_SMOOTH - 1) + (STOCH_D_SMOOTH - 1)
MIN_DAILY_BARS = SMA_SLOW

OHLC_AGG = {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}


class InsufficientHistory(ValueError):
    """Raised when a symbol lacks the bars needed to define every signal."""


@dataclass(frozen=True)
class Signals:
    """Everything §4 defines for one symbol, as of one completed week."""

    as_of_date: date
    spot: float
    sma50: float
    sma200: float
    trend_pass: bool
    stoch_k: float
    stoch_d: float
    stoch_k_prev: float
    in_zone: bool
    turning_up: bool
    pct_off_52w_high: float
    avg_volume_30d: float


def sma(series: pd.Series, window: int) -> pd.Series:
    """Simple moving average; NaN until `window` observations exist."""
    return series.rolling(window=window, min_periods=window).mean()


def weekly_bars(daily: pd.DataFrame) -> pd.DataFrame:
    """Resample daily OHLCV to Friday-ending weekly bars.

    Includes the current, in-progress week — call `completed_weekly_bars` for
    anything a signal depends on.
    """
    resampled = daily.resample("W-FRI").agg(OHLC_AGG)
    return resampled.dropna(subset=["Close"])


def completed_weekly_bars(daily: pd.DataFrame, today: date | None = None) -> pd.DataFrame:
    """Weekly bars with the in-progress week dropped (SPEC.md §4).

    A week is complete only when both hold:

    * the data has reached its Friday — a last session of Monday-Thursday means
      the week is still trading; and
    * that session is not today — a bar dated today is still being written, and
      Friday's bar mid-session would repaint at the close.

    The second condition is what makes an ad-hoc Friday-lunchtime run agree with
    the Saturday cron. A Friday holiday makes this drop a week that had in fact
    finished on the Thursday: one week stale, which is the safe direction to be
    wrong in.
    """
    weekly = weekly_bars(daily)
    if weekly.empty or daily.empty:
        return weekly

    reference = date.today() if today is None else today
    last_session = daily.index[-1]
    if last_session.weekday() < 4 or last_session.date() >= reference:
        return weekly.iloc[:-1]
    return weekly


def slow_stochastic(
    weekly: pd.DataFrame,
    k_period: int = STOCH_K_PERIOD,
    k_smooth: int = STOCH_K_SMOOTH,
    d_smooth: int = STOCH_D_SMOOTH,
) -> pd.DataFrame:
    """Slow stochastic (SPEC.md §4).

        rawK_t = 100 * (C_t - min(L, 10w)) / (max(H, 10w) - min(L, 10w))
        slowK  = SMA_3(rawK)
        D      = SMA_3(slowK)

    A window whose high equals its low has no defined %K; that yields NaN rather
    than an invented midpoint, and the symbol is then reported as having
    incomplete signals instead of a fabricated one.
    """
    lowest = weekly["Low"].rolling(window=k_period, min_periods=k_period).min()
    highest = weekly["High"].rolling(window=k_period, min_periods=k_period).max()
    span = highest - lowest

    raw_k = pd.Series(
        np.where(span > 0, 100.0 * (weekly["Close"] - lowest) / span, np.nan),
        index=weekly.index,
        dtype="float64",
    )
    slow_k = sma(raw_k, k_smooth)
    d = sma(slow_k, d_smooth)

    return pd.DataFrame({"raw_k": raw_k, "slow_k": slow_k, "d": d})


def crosses_below(series: pd.Series, level: float) -> bool:
    """True when the last value crossed below `level` on this bar.

    Crossing is a transition, not a state: at-or-above previously, below now.
    A series that has sat below the level for weeks is *not* crossing it
    (SPEC.md §10).
    """
    clean = series.dropna()
    if len(clean) < 2:
        return False

    previous, current = float(clean.iloc[-2]), float(clean.iloc[-1])
    return previous >= level and current < level


def crosses_above(series: pd.Series, level: float) -> bool:
    """Mirror of `crosses_below`: at-or-below previously, above now."""
    clean = series.dropna()
    if len(clean) < 2:
        return False

    previous, current = float(clean.iloc[-2]), float(clean.iloc[-1])
    return previous <= level and current > level


def evaluate(daily: pd.DataFrame, today: date | None = None) -> Signals:
    """Compute every §4 signal for one symbol from its daily OHLCV history.

    Daily indicators are read at the close of the last completed week, not at
    whatever the most recent session happens to be, so a Saturday scan and a
    Tuesday rerun of the same week produce identical numbers.
    """
    if daily.empty:
        raise InsufficientHistory("no daily bars")

    daily = daily.sort_index()
    weekly = completed_weekly_bars(daily, today=today)

    if len(weekly) < MIN_WEEKLY_BARS:
        raise InsufficientHistory(f"{len(weekly)} completed weekly bars, need {MIN_WEEKLY_BARS}")

    as_of = weekly.index[-1]
    # The weekly bar is labelled with its Friday, which may be a holiday or fall
    # after the last session; take the last daily close at or before it.
    through_week = daily.loc[:as_of]
    if len(through_week) < MIN_DAILY_BARS:
        raise InsufficientHistory(f"{len(through_week)} daily bars, need {MIN_DAILY_BARS}")

    close = through_week["Close"]
    spot = float(close.iloc[-1])
    sma50 = float(sma(close, SMA_FAST).iloc[-1])
    sma200 = float(sma(close, SMA_SLOW).iloc[-1])

    stoch = slow_stochastic(weekly)
    slow_k = stoch["slow_k"]
    stoch_k = float(slow_k.iloc[-1])
    stoch_k_prev = float(slow_k.iloc[-2])
    stoch_d = float(stoch["d"].iloc[-1])

    if not all(np.isfinite([spot, sma50, sma200, stoch_k, stoch_k_prev, stoch_d])):
        raise InsufficientHistory("signal inputs contain NaN")

    high_52w = float(through_week["High"].tail(SESSIONS_52W).max())
    avg_volume_30d = float(through_week["Volume"].tail(30).mean())

    return Signals(
        as_of_date=as_of.date(),
        spot=spot,
        sma50=sma50,
        sma200=sma200,
        # SPEC.md §4: close above both averages, with the 50 above the 200.
        trend_pass=bool(spot > sma50 and spot > sma200 and sma50 > sma200),
        stoch_k=stoch_k,
        stoch_d=stoch_d,
        stoch_k_prev=stoch_k_prev,
        in_zone=bool(ZONE_LOW <= stoch_k <= ZONE_HIGH),
        turning_up=bool(stoch_k > stoch_d and stoch_k > stoch_k_prev),
        # Zero at the high, negative below it.
        pct_off_52w_high=spot / high_52w - 1.0,
        avg_volume_30d=avg_volume_30d,
    )
