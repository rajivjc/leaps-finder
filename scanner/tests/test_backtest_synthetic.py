"""B3 tests: SPEC-BACKTEST.md §5 and §9, acceptance criterion 3.

The load-bearing one is `TestGoldenTrade`. Everything §5 computes for a trade —
σ, q, the strike, the entry premium, the daily mids, the exit value and both
tracks' P&L — is derived there a second time, from the spec's formulas alone,
using nothing from `synthetic.py`; the values are then pinned as literals so a
change of behaviour cannot quietly re-bless itself. The price path is chosen so
that even RV252 has a closed form: its log returns repeat with a period that
divides 252, so any trailing 252-session window holds the same multiset of
returns and its sample standard deviation can be written down.
"""

from __future__ import annotations

import dataclasses
import json
import math
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from test_backtest_engine import (
    build_fixture,
    business_days,
    fixture_frames,
    price_frame,
    rate_frame,
)

from leaps_scanner import options, risk
from leaps_scanner.backtest import data, engine, metrics, synthetic
from leaps_scanner.backtest.membership import Membership

# -- the golden trade's universe ------------------------------------------
#
# A sawtooth in log space: 14 sessions of +0.8% then 7 of −1.4%, repeating.
# 252 = 12 × 21, so every trailing 252-session window contains exactly 168 up
# returns and 84 down ones whatever session it ends on — which is what makes
# RV252 a constant this test can write down rather than measure.
GOLDEN_UP = 0.008
GOLDEN_DOWN = 0.014
GOLDEN_UPS = 14
GOLDEN_PERIOD = 21
GOLDEN_BASE = 100.0

GOLDEN_FETCH_START = date(2015, 1, 1)
GOLDEN_END = date(2018, 12, 31)
# Opens at the first eligible date (§3.1's warm-up lands 2015-12-23) and closes
# after the one trade the window contains has exited.
GOLDEN_WINDOW = data.Window(
    start=date(2016, 3, 1), end=date(2016, 10, 31), fetch_start=GOLDEN_FETCH_START
)
GOLDEN_RATE_PERCENT = 0.75  # ^IRX quotes ×100, so r = 0.0075 exactly

# Five payments around the entry fill of 2016-03-14: three inside P6's trailing
# window and two outside it, so q is 3 × 0.60 ÷ the fill open. The exact
# `entry − 365` boundary is a Sunday here and so cannot be a session; it is
# pinned in `TestMarketInputs` instead, on a frame with dates of its own.
GOLDEN_DIVIDEND = 0.60
GOLDEN_DIVIDEND_DATES = (
    date(2015, 3, 13),  # more than 365 days before the fill: outside
    date(2015, 6, 15),
    date(2015, 12, 15),
    date(2016, 3, 14),  # on the fill date itself: inside
    date(2016, 4, 15),  # after the fill: outside
)
GOLDEN_MEMBERSHIP = """\
# Fixture membership for the B3 overlay tests. Not the shipped file.
symbol,added,removed,price_symbol
GLD,2010-01-04,,
"""


def golden_sessions() -> list[date]:
    return business_days(GOLDEN_FETCH_START, GOLDEN_END)


def golden_closes(count: int) -> np.ndarray:
    """The closed-form path: 100 · exp(Σ of the repeating return pattern)."""
    steps = np.array(
        [GOLDEN_UP if i % GOLDEN_PERIOD < GOLDEN_UPS else -GOLDEN_DOWN for i in range(count - 1)]
    )
    return GOLDEN_BASE * np.exp(np.concatenate([[0.0], np.cumsum(steps)]))


def golden_rv252() -> float:
    """§5.1's RV252 for this path, in closed form.

    168 returns of +0.008 and 84 of −0.014 in any trailing 252, so the sample
    variance is Σ(x − x̄)²/251 over a multiset that never changes.
    """
    ups = 252 // GOLDEN_PERIOD * GOLDEN_UPS
    downs = 252 - ups
    mean = (ups * GOLDEN_UP + downs * -GOLDEN_DOWN) / 252
    variance = (ups * (GOLDEN_UP - mean) ** 2 + downs * (-GOLDEN_DOWN - mean) ** 2) / 251
    return math.sqrt(variance) * math.sqrt(252)


def golden_frame() -> pd.DataFrame:
    """The chain's series. The open is the previous close, exactly, so a §4.3
    fill price is a close this test can compute rather than look up."""
    days = golden_sessions()
    close = golden_closes(len(days))
    open_ = np.empty_like(close)
    open_[0] = close[0]
    open_[1:] = close[:-1]

    payments = np.zeros(len(days))
    for when in GOLDEN_DIVIDEND_DATES:
        payments[days.index(when)] = GOLDEN_DIVIDEND

    return pd.DataFrame(
        {
            "Open": open_,
            "High": np.maximum(open_, close),
            "Low": np.minimum(open_, close),
            "Close": close,
            "Adj Close": close,
            "Volume": np.full(len(days), 1_000_000.0),
            data.DIVIDEND_COLUMN: payments,
        },
        index=pd.DatetimeIndex([pd.Timestamp(day) for day in days]),
    )


