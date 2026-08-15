"""Options math and selection tests (SPEC.md §5, §10). Pure — no network."""

import math
from datetime import date

import numpy as np
import pandas as pd
import pytest

from leaps_scanner import options
from leaps_scanner.options import OptionChain, OptionQuery
from leaps_scanner.prices import Throttle

TODAY = date(2026, 8, 14)


def instant_throttle() -> Throttle:
    return Throttle(sleeper=lambda _: None)


def calls_frame(rows) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["strike", "bid", "ask", "openInterest", "impliedVolatility"])


class TestBsDelta:
    """§10: verify against reference values at σ=0.30, T=1, r=0.04.

    References computed independently with scipy.stats.norm.cdf.
    """

    @pytest.mark.parametrize(
        ("strike", "q", "expected"),
        [
            (100.0, 0.0, 0.6115393363),
            (80.0, 0.0, 0.8478239444),
            (120.0, 0.0, 0.3728156458),
            (100.0, 0.02, 0.5741669938),
            (70.0, 0.0, 0.9295232848),
            (130.0, 0.0, 0.2771884498),
        ],
    )
    def test_matches_reference_values(self, strike, q, expected):
        delta = options.bs_delta(100.0, strike, 1.0, 0.04, q, 0.30)

        assert delta == pytest.approx(expected, abs=1e-9)

    def test_deep_itm_approaches_one_and_deep_otm_zero(self):
        assert options.bs_delta(100.0, 1.0, 1.0, 0.04, 0.0, 0.30) == pytest.approx(1.0, abs=1e-6)
        assert options.bs_delta(100.0, 10_000.0, 1.0, 0.04, 0.0, 0.30) == pytest.approx(
            0.0, abs=1e-6
        )

    def test_dividend_yield_lowers_delta(self):
        without = options.bs_delta(100.0, 100.0, 1.0, 0.04, 0.0, 0.30)
        with_q = options.bs_delta(100.0, 100.0, 1.0, 0.04, 0.03, 0.30)

        assert with_q < without

    @pytest.mark.parametrize(
        ("spot", "strike", "t", "sigma"),
        [(0.0, 100, 1, 0.3), (100, 0.0, 1, 0.3), (100, 100, 0.0, 0.3), (100, 100, 1, 0.0)],
    )
    def test_degenerate_inputs_are_rejected(self, spot, strike, t, sigma):
        with pytest.raises(ValueError):
            options.bs_delta(spot, strike, t, 0.04, 0.0, sigma)


class TestSelectLeapExpiry:
    def test_prefers_the_nearest_expiry_at_or_beyond_350_days(self):
        expiries = [
            TODAY.fromordinal(TODAY.toordinal() + days) for days in (30, 100, 349, 350, 420, 700)
        ]

        choice = options.select_leap_expiry(expiries, TODAY)

        assert choice.dte == 350
        assert choice.meets_min_dte is True

    def test_falls_back_to_the_longest_available_and_flags_it(self):
        expiries = [date(2026, 9, 18), date(2026, 12, 18)]

        choice = options.select_leap_expiry(expiries, TODAY)

        assert choice.expiry == date(2026, 12, 18)
        assert choice.dte < options.MIN_LEAP_DTE
        assert choice.meets_min_dte is False

    def test_no_future_expiries_yields_none(self):
        assert options.select_leap_expiry([date(2026, 8, 14), date(2025, 1, 1)], TODAY) is None


