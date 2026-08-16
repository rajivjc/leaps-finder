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

# SPEC.md §6 Trend: share of the last 60 sessions closing above the SMA50.
SHARE_SESSIONS = 60

# Completed weeks of history persisted for the charts (SPEC.md §8.2 asks for ~1y
# of weekly candles). Two years is kept because the SMA200 overlay needs 200
# daily sessions before it is defined at all: a 1-year chart with a full SMA200
# line requires roughly a year of bars on top of that warm-up.
WEEKLY_HISTORY_WEEKS = 104

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
    # §6 scoring inputs: fraction of the last 60 sessions closing above their
    # own SMA50, and how many completed weeks ago slowK last crossed above D
    # (1 = on the latest bar; None = never in the available history).
    share_above_sma50_60d: float
    weeks_since_cross_up: int | None


@dataclass(frozen=True)
class DailyTrend:
    """§4's trend, read at the most recent daily session.

    `Signals` reads the daily averages at the last *completed week* so that a
    Saturday scan and a Tuesday rerun of the same week agree. §7 gives the
    trend-break exit a **daily** cadence, which is the opposite requirement: it
    has to see today's close, not last Friday's. Hence a second reading rather
    than a reinterpretation of the first.
    """

    as_of_date: date
    close: float
    sma50: float
    sma200: float
    trend_pass: bool
    trend_broken: bool


@dataclass(frozen=True)
class WeeklyBar:
    """One completed weekly bar plus the overlays the charts draw on it (§8.2)."""

    week_ending: date
    open: float
    high: float
    low: float
    close: float
    volume: float | None
    # Null wherever the indicator is not yet defined over the fetched window;
    # the chart draws a gap there rather than a fabricated value.
    slow_k: float | None
    d: float | None
    sma50: float | None
    sma200: float | None


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


def weeks_since_cross_up(slow_k: pd.Series, d: pd.Series) -> int | None:
    """Completed weeks since slowK last crossed above D (SPEC.md §6 Entry).

    A cross on bar t means slowK ≤ D on t−1 and slowK > D on t. Returns 1 when
    the cross happened on the latest bar, 2 for the bar before, and so on;
    None when no cross exists in the overlapping history.
    """
    diff = (slow_k - d).dropna()
    values = diff.to_numpy()
    for back in range(1, len(values)):
        if values[-back] > 0 and values[-back - 1] <= 0:
            return back
    return None


def crosses_above(series: pd.Series, level: float) -> bool:
    """Mirror of `crosses_below`: at-or-below previously, above now."""
    clean = series.dropna()
    if len(clean) < 2:
        return False

    previous, current = float(clean.iloc[-2]), float(clean.iloc[-1])
    return previous <= level and current > level


def _finite(value: object) -> float | None:
    """Coerce to float, mapping missing and non-finite alike to None.

    NaN is what pandas returns for an indicator that is not defined yet; it is
    also not representable in JSON. A null column says "not defined" honestly,
    where a zero would read as a real reading of zero.
    """
    if value is None:
        return None
    number = float(value)  # type: ignore[arg-type]
    return number if np.isfinite(number) else None