def golden_universe(root: Path) -> tuple[Membership, data.PriceCache]:
    days = golden_sessions()
    root.mkdir(parents=True, exist_ok=True)
    path = root / "membership.csv"
    path.write_text(GOLDEN_MEMBERSHIP, encoding="utf-8")
    cache = data.PriceCache(root / "cache")
    cache.store("GLD", golden_frame())
    cache.store(data.BENCHMARK_SYMBOL, price_frame(days, base=180.0, drift=0.0004))
    cache.store(data.RATE_SYMBOL, rate_frame(days, base=GOLDEN_RATE_PERCENT, amplitude=0.0))
    return Membership.load(path), cache


def golden_overlay_trades(
    root: Path, config: synthetic.OverlayConfig
) -> tuple[synthetic.SyntheticTrade, ...]:
    membership, cache = golden_universe(root)
    (overlay,) = synthetic.build_overlays(cache, [config])
    engine.run(membership, cache, GOLDEN_WINDOW, overlays=[overlay])
    return overlay.trades


def golden_overlay_trade(root: Path, config: synthetic.OverlayConfig) -> synthetic.SyntheticTrade:
    """The window's first overlay trade, which every configuration shares.

    Only the *first* — a configuration whose premium stop fires releases the
    chain early and can take a second entry the base case never sees, which is
    the divergence P8 permits rather than a fixture defect.
    """
    trades = golden_overlay_trades(root, config)
    assert trades and trades[0].trade.entry_date == date(2016, 3, 14)
    return trades[0]