class TestSelectContract:
    EXPIRY = date(2027, 8, 20)  # 371 days from TODAY

    def select(self, rows, spot=100.0, r=0.04, q=0.0):
        return options.select_contract(
            calls_frame(rows), spot=spot, expiry=self.EXPIRY, today=TODAY, r=r, q=q
        )

    def test_picks_the_strike_with_delta_nearest_070(self):
        # At σ=0.30, T≈1.016: Δ(80)≈0.85, Δ(100)≈0.61, Δ(90)≈0.74 — 90 wins.
        contract = self.select(
            [
                (80.0, 24.0, 26.0, 1000, 0.30),
                (90.0, 17.0, 19.0, 1000, 0.30),
                (100.0, 12.0, 14.0, 1000, 0.30),
            ]
        )

        assert contract.strike == 90.0
        assert abs(contract.delta - 0.70) < abs(
            options.bs_delta(100.0, 100.0, contract.dte / 365.0, 0.04, 0.0, 0.30) - 0.70
        )

    def test_crossed_quotes_are_excluded(self):
        # A stale crossed market (ask below bid) would carry a negative
        # spread that trivially clears every preset gate.
        contract = self.select(
            [
                (90.0, 5.00, 0.05, 1000, 0.30),  # crossed: nearest to 0.70Δ
                (100.0, 12.0, 14.0, 1000, 0.30),
            ]
        )

        assert contract.strike == 100.0
        assert contract.spread_pct > 0

    def test_junk_strings_in_chain_cells_are_skipped_not_fatal(self):
        # One bad cell must cost one contract, never the scan.
        contract = self.select(
            [
                (90.0, "N/A", 19.0, 1000, 0.30),
                (100.0, 12.0, 14.0, "Infinity", "0.30x"),
                (110.0, 9.0, 10.0, 800, 0.30),
            ]
        )

        assert contract.strike == 110.0

    def test_zero_bid_contracts_are_excluded_even_if_closer_to_target(self):
        contract = self.select(
            [
                (90.0, 0.0, 19.0, 1000, 0.30),  # closest to 0.70Δ but unquotable
                (100.0, 12.0, 14.0, 1000, 0.30),
            ]
        )

        assert contract.strike == 100.0

    def test_missing_iv_contracts_are_skipped_not_guessed(self):
        contract = self.select(
            [
                (90.0, 17.0, 19.0, 1000, np.nan),
                (100.0, 12.0, 14.0, 1000, 0.30),
            ]
        )

        assert contract.strike == 100.0

    def test_no_quotable_contract_yields_none(self):
        assert self.select([(90.0, 0.0, 0.0, 1000, 0.30)]) is None
        assert self.select([]) is None

    def test_economics_match_hand_calculation(self):
        # §5.4 and §10 acceptance: mid, breakeven, cost %, spread % to the cent.
        contract = self.select([(90.0, 17.0, 19.0, 750, 0.30)])

        assert contract.mid == pytest.approx(18.0)
        assert contract.breakeven == pytest.approx(108.0)
        assert contract.breakeven_pct == pytest.approx(0.08)
        assert contract.cost_pct_spot == pytest.approx(0.18)
        assert contract.spread_pct == pytest.approx(2.0 / 18.0)
        assert contract.oi == 750
        assert contract.iv == pytest.approx(0.30)
        assert contract.dte == 371

    def test_missing_open_interest_counts_as_zero(self):
        rows = pd.DataFrame(
            [(90.0, 17.0, 19.0, np.nan, 0.30)],
            columns=["strike", "bid", "ask", "openInterest", "impliedVolatility"],
        )

        contract = options.select_contract(
            rows, spot=100.0, expiry=self.EXPIRY, today=TODAY, r=0.04, q=0.0
        )

        assert contract.oi == 0


class TestAtmIv:
    def test_averages_call_and_put_iv_at_the_strike_nearest_spot(self):
        chain = OptionChain(
            calls=calls_frame([(95.0, 1.0, 2.0, 10, 0.25), (100.0, 1.0, 2.0, 10, 0.30)]),
            puts=calls_frame([(100.0, 1.0, 2.0, 10, 0.40), (110.0, 1.0, 2.0, 10, 0.45)]),
        )

        assert options.atm_iv(chain, 101.0) == pytest.approx(0.35)  # (0.30 + 0.40) / 2

    def test_one_usable_side_is_accepted(self):
        chain = OptionChain(
            calls=calls_frame([(100.0, 1.0, 2.0, 10, 0.30)]),
            puts=pd.DataFrame(),
        )

        assert options.atm_iv(chain, 100.0) == pytest.approx(0.30)

    def test_no_usable_iv_yields_none(self):
        chain = OptionChain(
            calls=calls_frame([(100.0, 1.0, 2.0, 10, np.nan)]),
            puts=pd.DataFrame(),
        )

        assert options.atm_iv(chain, 100.0) is None

    def test_a_side_whose_nearest_usable_strike_is_far_from_spot_is_skipped(self):
        # Near-the-money put IVs are all NaN; the only usable put sits at half
        # of spot. Blending that wing quote into "ATM" would import skew.
        chain = OptionChain(
            calls=calls_frame([(100.0, 1.0, 2.0, 10, 0.30)]),
            puts=calls_frame([(50.0, 1.0, 2.0, 10, 0.90), (100.0, 1.0, 2.0, 10, np.nan)]),
        )

        assert options.atm_iv(chain, 100.0) == pytest.approx(0.30)

    def test_none_sides_from_yfinance_are_treated_as_empty(self):
        # yfinance returns Options(calls=None, puts=None) when the payload is
        # missing; the emptiness probe must not raise inside the retry loop.
        chain = OptionChain(calls=None, puts=None)

        assert chain.is_empty is True
        assert options.atm_iv(chain, 100.0) is None
        assert (
            options.select_contract(
                None, spot=100.0, expiry=date(2027, 8, 20), today=TODAY, r=0.04, q=0.0
            )
            is None
        )


