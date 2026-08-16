"""§5's synthetic LEAP overlay: pricing, the closed-form strike, friction and
the premium stop (SPEC-BACKTEST.md §5).

`leaps_scanner.options` stays the reference implementation, exactly as
`indicators.py` is for §4: §8 puts the call price and the inverse normal CDF
*there*, beside `norm_cdf` and `bs_delta`, and this module imports them. The
Black-Scholes formulas are therefore written down once in the package, and
nothing here restates them. What this module adds is §5's economics — which
inputs a trade is priced from, how the 0.70Δ strike is inverted, what the
haircut does, and when the premium stop fires.

**The overlay is a track, not an annotation.** §4.4 gives `premium_stop` to the
LEAP overlay alone, so an overlay position can exit before the stock position
opened on the same signal; P8 then scopes "one open trade" per *track*, so the
chain frees up earlier and the overlay may take an entry the stock track was
still holding through. The two trade sets differ in count and in dates by
design. `engine.PositionOverlay` is the seam: the engine replays its own loop
once per configuration and this module supplies the vehicle.

Two readings of §5.1 that the spec leaves implicit, stated here rather than
buried in the code:

* **σ reads closes strictly before the fill.** §5.1 says the inputs are taken
  at the fill date with S = the fill open. The fill happens at that session's
  *open*, so that session's own close is not knowable yet; RV252 therefore ends
  at the previous session's close. Using the fill day's close would be intraday
  look-ahead — one day out of 252, but acceptance 4's property is not a matter
  of materiality. `r` is different and is taken on the entry date itself,
  because §5.1/P7 pin it there in so many words ("^IRX/100 on the entry date").
* **q's trailing window is measured from the fill date**, half-open on the left:
  payments dated in `(fill − 365 days, fill]`. §5.1 lists q among the inputs
  "at entry (fill date)", so the fill date governs; half-open is what makes an
  anniversary payment count once rather than twice.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import pandas as pd

from leaps_scanner import options, risk
from leaps_scanner.backtest import data
from leaps_scanner.backtest.engine import Panel, SkippedEntry, Trade

logger = logging.getLogger(__name__)

# P3: a fixed one-year tenor at entry. §4.4's time exit (DTE < 180) therefore
# first bites at 186 calendar days held, which `engine.TIME_EXIT_DAYS` pins.
TENOR_DAYS = 365
DAYS_PER_YEAR = options.DAYS_PER_YEAR

# Decision 5 / P5: the base configuration. Both are varied one at a time by
# §5.6's grid, and neither touches §4's signals or the stock track.
BASE_SIGMA_MULTIPLIER = 1.1
BASE_FRICTION = 0.04

# §5.2's target delta is SPEC.md §5's, not a new number. §5.4's stop threshold
# is not restated at all: `risk.is_premium_stopped` owns it, and `premium_stopped`
# below calls it rather than comparing against a copy that could drift.
TARGET_DELTA = options.TARGET_DELTA

# P6: q is the trailing 365 calendar days of cash dividends per share.
DIVIDEND_LOOKBACK_DAYS = 365

# The largest argument `math.exp` can take before it raises rather than returns
# (e^709 is finite in float64, e^710 is not). Used to turn an unpriceable input
# into a counted skip instead of an exception — see `target_strike`.
_MAX_EXPONENT = 709.0

# Why an overlay entry was not taken. Distinct from §4.3's `no_fill` /
# `halt_too_long`, which the same skipped list also carries: these say the
# signal was tradeable but the *vehicle* could not be priced.
SKIP_DELTA_UNREACHABLE = "overlay_delta_unreachable"  # §5.2's domain guard
SKIP_NO_VOLATILITY = "overlay_no_volatility"
SKIP_NO_RATE = "overlay_no_rate"
SKIP_UNPRICEABLE = "overlay_unpriceable"


@dataclass(frozen=True)
class OverlayConfig:
    """One §5.6 configuration: a zone variant, a σ multiplier and a haircut."""

    name: str
    zone: str = "base"
    sigma_multiplier: float = BASE_SIGMA_MULTIPLIER
    friction: float = BASE_FRICTION


# §5.6: five configurations, one-at-a-time around the base case, plus P13's
# Strict-zone variant at base parameters. Six replays in all — the panel and the
# §4 signals behind them are computed once and shared (§5.6's scope rule).
CONFIGS: tuple[OverlayConfig, ...] = (
    OverlayConfig("base"),
    OverlayConfig("m1.0", sigma_multiplier=1.0),
    OverlayConfig("m1.2", sigma_multiplier=1.2),
    OverlayConfig("h0.00", friction=0.0),
    OverlayConfig("h0.06", friction=0.06),
    OverlayConfig("strict", zone="strict"),
)


@dataclass(frozen=True)
class SyntheticPosition:
    """One open synthetic LEAP: §5.1's inputs, frozen for the trade's life."""

    entry_date: date
    expiry: date
    spot: float
    strike: float
    sigma: float
    rate: float
    dividend_yield: float
    entry_cost: float

    def mid(self, spot: float, day: date) -> float:
        """§5.4's model mid at `day`, with σ, r and q held flat.

        Past expiry the Black-Scholes form is undefined and the contract is
        worth its intrinsic value; §4.4's time exit means a trade should never
        get there, but a forced `end_of_window` mark on a series whose tail is
        untradeable could, and an exception at that point would lose the trade.
        """
        t_years = (self.expiry - day).days / DAYS_PER_YEAR
        if t_years <= 0:
            return max(spot - self.strike, 0.0)
        price = options.bs_call_price(
            spot, self.strike, t_years, self.rate, self.dividend_yield, self.sigma
        )
        # The two discounted terms are each non-negative and their difference is
        # too, up to a rounding step deep out of the money.
        return max(price, 0.0)


@dataclass(frozen=True)
class SyntheticTrade:
    """A completed overlay trade, beside the share hold over the same window.

    `trade` is not decoration: §6.1's vehicle alpha is `r_overlay − r_hold` with
    `r_hold` measured entry-fill → exit-fill *of this trade*, and because the
    premium stop moves the exit, that window is not the stock track's window for
    the same signal. Carrying the stock leg here is what makes the comparison
    the one §6.1 asks for rather than a join against a different trade.
    """

    trade: Trade
    strike: float
    sigma: float
    rate: float
    dividend_yield: float
    expiry: date
    entry_cost: float
    exit_value: float

    @property
    def r_overlay(self) -> float:
        """§5.5: exit_value / entry_cost − 1."""
        return self.exit_value / self.entry_cost - 1.0

    @property
    def vehicle_alpha(self) -> float:
        """§6.1: what the option vehicle added over owning the shares."""
        return self.r_overlay - self.trade.r_trade

    def as_dict(self) -> dict:
        return {
            **self.trade.as_dict(),
            "strike": self.strike,
            "sigma": self.sigma,
            "rate": self.rate,
            "dividend_yield": self.dividend_yield,
            "expiry": self.expiry.isoformat(),
            "entry_cost": self.entry_cost,
            "exit_value": self.exit_value,
            "r_overlay": self.r_overlay,
            "vehicle_alpha": self.vehicle_alpha,
        }


def target_strike(
    spot: float,
    sigma: float,
    rate: float,
    dividend_yield: float,
    t_years: float,
    *,
    target: float = TARGET_DELTA,
) -> float | None:
    """§5.2's closed-form 0.70Δ strike. None when the domain guard fires.

        d1* = N⁻¹(0.70 · e^(qT₀))
        K   = S · exp(−(d1*·σ√T₀ − (r − q + σ²/2)·T₀))

    The guard is `0.70 · e^(qT₀) ≥ 1` — a delta of `e^(−qT)·N(d1)` cannot reach
    0.70 once the carry alone has eaten the difference (q ≥ ln(1/0.70) ≈ 35.7%),
    so there is no strike to solve for. It is close to reachable on real data:
    the highest trailing-365-day yield in the ten-year universe is about 34.9%,
    a spin-off distorting the dividend sum rather than a real payout.

    Both exponents are checked for representability before `math.exp` evaluates
    them, because past about 709 it raises `OverflowError` rather than returning
    a large number. A corrupt dividend row — a $100 payment recorded against a
    12-cent spot is the shape yfinance actually produces — puts q in the
    hundreds, and discovering "no strike exists" by way of an exception would
    abort the whole ten-year run instead of skipping one trade, which is the
    opposite of what this function's None means and of `options._finite`'s house
    rule: one bad cell costs one contract, never the scan. The checks bound only
    that absurd region; the real guard below is still the spec's own
    `0.70 · e^(qT₀) ≥ 1`, tested on the probability exactly as written, so the
    threshold itself is unmoved to the last bit.
    """
    if spot <= 0 or sigma <= 0 or t_years <= 0:
        return None
    carry = dividend_yield * t_years
    if carry >= _MAX_EXPONENT:
        return None
    probability = target * math.exp(carry)
    if probability >= 1.0:
        return None
    d1_star = options.inv_norm_cdf(probability)
    exponent = -(
        d1_star * sigma * math.sqrt(t_years) - (rate - dividend_yield + sigma**2 / 2) * t_years
    )
    if exponent > _MAX_EXPONENT:
        return None
    return spot * math.exp(exponent)


class MarketInputs:
    """§5.1's r and q, read from the cache once and answered per trade.

    Shared by every configuration: r and q depend on the trade, not on m or h,
    so the six replays ask the same questions and get the same answers. RV252 is
    memoized per chain for the same reason — the configurations' trade sets
    diverge, but they overlap heavily, and the divergence is in the exits.
    """

    def __init__(self, cache: data.PriceCache) -> None:
        self._cache = cache
        self._rate_dates, self._rate_values = _rate_series(cache)
        self._chain: str | None = None
        self._dividends: tuple[tuple[date, float], ...] = ()
        self._volatility: dict[int, float | None] = {}

    def use_chain(self, chain: str) -> None:
        """Point the per-chain caches at `chain`, if they are not already."""
        if chain == self._chain:
            return
        self._chain = chain
        # Keyed by the rename chain, exactly as §4.3a keys prices: Yahoo serves a
        # renamed company's whole dividend history under its current ticker.
        self._dividends = _dividend_payments(self._cache.load_dividends(chain))
        self._volatility = {}

    def rate(self, day: date) -> float | None:
        """P7: ^IRX/100, last print at or before `day`. May be negative."""
        position = int(np.searchsorted(self._rate_dates, np.datetime64(day), side="right")) - 1
        if position < 0:
            return None
        return float(self._rate_values[position])

    def dividend_yield(self, day: date, spot: float) -> float:
        """P6: trailing 365-day cash dividends per share ÷ spot.

        A chain with no dividend file has never paid one on this basis, and zero
        is the fact rather than a stand-in for missing data: `PriceCache.store`
        writes the file whenever yfinance returned the actions column at all.
        """
        if spot <= 0:
            return 0.0
        floor = day - timedelta(days=DIVIDEND_LOOKBACK_DAYS)
        paid = sum(amount for when, amount in self._dividends if floor < when <= day)
        return paid / spot

    def realized_vol(self, panel: Panel, index: int) -> float | None:
        """§5.1's RV252 from the closes *before* session `index` (see module docstring).

        Memoized by session index alone, which is sound because `engine.run`
        builds one panel per chain and `use_chain` clears the memo when the
        chain turns over. Two different panels for one chain would collide.
        """
        if index in self._volatility:
            return self._volatility[index]
        closes = panel.close[:index]
        value = (
            options.realized_vol_20d(pd.Series(closes), window=data.RV_SESSIONS)
            if closes.size > data.RV_SESSIONS
            else None
        )
        if value is not None and not (math.isfinite(value) and value > 0):
            # A perfectly flat 252 sessions gives zero vol, which prices nothing.
            value = None
        self._volatility[index] = value
        return value


def _rate_series(cache: data.PriceCache) -> tuple[np.ndarray, np.ndarray]:
    """^IRX as sorted (date, rate) arrays, undefined prints dropped.

    Dropping them is what makes "last available print" true by construction: a
    bisect then lands on the last date that actually quoted. The rate is *not*
    floored at zero — ^IRX printed negative in March 2020, and clamping it would
    be inventing data rather than reporting it (§7's honesty rule).
    """
    frame = cache.load_rates()
    if frame is None or frame.empty or "Close" not in frame:
        logger.warning("%s is not cached; §5's r has no source", data.RATE_SYMBOL)
        return np.array([], dtype="datetime64[D]"), np.array([], dtype="float64")

    frame = frame.sort_index()
    values = frame["Close"].to_numpy(dtype="float64") / 100.0
    usable = np.isfinite(values)
    dates = pd.DatetimeIndex(frame.index).to_numpy(dtype="datetime64[D]")
    return dates[usable], values[usable]


def _dividend_payments(frame: pd.DataFrame | None) -> tuple[tuple[date, float], ...]:
    """The cached dividend rows as (date, amount), positive amounts only."""
    if frame is None or frame.empty or data.DIVIDEND_COLUMN not in frame:
        return ()
    frame = frame.sort_index()
    amounts = frame[data.DIVIDEND_COLUMN].to_numpy(dtype="float64")
    stamps = pd.DatetimeIndex(frame.index)
    return tuple(
        (stamp.date(), float(amount))
        for stamp, amount in zip(stamps, amounts, strict=True)
        if np.isfinite(amount) and amount > 0
    )


class LeapOverlay:
    """§5's overlay for one configuration, driven by `engine._replay`.

    One instance runs the whole universe: chains are replayed one after another,
    so the open position and the per-chain inputs are reset at each `begin_replay`
    and the finished trades accumulate across all of them.
    """

    def __init__(self, config: OverlayConfig, inputs: MarketInputs) -> None:
        self.config = config
        self.name = config.name
        self.zone = config.zone
        self._inputs = inputs
        self._position: SyntheticPosition | None = None
        self._trades: list[SyntheticTrade] = []
        self._skipped: list[SkippedEntry] = []

    # -- engine.PositionOverlay -------------------------------------------

    def begin_replay(self, panel: Panel) -> None:
        self._position = None
        self._inputs.use_chain(panel.chain)

    def open_position(self, panel: Panel, index: int) -> str | None:
        """§5.1-5.3: price the entry, or say why the vehicle cannot be."""
        entry_date = panel.session_dates[index]
        spot = float(panel.open_[index])  # §4.3's fill; `_next_fill` guarantees it is tradeable

        volatility = self._inputs.realized_vol(panel, index)
        if volatility is None:
            return SKIP_NO_VOLATILITY
        rate = self._inputs.rate(entry_date)
        if rate is None:
            return SKIP_NO_RATE

        sigma = self.config.sigma_multiplier * volatility
        dividend_yield = self._inputs.dividend_yield(entry_date, spot)
        tenor = TENOR_DAYS / DAYS_PER_YEAR

        strike = target_strike(spot, sigma, rate, dividend_yield, tenor)
        if strike is None or not (math.isfinite(strike) and strike > 0):
            return SKIP_DELTA_UNREACHABLE

        entry_cost = options.bs_call_price(spot, strike, tenor, rate, dividend_yield, sigma) * (
            1 + self.config.friction
        )
        if not (math.isfinite(entry_cost) and entry_cost > 0):
            # The divisor of every return this trade would report. A trade
            # priced at nothing is not a cheap trade, it is an unusable model
            # output, and dividing by it would poison §6.1's whole table.
            return SKIP_UNPRICEABLE

        self._position = SyntheticPosition(
            entry_date=entry_date,
            expiry=entry_date + timedelta(days=TENOR_DAYS),
            spot=spot,
            strike=strike,
            sigma=sigma,
            rate=rate,
            dividend_yield=dividend_yield,
            entry_cost=entry_cost,
        )
        return None

    def premium_stopped(self, panel: Panel, index: int) -> bool:
        """§5.4 / §4.4: model mid at this close ≤ 50% of the premium paid.

        Delegated to `risk.is_premium_stopped` so the live comparison and the
        simulated one cannot drift apart — including its `≤`, where a mark
        landing exactly on the stop is a stop.
        """
        position = self._position
        if position is None:
            return False
        spot = float(panel.close[index])
        if not (math.isfinite(spot) and spot > 0):
            # A quoted session with no usable close marks nothing; the position
            # runs on. Inventing a mark here would fire or suppress a stop on
            # the strength of a data gap.
            return False
        mid = position.mid(spot, panel.session_dates[index])
        return risk.is_premium_stopped(position.entry_cost, mid)

    def record_exit(self, trade: Trade) -> None:
        """§5.5: the exit fill, haircut the other way."""
        position = self._position
        self._position = None
        if position is None:  # pragma: no cover - the engine only closes what it opened
            logger.warning("%s: exit recorded with no open overlay position", self.name)
            return

        gross = position.mid(trade.exit_price, trade.exit_date)
        self._trades.append(
            SyntheticTrade(
                trade=trade,
                strike=position.strike,
                sigma=position.sigma,
                rate=position.rate,
                dividend_yield=position.dividend_yield,
                expiry=position.expiry,
                entry_cost=position.entry_cost,
                exit_value=gross * (1 - self.config.friction),
            )
        )

    def record_skipped(self, entries: Sequence[SkippedEntry]) -> None:
        self._skipped.extend(entries)

    # -- results ------------------------------------------------------------

    @property
    def trades(self) -> tuple[SyntheticTrade, ...]:
        """Every completed overlay trade, in the engine's deterministic order."""
        return tuple(
            sorted(self._trades, key=lambda item: (item.trade.entry_date, item.trade.chain))
        )

    @property
    def skipped(self) -> tuple[SkippedEntry, ...]:
        return tuple(sorted(self._skipped, key=lambda item: (item.signal_date, item.chain)))


def build_overlays(
    cache: data.PriceCache, configs: Sequence[OverlayConfig] = CONFIGS
) -> tuple[LeapOverlay, ...]:
    """One overlay per §5.6 configuration, sharing one set of market inputs."""
    inputs = MarketInputs(cache)
    return tuple(LeapOverlay(config, inputs) for config in configs)