class TestGoldenTrade:
    """§9's hand-computed trade / acceptance 3, reproduced to the cent.

    The expectations below are derived from SPEC-BACKTEST.md §5 directly —
    closed-form RV252, then d1*, K, C and the two haircuts written out here in
    plain arithmetic — and cross-checked against literals. `synthetic.py` is
    never consulted for an expected value, only for the value under test.
    """

    ENTRY_DATE = date(2016, 3, 14)
    EXIT_DATE = date(2016, 9, 19)
    EXPIRY = date(2017, 3, 14)

    # The independent derivation's results, pinned. Each reconciles by hand:
    # spot 134.9859, σ = 1.1 × 16.496% = 18.146%, q = 1.80/134.9859 = 1.3335%,
    # so the 0.70Δ strike lands 8.5582% below spot at 123.4336. The call is then
    # worth 15.5474 — 11.5523 intrinsic plus 3.9951 of time value — and 189 days
    # later 14.3344, of which 12.6365 is intrinsic and only 1.6978 time value.
    # Gross, that is −7.80%; the two haircuts multiply it by 0.96/1.04, giving
    # −14.894% against a share position that made +0.803%.
    SIGMA = 0.18145660835894484
    DIVIDEND_YIELD = 0.013334727972270912
    STRIKE = 123.4335537582445
    ENTRY_COST = 16.169317339051467
    EXIT_VALUE = 13.760990146196427
    R_OVERLAY = -0.14894427157036172
    VEHICLE_ALPHA = -0.15697635707463498

    def spot_at(self, day: date) -> float:
        """The fill price, from the closed form rather than from the cache."""
        days = golden_sessions()
        return float(golden_closes(len(days))[days.index(day) - 1])

    def call(self, spot: float, t_years: float, strike: float, sigma: float, q: float) -> float:
        """§5's C, written out here: `synthetic.py` may not be asked."""
        rate = GOLDEN_RATE_PERCENT / 100
        d1 = (math.log(spot / strike) + (rate - q + sigma**2 / 2) * t_years) / (
            sigma * math.sqrt(t_years)
        )
        d2 = d1 - sigma * math.sqrt(t_years)
        return spot * math.exp(-q * t_years) * options.norm_cdf(d1) - strike * math.exp(
            -rate * t_years
        ) * options.norm_cdf(d2)

    def test_the_derivation_and_the_pinned_literals_agree(self) -> None:
        """The literals above are the derivation's output, not the code's."""
        spot = self.spot_at(self.ENTRY_DATE)
        sigma = synthetic.BASE_SIGMA_MULTIPLIER * golden_rv252()
        q = 3 * GOLDEN_DIVIDEND / spot  # three of the five payments are in window
        rate = GOLDEN_RATE_PERCENT / 100

        d1_star = options.inv_norm_cdf(0.70 * math.exp(q))
        strike = spot * math.exp(-(d1_star * sigma - (rate - q + sigma**2 / 2)))
        entry_cost = self.call(spot, 1.0, strike, sigma, q) * 1.04
        exit_spot = self.spot_at(self.EXIT_DATE)
        exit_years = (self.EXPIRY - self.EXIT_DATE).days / 365
        exit_value = self.call(exit_spot, exit_years, strike, sigma, q) * 0.96

        assert sigma == pytest.approx(self.SIGMA, rel=1e-12)
        assert q == pytest.approx(self.DIVIDEND_YIELD, rel=1e-12)
        assert strike == pytest.approx(self.STRIKE, rel=1e-12)
        assert entry_cost == pytest.approx(self.ENTRY_COST, rel=1e-12)
        assert exit_value == pytest.approx(self.EXIT_VALUE, rel=1e-12)
        assert exit_value / entry_cost - 1 == pytest.approx(self.R_OVERLAY, rel=1e-12)

    def test_the_engine_reproduces_the_trade_to_the_cent(self, tmp_path: Path) -> None:
        trades = golden_overlay_trades(tmp_path, synthetic.OverlayConfig("base"))
        assert len(trades) == 1  # the window is chosen to hold exactly one
        trade = trades[0]

        assert trade.trade.signal_date == date(2016, 3, 11)
        assert trade.trade.entry_date == self.ENTRY_DATE
        assert trade.trade.exit_date == self.EXIT_DATE
        assert trade.trade.exit_reason == "time_exit"
        assert trade.expiry == self.EXPIRY

        assert trade.sigma == pytest.approx(self.SIGMA, rel=1e-12)
        assert trade.rate == pytest.approx(GOLDEN_RATE_PERCENT / 100, rel=1e-12)
        assert trade.dividend_yield == pytest.approx(self.DIVIDEND_YIELD, rel=1e-12)
        # "To the cent" on quantities quoted per share: half a cent either way.
        assert trade.strike == pytest.approx(self.STRIKE, abs=0.005)
        assert trade.entry_cost == pytest.approx(self.ENTRY_COST, abs=0.005)
        assert trade.exit_value == pytest.approx(self.EXIT_VALUE, abs=0.005)

    def test_both_tracks_p_and_l_reproduce(self, tmp_path: Path) -> None:
        trade = golden_overlay_trade(tmp_path, synthetic.OverlayConfig("base"))
        stock_return = self.spot_at(self.EXIT_DATE) / self.spot_at(self.ENTRY_DATE) - 1

        assert trade.trade.r_trade == pytest.approx(stock_return, rel=1e-12)
        assert trade.r_overlay == pytest.approx(self.R_OVERLAY, rel=1e-9)
        assert trade.vehicle_alpha == pytest.approx(self.VEHICLE_ALPHA, rel=1e-9)
        # The whole point of the vehicle: a share position that went nowhere,
        # expressed as a call, lost 15% to decay and friction.
        assert stock_return > 0 > trade.r_overlay

    @pytest.mark.parametrize(
        "day", [date(2016, 3, 31), date(2016, 5, 16), date(2016, 7, 1), date(2016, 9, 16)]
    )
    def test_the_daily_mids_reproduce(self, tmp_path: Path, day: date) -> None:
        """§9 asks for the mids, not just the endpoints: the premium stop is
        evaluated against every one of them."""
        trade = golden_overlay_trade(tmp_path, synthetic.OverlayConfig("base"))
        position = synthetic.SyntheticPosition(
            entry_date=self.ENTRY_DATE,
            expiry=self.EXPIRY,
            spot=self.spot_at(self.ENTRY_DATE),
            strike=trade.strike,
            sigma=trade.sigma,
            rate=trade.rate,
            dividend_yield=trade.dividend_yield,
            entry_cost=trade.entry_cost,
        )
        days = golden_sessions()
        close = float(golden_closes(len(days))[days.index(day)])
        expected = self.call(
            close,
            (self.EXPIRY - day).days / 365,
            self.STRIKE,
            self.SIGMA,
            self.DIVIDEND_YIELD,
        )

        assert position.mid(close, day) == pytest.approx(expected, abs=0.005)
        # None of them halved the premium, which is why this trade timed out.
        assert not risk.is_premium_stopped(position.entry_cost, position.mid(close, day))

    def test_friction_is_exactly_four_percent_each_way(self, tmp_path: Path) -> None:
        """§9's friction arithmetic, to the cent: the same trade at h = 0."""
        base = golden_overlay_trade(tmp_path / "base", synthetic.OverlayConfig("base"))
        frictionless = golden_overlay_trade(
            tmp_path / "none", synthetic.OverlayConfig("h0.00", friction=0.0)
        )

        assert base.strike == frictionless.strike  # h moves neither σ nor K
        assert base.entry_cost == pytest.approx(frictionless.entry_cost * 1.04, abs=0.005)
        assert base.exit_value == pytest.approx(frictionless.exit_value * 0.96, abs=0.005)
        # The round trip costs 1 − 0.96/1.04 of the gross return, i.e. 7.7%.
        assert 1 + base.r_overlay == pytest.approx(
            (1 + frictionless.r_overlay) * 0.96 / 1.04, rel=1e-12
        )


class TestRealizedVolInput:
    """§5.1's σ, and the reading of "at entry" the module docstring states."""

    def test_rv252_matches_the_closed_form_everywhere_on_the_path(self) -> None:
        days = golden_sessions()
        close = pd.Series(golden_closes(len(days)))

        for end in (300, 500, 800):
            assert options.realized_vol_20d(close.iloc[:end], window=252) == pytest.approx(
                golden_rv252(), rel=1e-12
            )

    def test_volatility_reads_only_closes_before_the_fill(self, tmp_path: Path) -> None:
        """The fill happens at the open, so that session's close is not known
        yet. Changing it must not move σ — changing the one before must."""
        _, cache = golden_universe(tmp_path)
        frame = cache.load("GLD")
        panel = engine.build_panel("GLD", frame)
        assert panel is not None
        inputs = synthetic.MarketInputs(cache)
        inputs.use_chain("GLD")
        index = 400
        baseline = inputs.realized_vol(panel, index)

        # The panel's arrays are parquet-backed and read-only, so the perturbed
        # run gets a copy — of the close array only, which is all σ reads.
        moved = engine.build_panel("GLD", frame)
        assert moved is not None
        moved = dataclasses.replace(moved, close=np.array(moved.close, dtype="float64"))
        moved.close[index] *= 1.5
        shifted = synthetic.MarketInputs(cache)
        shifted.use_chain("GLD")
        assert shifted.realized_vol(moved, index) == baseline

        moved.close[index - 1] *= 1.5
        later = synthetic.MarketInputs(cache)
        later.use_chain("GLD")
        assert later.realized_vol(moved, index) != baseline

    def test_too_little_history_yields_no_volatility(self, tmp_path: Path) -> None:
        _, cache = golden_universe(tmp_path)
        panel = engine.build_panel("GLD", cache.load("GLD"))
        assert panel is not None
        inputs = synthetic.MarketInputs(cache)
        inputs.use_chain("GLD")

        assert inputs.realized_vol(panel, 252) is None  # 252 closes, 251 returns
        assert inputs.realized_vol(panel, 253) is not None