def weekly_history(
    daily: pd.DataFrame,
    today: date | None = None,
    weeks: int = WEEKLY_HISTORY_WEEKS,
    through: date | None = None,
) -> list[WeeklyBar]:
    """The chart series for one symbol: completed weekly bars, newest last.

    `through` caps the newest bar returned, and is what actually holds the
    invariant that the chart agrees with the scan row beside it. Deriving the
    cutoff from `today` alone is not enough: the caller evaluates signals at one
    moment and builds this series minutes later, so a run straddling local
    midnight would evaluate with today=Friday (dropping the week) and then build
    bars with today=Saturday (keeping it), writing a chart a week ahead of every
    row from the same scan. Passing the scan's resolved `as_of` removes that
    dependence on wall-clock timing entirely.

    The stochastic and the moving averages are computed over the *whole*
    available history and only then sliced to `weeks`. Slicing first would leave
    the oldest 12 bars of the chart undefined, and would make a bar's value
    depend on how much history happened to be fetched.
    """
    if daily.empty:
        return []

    daily = daily.sort_index()
    weekly = completed_weekly_bars(daily, today=today)
    if through is not None:
        # Truncate before the indicators are computed so a trimmed bar cannot
        # influence the ones that remain.
        weekly = weekly.loc[weekly.index <= pd.Timestamp(through)]
    if weekly.empty:
        return []

    stoch = slow_stochastic(weekly)
    close = daily["Close"]
    # §8.2 overlays the daily SMA50/200 that §4's trend filter is defined on, not
    # an average of weekly closes. Each weekly bar is labelled with its Friday,
    # which may be a holiday or fall past the last session, so `ffill` takes the
    # last daily session at or before the label — the same reading `evaluate`
    # uses for the current week.
    sma50 = sma(close, SMA_FAST).reindex(weekly.index, method="ffill")
    sma200 = sma(close, SMA_SLOW).reindex(weekly.index, method="ffill")

    bars: list[WeeklyBar] = []
    for timestamp, row in weekly.tail(weeks).iterrows():
        ohlc = [_finite(row[column]) for column in ("Open", "High", "Low", "Close")]
        if any(value is None for value in ohlc):
            continue  # a bar without a full quote is not a candle worth drawing
        bar_open, bar_high, bar_low, bar_close = ohlc
        bars.append(
            WeeklyBar(
                week_ending=timestamp.date(),
                open=bar_open,
                high=bar_high,
                low=bar_low,
                close=bar_close,
                volume=_finite(row["Volume"]),
                slow_k=_finite(stoch["slow_k"].get(timestamp)),
                d=_finite(stoch["d"].get(timestamp)),
                sma50=_finite(sma50.get(timestamp)),
                sma200=_finite(sma200.get(timestamp)),
            )
        )
    return bars


def daily_trend(daily: pd.DataFrame) -> DailyTrend:
    """The §4 trend filter and its exit condition at the latest daily session.

    Entry and exit are deliberately *not* complements. `trend_pass` needs the
    close above both averages with the 50 above the 200; `trend_broken` needs
    the close below the 200 or the 50 below it. A close that has slipped under
    the 50-day but is still well above the 200 satisfies neither — it is no
    longer an entry and not yet an exit, and §4 leaves that band as a hold on
    purpose. Deriving one from the other would collapse it.

    The reading is taken from the last session in the frame, whatever it is. The
    daily job runs after the US close by design, so that session is a settled
    one; a run started mid-session marks against a bar still being written,
    which is why every alert and mark records the session date it used.
    """
    if daily.empty:
        raise InsufficientHistory("no daily bars")

    daily = daily.sort_index()
    if len(daily) < MIN_DAILY_BARS:
        raise InsufficientHistory(f"{len(daily)} daily bars, need {MIN_DAILY_BARS}")

    close_series = daily["Close"]
    close = float(close_series.iloc[-1])
    sma50 = float(sma(close_series, SMA_FAST).iloc[-1])
    sma200 = float(sma(close_series, SMA_SLOW).iloc[-1])

    if not all(np.isfinite([close, sma50, sma200])):
        raise InsufficientHistory("trend inputs contain NaN")

    return DailyTrend(
        as_of_date=daily.index[-1].date(),
        close=close,
        sma50=sma50,
        sma200=sma200,
        trend_pass=bool(close > sma50 and close > sma200 and sma50 > sma200),
        trend_broken=bool(close < sma200 or sma50 < sma200),
    )


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
    sma50_series = sma(close, SMA_FAST)
    sma50 = float(sma50_series.iloc[-1])
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
    # Each session's close against that session's own SMA50. MIN_DAILY_BARS
    # (200) guarantees the SMA is defined across the whole 60-session tail.
    share_above = float((close > sma50_series).tail(SHARE_SESSIONS).mean())

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
        share_above_sma50_60d=share_above,
        weeks_since_cross_up=weeks_since_cross_up(slow_k, stoch["d"]),
    )