class TestIv30:
    def test_interpolates_between_bracketing_expiries(self):
        # 21d at 0.28, 49d at 0.32 → 30d = 0.28 + 9/28 · 0.04
        value = options.interpolate_iv30([(21, 0.28), (49, 0.32)])

        assert value == pytest.approx(0.28 + 9.0 / 28.0 * 0.04)

    def test_single_reading_is_used_as_is(self):
        assert options.interpolate_iv30([(21, 0.28)]) == pytest.approx(0.28)

    def test_one_sided_readings_use_the_nearest(self):
        assert options.interpolate_iv30([(40, 0.31), (60, 0.35)]) == pytest.approx(0.31)

    def test_no_readings_yield_none(self):
        assert options.interpolate_iv30([]) is None

    def test_expiry_pair_brackets_thirty_days(self):
        expiries = [
            date(2026, 8, 21),  # 7d
            date(2026, 9, 4),  # 21d
            date(2026, 10, 2),  # 49d
            date(2026, 11, 20),
        ]

        picks = options.iv30_expiries(expiries, TODAY)

        assert picks == [date(2026, 9, 4), date(2026, 10, 2)]


class TestRealizedVol:
    def test_constant_growth_has_zero_volatility(self):
        close = pd.Series([100.0 * 1.01**i for i in range(30)])

        assert options.realized_vol_20d(close) == pytest.approx(0.0, abs=1e-9)

    def test_matches_hand_computed_annualized_std(self):
        rng = np.random.default_rng(7)
        close = pd.Series(100.0 * np.exp(np.cumsum(rng.normal(0, 0.02, 40))))
        returns = np.log(close / close.shift(1)).dropna().tail(20)
        expected = returns.std(ddof=1) * math.sqrt(252)

        assert options.realized_vol_20d(close) == pytest.approx(float(expected))

    def test_short_history_yields_none(self):
        assert options.realized_vol_20d(pd.Series([100.0] * 15)) is None


class TestEvaluateSymbolOptions:
    """Fetch orchestration: what failure means, and what merely degrades."""

    LEAP_EXPIRY = date(2027, 8, 20)
    NEAR_EXPIRY = date(2026, 9, 4)

    def evaluate(self, expiries_fetcher, chain_fetcher):
        return options.evaluate_symbol_options(
            OptionQuery(symbol="AAA", spot=100.0, dividend_yield=0.0),
            today=TODAY,
            r=0.04,
            expiries_fetcher=expiries_fetcher,
            chain_fetcher=chain_fetcher,
            throttle=instant_throttle(),
            sleeper=lambda _: None,
        )

    def full_chain(self):
        return OptionChain(
            calls=calls_frame([(90.0, 17.0, 19.0, 1000, 0.30)]),
            puts=calls_frame([(90.0, 5.0, 6.0, 1000, 0.32)]),
        )

    def test_unfetchable_expiries_mean_failure(self):
        def broken(symbol):
            raise ConnectionError("yahoo down")

        assert self.evaluate(broken, lambda s, e: self.full_chain()) is None

    def test_unfetchable_leap_chain_is_flagged_but_keeps_iv30(self):
        # Losing the contract must not also stall §6 snapshot accrual.
        def chains(symbol, expiry):
            if expiry == self.LEAP_EXPIRY:
                raise ConnectionError("yahoo down")
            return self.full_chain()

        result = self.evaluate(lambda s: [self.LEAP_EXPIRY, self.NEAR_EXPIRY], chains)

        assert result is not None
        assert result.chain_failed is True
        assert result.contract is None
        assert result.iv30 == pytest.approx(0.31)  # mean of 0.30 call / 0.32 put

    def test_unfetchable_iv30_chains_degrade_to_missing_iv30(self):
        # A 49d expiry keeps the LEAP chain out of the 30d bracket, so both
        # IV30 chains fail while the contract fetch succeeds.
        def chains(symbol, expiry):
            if expiry != self.LEAP_EXPIRY:
                raise ConnectionError("yahoo down")
            return self.full_chain()

        result = self.evaluate(
            lambda s: [self.LEAP_EXPIRY, self.NEAR_EXPIRY, date(2026, 10, 2)], chains
        )

        assert result is not None
        assert result.contract is not None
        assert result.iv30 is None

    def test_fetched_chain_with_no_valid_contract_is_not_a_failure(self):
        def chains(symbol, expiry):
            return OptionChain(
                calls=calls_frame([(90.0, 0.0, 0.0, 1000, 0.30)]),
                puts=pd.DataFrame(),
            )

        result = self.evaluate(lambda s: [self.LEAP_EXPIRY], chains)

        assert result is not None
        assert result.contract is None

    def test_empty_chains_are_retried_like_errors(self):
        # yfinance reports rate limiting as empty frames, not exceptions.
        attempts = []

        def chains(symbol, expiry):
            attempts.append(expiry)
            if len(attempts) == 1:
                return OptionChain(calls=pd.DataFrame(), puts=pd.DataFrame())
            return self.full_chain()

        result = self.evaluate(lambda s: [self.LEAP_EXPIRY], chains)

        # Attempt 1 came back empty and was retried; attempt 2 delivered the
        # chain. The LEAP expiry doubles as the only IV30 term, and the memo
        # means it is not fetched a third time.
        assert result.contract is not None
        assert len(attempts) == 2