class TestTargetStrike:
    """§5.2: the closed form, and the domain guard that bounds it."""

    @pytest.mark.parametrize("spot", [17.5, 100.0, 1840.0])
    @pytest.mark.parametrize("sigma", [0.08, 0.25, 0.90])
    @pytest.mark.parametrize(
        ("rate", "dividend_yield"), [(0.0, 0.0), (0.052, 0.031), (-0.001, 0.2)]
    )
    def test_the_strike_it_returns_has_delta_exactly_070(
        self, spot: float, sigma: float, rate: float, dividend_yield: float
    ) -> None:
        """§9's round trip. The tolerance is 1e-9; it lands near 1e-16."""
        strike = synthetic.target_strike(spot, sigma, rate, dividend_yield, 1.0)
        assert strike is not None

        delta = options.bs_delta(spot, strike, 1.0, rate, dividend_yield, sigma)

        assert delta == pytest.approx(0.70, abs=1e-9)

    def test_a_shorter_tenor_round_trips_too(self) -> None:
        strike = synthetic.target_strike(100.0, 0.3, 0.02, 0.01, 0.5)
        assert strike is not None

        assert options.bs_delta(100.0, strike, 0.5, 0.02, 0.01, 0.3) == pytest.approx(
            0.70, abs=1e-9
        )

    def test_the_domain_guard_fires_above_the_carry_threshold(self) -> None:
        """§5.2: no strike reaches 0.70Δ once q ≥ ln(1/0.70) ≈ 35.67%."""
        threshold = math.log(1 / 0.70)

        assert synthetic.target_strike(100.0, 0.3, 0.02, threshold - 1e-6, 1.0) is not None
        assert synthetic.target_strike(100.0, 0.3, 0.02, threshold, 1.0) is None
        assert synthetic.target_strike(100.0, 0.3, 0.02, 0.50, 1.0) is None

    @pytest.mark.parametrize(
        ("spot", "sigma", "t_years"), [(0.0, 0.3, 1.0), (100.0, 0.0, 1.0), (100.0, 0.3, 0.0)]
    )
    def test_degenerate_inputs_yield_no_strike(self, spot, sigma, t_years) -> None:
        assert synthetic.target_strike(spot, sigma, 0.02, 0.0, t_years) is None


class TestMarketInputs:
    """§5.1's r and q, as read from the cache."""

    def test_the_rate_is_the_last_print_at_or_before_the_day(self, tmp_path: Path) -> None:
        cache = data.PriceCache(tmp_path / "cache")
        frame = pd.DataFrame(
            {"Open": [1.0, 2.0, 3.0], "Close": [1.0, 2.0, 3.0]},
            index=pd.DatetimeIndex(["2020-01-02", "2020-01-06", "2020-01-07"]),
        )
        cache.store(data.RATE_SYMBOL, frame)
        inputs = synthetic.MarketInputs(cache)

        assert inputs.rate(date(2020, 1, 1)) is None  # nothing has printed yet
        assert inputs.rate(date(2020, 1, 2)) == pytest.approx(0.01)
        assert inputs.rate(date(2020, 1, 5)) == pytest.approx(0.01)  # holiday: last print
        assert inputs.rate(date(2020, 1, 7)) == pytest.approx(0.03)
        assert inputs.rate(date(2021, 1, 1)) == pytest.approx(0.03)  # runs on past the end

    def test_a_negative_rate_is_carried_through(self, tmp_path: Path) -> None:
        """^IRX printed −0.105 in March 2020. Clamping it to zero would be
        inventing data; §5's forms handle it."""
        cache = data.PriceCache(tmp_path / "cache")
        cache.store(
            data.RATE_SYMBOL,
            pd.DataFrame({"Close": [-0.105]}, index=pd.DatetimeIndex(["2020-03-25"])),
        )

        assert synthetic.MarketInputs(cache).rate(date(2020, 3, 26)) == pytest.approx(-0.00105)

    def test_an_undefined_print_falls_back_to_the_previous_one(self, tmp_path: Path) -> None:
        cache = data.PriceCache(tmp_path / "cache")
        cache.store(
            data.RATE_SYMBOL,
            pd.DataFrame(
                {"Close": [1.5, float("nan")]},
                index=pd.DatetimeIndex(["2020-01-02", "2020-01-03"]),
            ),
        )

        assert synthetic.MarketInputs(cache).rate(date(2020, 1, 3)) == pytest.approx(0.015)

    def test_a_missing_rate_series_leaves_the_rate_undefined(self, tmp_path: Path) -> None:
        inputs = synthetic.MarketInputs(data.PriceCache(tmp_path / "cache"))

        assert inputs.rate(date(2020, 1, 2)) is None

    def test_the_dividend_window_is_half_open_on_the_left(self, tmp_path: Path) -> None:
        """P6's trailing 365 days, measured from the fill date: a payment
        exactly 365 days back is out, one on the day itself is in.

        A frame of its own rather than the golden universe's, because the exact
        `day − 365` boundary has to be a row and in the golden calendar it lands
        on a Sunday.
        """
        day = date(2016, 3, 14)
        cache = data.PriceCache(tmp_path / "cache")
        payments = {
            day - timedelta(days=366): 5.0,  # outside
            day - timedelta(days=365): 4.0,  # exactly at the edge: outside
            day - timedelta(days=364): 3.0,  # just inside
            day: 2.0,  # the fill date itself: inside
            day + timedelta(days=1): 9.0,  # ahead of the fill: outside
        }
        cache.store(
            "XYZ",
            pd.DataFrame(
                {"Close": [1.0] * len(payments), data.DIVIDEND_COLUMN: list(payments.values())},
                index=pd.DatetimeIndex([pd.Timestamp(when) for when in payments]),
            ),
        )
        inputs = synthetic.MarketInputs(cache)
        inputs.use_chain("XYZ")

        assert inputs.dividend_yield(day, 100.0) == pytest.approx(0.05)  # 3.0 + 2.0
        # A day earlier the fill-date payment has not happened, and the 365-day
        # boundary has slid back far enough to admit the 4.0.
        assert inputs.dividend_yield(day - timedelta(days=1), 100.0) == pytest.approx(0.07)

    def test_a_chain_that_pays_nothing_has_a_zero_yield(self, tmp_path: Path) -> None:
        cache = data.PriceCache(tmp_path / "cache")
        cache.store("XYZ", price_frame(business_days(date(2020, 1, 1), date(2020, 6, 30))))
        inputs = synthetic.MarketInputs(cache)
        inputs.use_chain("XYZ")

        assert inputs.dividend_yield(date(2020, 6, 1), 50.0) == 0.0

    def test_switching_chains_switches_the_dividend_history(self, tmp_path: Path) -> None:
        """One `MarketInputs` serves every configuration and every chain; its
        per-chain caches must not outlive the chain."""
        _, cache = golden_universe(tmp_path)
        cache.store("XYZ", price_frame(business_days(date(2015, 1, 1), date(2016, 12, 31))))
        inputs = synthetic.MarketInputs(cache)

        inputs.use_chain("GLD")
        assert inputs.dividend_yield(date(2016, 3, 14), 100.0) > 0
        inputs.use_chain("XYZ")
        assert inputs.dividend_yield(date(2016, 3, 14), 100.0) == 0.0


class TestPremiumStop:
    """§5.4 / §4.4's overlay-only exit."""

    def position(self, entry_cost: float) -> synthetic.SyntheticPosition:
        return synthetic.SyntheticPosition(
            entry_date=date(2020, 1, 2),
            expiry=date(2021, 1, 1),
            spot=100.0,
            strike=90.0,
            sigma=0.3,
            rate=0.02,
            dividend_yield=0.0,
            entry_cost=entry_cost,
        )

    def test_a_mark_exactly_on_the_stop_is_a_stop(self) -> None:
        """`risk.is_premium_stopped` writes `≤`, and this must agree with it."""
        assert risk.is_premium_stopped(10.0, 5.0) is True
        assert risk.is_premium_stopped(10.0, 5.000001) is False

    def test_the_stop_fires_only_when_the_mid_has_halved(self) -> None:
        panel_dates = [date(2020, 6, 1)]
        position = self.position(20.0)
        overlay = synthetic.LeapOverlay(synthetic.OverlayConfig("base"), _NoMarketInputs())
        overlay._position = position
        panel = _stub_panel(panel_dates, close=[position.strike])  # deep at the strike

        # Struck at 90 with the spot at 90 and half a year to run, the mid is
        # well under half of a 20.00 premium.
        assert overlay.premium_stopped(panel, 0) is True
        overlay._position = self.position(2.0)
        assert overlay.premium_stopped(panel, 0) is False

    def test_an_unusable_close_marks_nothing(self) -> None:
        overlay = synthetic.LeapOverlay(synthetic.OverlayConfig("base"), _NoMarketInputs())
        overlay._position = self.position(20.0)

        assert (
            overlay.premium_stopped(_stub_panel([date(2020, 6, 1)], close=[math.nan]), 0) is False
        )
        assert overlay.premium_stopped(_stub_panel([date(2020, 6, 1)], close=[0.0]), 0) is False

    def test_no_position_means_no_stop(self) -> None:
        overlay = synthetic.LeapOverlay(synthetic.OverlayConfig("base"), _NoMarketInputs())

        assert overlay.premium_stopped(_stub_panel([date(2020, 6, 1)], close=[10.0]), 0) is False

    def test_past_expiry_the_mid_is_intrinsic(self) -> None:
        position = self.position(20.0)

        assert position.mid(120.0, date(2021, 6, 1)) == pytest.approx(30.0)
        assert position.mid(50.0, date(2021, 6, 1)) == 0.0


class TestExitPriority:
    """§4.4's ordering, now that `premium_stop` is in it."""

    def test_premium_stop_sits_between_the_stochastic_and_the_time_exit(self) -> None:
        assert engine.EXIT_PRIORITY == (
            "trend_break",
            "stoch_below_20",
            "premium_stop",
            "time_exit",
        )

    def test_a_more_severe_rule_wins_and_a_less_severe_one_loses(self) -> None:
        days = [date(2020, 1, 2)]
        entry = date(2019, 1, 2)  # long enough ago that the time exit is armed

        broken = _stub_panel(days, close=[100.0], trend_broken=[True])
        assert engine._exit_reason(broken, 0, entry, premium_stopped=True) == "trend_break"

        quiet = _stub_panel(days, close=[100.0])
        assert engine._exit_reason(quiet, 0, entry, premium_stopped=True) == "premium_stop"
        assert engine._exit_reason(quiet, 0, entry, premium_stopped=False) == "time_exit"

    def test_the_stock_track_never_reports_a_premium_stop(self, tmp_path: Path) -> None:
        """It has no premium to halve; §4.4 gives the rule to the overlay only."""
        membership, cache = build_fixture(tmp_path)
        window = data.Window(
            start=date(2017, 1, 2), end=date(2020, 12, 18), fetch_start=date(2015, 1, 1)
        )

        result = engine.run(membership, cache, window)

        reasons = {trade.exit_reason for rows in result.trades.values() for trade in rows}
        assert reasons and "premium_stop" not in reasons


class TestDomainGuardInTheEngine:
    """§5.2: the overlay skips, counts it, and the stock track is untouched."""

    def universe(self, root: Path) -> tuple[Membership, data.PriceCache]:
        """The golden universe, with dividends raised past the carry threshold.

        Every payment becomes 30.00 on a spot near 135, so any trailing 365 days
        in the window holds at least two of them and q clears ln(1/0.70) ≈ 35.7%
        at every possible fill — the guard has to fire for all of them, not just
        the first, or a later entry would slip through and mask it.
        """
        membership, cache = golden_universe(root)
        frame = golden_frame()
        frame[data.DIVIDEND_COLUMN] = frame[data.DIVIDEND_COLUMN].replace(GOLDEN_DIVIDEND, 30.0)
        cache.store("GLD", frame)
        return membership, cache

    def test_the_overlay_skips_the_trade_and_says_why(self, tmp_path: Path) -> None:
        membership, cache = self.universe(tmp_path)
        (overlay,) = synthetic.build_overlays(cache, [synthetic.OverlayConfig("base")])

        result = engine.run(membership, cache, GOLDEN_WINDOW, overlays=[overlay])

        assert overlay.trades == ()
        assert overlay.skipped
        assert {entry.reason for entry in overlay.skipped} == {synthetic.SKIP_DELTA_UNREACHABLE}
        # …and the stock-track trade proceeds unaffected, in so many words.
        assert len(result.trades["base"]) == 1
        assert result.skipped["base"] == ()

    def test_the_skip_reaches_the_statistics_as_a_count(self, tmp_path: Path) -> None:
        membership, cache = self.universe(tmp_path)
        (overlay,) = synthetic.build_overlays(cache, [synthetic.OverlayConfig("base")])
        engine.run(membership, cache, GOLDEN_WINDOW, overlays=[overlay])

        stats = metrics.track_a_overlay(
            overlay.config,
            overlay.trades,
            overlay.skipped,
            metrics.BenchmarkPrices(cache.load_benchmark()),
        )

        assert stats.trades == 0
        assert set(stats.skipped_entries) == {synthetic.SKIP_DELTA_UNREACHABLE}
        assert stats.skipped_entries[synthetic.SKIP_DELTA_UNREACHABLE] >= 1
        # No trades is not a win rate of zero, nor a mean return of zero.
        assert stats.win_rate is None
        assert stats.mean_return is None
        assert stats.vehicle_alpha == {"mean": None, "median": None, "min": None, "max": None}


class TestUnpriceableEntries:
    """§5.1's inputs can be absent, and an absent input is a skip, not a guess."""

    def test_a_missing_rate_series_skips_every_overlay_entry(self, tmp_path: Path) -> None:
        """^IRX is cached today, so this path never fires on the real universe —
        which is exactly why it needs a test rather than a shrug."""
        membership, cache = golden_universe(tmp_path)
        (cache.prices_dir / "_IRX.parquet").unlink()
        (overlay,) = synthetic.build_overlays(cache, [synthetic.OverlayConfig("base")])

        result = engine.run(membership, cache, GOLDEN_WINDOW, overlays=[overlay])

        assert overlay.trades == ()
        assert {entry.reason for entry in overlay.skipped} == {synthetic.SKIP_NO_RATE}
        assert len(result.trades["base"]) == 1  # the stock track is unaffected

    def test_an_entry_before_the_first_rate_print_is_skipped(self, tmp_path: Path) -> None:
        membership, cache = golden_universe(tmp_path)
        rates = cache.load_rates()
        assert rates is not None
        cache.store(data.RATE_SYMBOL, rates.loc[pd.Timestamp("2016-06-01") :])
        (overlay,) = synthetic.build_overlays(cache, [synthetic.OverlayConfig("base")])

        engine.run(membership, cache, GOLDEN_WINDOW, overlays=[overlay])

        skipped = overlay.skipped
        assert skipped and {entry.reason for entry in skipped} == {synthetic.SKIP_NO_RATE}
        assert all(entry.signal_date < date(2016, 6, 1) for entry in skipped)
        # Once ^IRX starts printing, entries price again — the gap is a gap, not
        # a latch that disables the overlay for the rest of the run.
        assert overlay.trades
        assert all(item.trade.entry_date >= date(2016, 6, 1) for item in overlay.trades)

    def test_a_chain_without_252_sessions_of_history_is_skipped(self, tmp_path: Path) -> None:
        """§3.1's eligibility gate should make this unreachable through the
        engine; `MarketInputs` still has to answer honestly when asked."""
        _, cache = golden_universe(tmp_path)
        panel = engine.build_panel("GLD", cache.load("GLD"))
        assert panel is not None
        inputs = synthetic.MarketInputs(cache)
        overlay = synthetic.LeapOverlay(synthetic.OverlayConfig("base"), inputs)
        overlay.begin_replay(panel)

        assert overlay.open_position(panel, 100) == synthetic.SKIP_NO_VOLATILITY
        assert overlay.open_position(panel, 300) is None


class TestTrackDivergence:
    """§4.4 + P8: the overlay is its own track, with its own exits."""

    def test_the_overlay_exits_a_shared_entry_earlier_when_the_premium_halves(
        self, tmp_path: Path
    ) -> None:
        membership, cache = build_fixture(tmp_path)
        (overlay,) = synthetic.build_overlays(cache, [synthetic.OverlayConfig("base")])
        window = data.Window(
            start=date(2017, 1, 2), end=date(2020, 12, 18), fetch_start=date(2015, 1, 1)
        )

        result = engine.run(membership, cache, window, overlays=[overlay])
        stock = {(t.chain, t.signal_date): t for t in result.trades["base"]}

        stopped = [t for t in overlay.trades if t.trade.exit_reason == "premium_stop"]
        assert stopped, "the fixture must exercise the overlay-only exit"
        for item in stopped:
            counterpart = stock[(item.trade.chain, item.trade.signal_date)]
            assert counterpart.exit_reason != "premium_stop"
            assert item.trade.exit_date < counterpart.exit_date

    def test_vehicle_alpha_uses_the_overlay_trades_own_window(self, tmp_path: Path) -> None:
        """§6.1's `r_hold` is the share hold over the *overlay* trade's dates,
        which a premium stop makes shorter than the stock track's."""
        membership, cache = build_fixture(tmp_path)
        (overlay,) = synthetic.build_overlays(cache, [synthetic.OverlayConfig("base")])
        window = data.Window(
            start=date(2017, 1, 2), end=date(2020, 12, 18), fetch_start=date(2015, 1, 1)
        )
        engine.run(membership, cache, window, overlays=[overlay])

        for item in overlay.trades:
            assert item.vehicle_alpha == pytest.approx(item.r_overlay - item.trade.r_trade)

    def test_every_configuration_takes_signals_from_its_own_zone(self, tmp_path: Path) -> None:
        """§5.6's scope rule and P13's zones.

        m and h move exits, never the §4.2 entry test, so every trade a
        base-zone configuration opens must have entered on a slowK inside
        20-70 and every Strict one inside 20-55. Set *equality* across
        configurations is deliberately not asserted: a configuration whose
        premium stop fires releases the chain sooner and can take a later entry
        the others were holding through, which is exactly what P8's "per track"
        permits.
        """
        membership, cache = build_fixture(tmp_path)
        overlays = synthetic.build_overlays(cache)
        window = data.Window(
            start=date(2017, 1, 2), end=date(2020, 12, 18), fetch_start=date(2015, 1, 1)
        )
        engine.run(membership, cache, window, overlays=overlays)

        ceilings = {"base": engine.BASE_ZONE_HIGH, "strict": engine.STRICT_ZONE_HIGH}
        for overlay in overlays:
            assert overlay.trades, f"{overlay.name} produced no trades to check"
            for item in overlay.trades:
                assert 20.0 <= item.trade.entry_slow_k <= ceilings[overlay.zone]

        strict = next(o for o in overlays if o.zone == "strict")
        base = next(o for o in overlays if o.name == "base")
        assert len(strict.trades) < len(base.trades)  # the narrower zone takes fewer


class TestSensitivityGrid:
    """§5.6: five configurations plus P13's variant, all reported."""

    def test_the_grid_is_exactly_what_the_spec_pins(self) -> None:
        grid = {(c.name, c.zone, c.sigma_multiplier, c.friction) for c in synthetic.CONFIGS}

        assert grid == {
            ("base", "base", 1.1, 0.04),
            ("m1.0", "base", 1.0, 0.04),
            ("m1.2", "base", 1.2, 0.04),
            ("h0.00", "base", 1.1, 0.0),
            ("h0.06", "base", 1.1, 0.06),
            ("strict", "strict", 1.1, 0.04),
        }

    def test_every_configured_zone_is_one_the_engine_replays(self) -> None:
        assert {config.zone for config in synthetic.CONFIGS} <= set(engine.VARIANTS)

    def test_a_higher_haircut_costs_and_never_pays(self, tmp_path: Path) -> None:
        cheap = golden_overlay_trade(tmp_path / "a", synthetic.OverlayConfig("h", friction=0.0))
        dear = golden_overlay_trade(tmp_path / "b", synthetic.OverlayConfig("h", friction=0.06))

        assert dear.entry_cost > cheap.entry_cost
        assert dear.exit_value < cheap.exit_value
        assert dear.r_overlay < cheap.r_overlay

    def test_a_higher_sigma_buys_a_lower_strike_and_a_dearer_option(self, tmp_path: Path) -> None:
        low = golden_overlay_trade(
            tmp_path / "a", synthetic.OverlayConfig("m", sigma_multiplier=1.0)
        )
        high = golden_overlay_trade(
            tmp_path / "b", synthetic.OverlayConfig("m", sigma_multiplier=1.2)
        )

        # More vol pushes a 0.70Δ strike further down and the premium up.
        assert high.strike < low.strike
        assert high.entry_cost > low.entry_cost


class TestJsonOutput:
    """§8's machine-readable output has to survive a strict parse."""

    def test_no_nan_or_infinity_reaches_the_rendered_json(self, tmp_path: Path) -> None:
        membership, cache = build_fixture(tmp_path)
        overlays = synthetic.build_overlays(cache)
        window = data.Window(
            start=date(2017, 1, 2), end=date(2020, 12, 18), fetch_start=date(2015, 1, 1)
        )
        engine.run(membership, cache, window, overlays=overlays)
        benchmark = metrics.BenchmarkPrices(cache.load_benchmark())
        stats = [
            metrics.track_a_overlay(o.config, o.trades, o.skipped, benchmark) for o in overlays
        ]

        rendered = metrics.track_a_json(stats) + json.dumps(
            [trade.as_dict() for overlay in overlays for trade in overlay.trades]
        )

        def reject(literal: str) -> float:
            raise AssertionError(f"{literal} is not valid JSON")

        json.loads(metrics.track_a_json(stats), parse_constant=reject)
        json.loads(
            json.dumps([t.as_dict() for o in overlays for t in o.trades]), parse_constant=reject
        )
        assert "NaN" not in rendered and "Infinity" not in rendered

    def test_the_overlay_table_is_grouped_under_its_own_track(self, tmp_path: Path) -> None:
        """ "base" names a zone variant on one track and a §5.6 configuration on
        the other; a flat mapping would silently overwrite one with the other."""
        membership, cache = build_fixture(tmp_path)
        overlays = synthetic.build_overlays(cache, [synthetic.OverlayConfig("base")])
        window = data.Window(
            start=date(2017, 1, 2), end=date(2020, 12, 18), fetch_start=date(2015, 1, 1)
        )
        result = engine.run(membership, cache, window, overlays=overlays)
        benchmark = metrics.BenchmarkPrices(cache.load_benchmark())

        payload = json.loads(
            metrics.track_a_json(
                [
                    metrics.track_a(
                        "base", result.trades["base"], result.skipped["base"], benchmark
                    ),
                    metrics.track_a_overlay(
                        overlays[0].config, overlays[0].trades, overlays[0].skipped, benchmark
                    ),
                ]
            )
        )

        assert set(payload) == {"stock", "overlay"}
        assert payload["stock"]["base"]["vehicle_alpha"] is None
        assert payload["overlay"]["base"]["vehicle_alpha"]["mean"] is not None
        assert payload["overlay"]["base"]["parameters"] == {
            "zone": "base",
            "sigma_multiplier": 1.1,
            "friction": 0.04,
        }


class _NoMarketInputs:
    """A `MarketInputs` stand-in for tests that price nothing."""

    def use_chain(self, chain: str) -> None:
        pass


def _stub_panel(
    days: list[date],
    *,
    close: list[float],
    trend_broken: list[bool] | None = None,
) -> engine.Panel:
    """The smallest panel the exit rules read: closes, dates and two flags."""
    count = len(days)
    return engine.Panel(
        chain="STUB",
        session_dates=tuple(days),
        open_=np.array(close, dtype="float64"),
        close=np.array(close, dtype="float64"),
        trend_pass=np.zeros(count, dtype=bool),
        trend_broken=np.array(trend_broken or [False] * count, dtype=bool),
        label_dates=(),
        slow_k=np.array([], dtype="float64"),
        stoch_d=np.array([], dtype="float64"),
        label_session=np.array([], dtype="int64"),
        latest_label=np.full(count, -1, dtype="int64"),
        cross_below_exit=np.array([], dtype=bool),
        entry_ready=np.array([], dtype=bool),
        in_zone_low=np.array([], dtype=bool),
        turning_up=np.array([], dtype=bool),
        first_eligible=None,
    )


def test_the_engine_fixture_carries_the_two_overlay_inputs() -> None:
    """A guard on the shared fixture: without ^IRX every overlay entry would
    skip for want of a rate, and the tests above would pass vacuously."""
    frames = fixture_frames()

    assert data.RATE_SYMBOL in frames
    assert (frames["AAA"][data.DIVIDEND_COLUMN] > 0).any()
