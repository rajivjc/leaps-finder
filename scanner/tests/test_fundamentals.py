"""Fundamentals derivation tests (SPEC.md §3.3). Pure — no network."""

from datetime import date

import pytest

from leaps_scanner import fundamentals
from leaps_scanner.fundamentals import Fundamentals, from_info
from leaps_scanner.prices import Throttle

FULL_INFO = {
    "operatingMargins": 0.25,
    "returnOnEquity": 0.30,
    "totalDebt": 100e9,
    "totalCash": 40e9,
    "ebitda": 30e9,
    "revenueGrowth": 0.12,
    "freeCashflow": 20e9,
    "totalRevenue": 100e9,
    "forwardPE": 22.5,
    "targetMeanPrice": 210.0,
    "trailingAnnualDividendYield": 0.0125,
}


class TestFromInfo:
    def test_derives_every_metric(self):
        snapshot = from_info(FULL_INFO, next_earnings=date(2026, 10, 22))

        assert snapshot.op_margin == pytest.approx(0.25)
        assert snapshot.roe == pytest.approx(0.30)
        assert snapshot.net_debt_ebitda == pytest.approx((100e9 - 40e9) / 30e9)
        assert snapshot.rev_growth == pytest.approx(0.12)
        assert snapshot.fcf_margin == pytest.approx(0.20)
        assert snapshot.fwd_pe == pytest.approx(22.5)
        assert snapshot.analyst_target == pytest.approx(210.0)
        assert snapshot.dividend_yield == pytest.approx(0.0125)
        assert snapshot.next_earnings == date(2026, 10, 22)
        assert snapshot.is_empty is False

    def test_missing_keys_stay_none_rather_than_zero(self):
        snapshot = from_info({})

        assert snapshot.op_margin is None
        assert snapshot.net_debt_ebitda is None
        assert snapshot.fcf_margin is None
        assert snapshot.is_empty is True
        # q defaults to 0 — an absent dividend is a zero dividend for §5.2.
        assert snapshot.dividend_yield == 0.0

    def test_net_debt_needs_debt_and_positive_ebitda(self):
        assert from_info({**FULL_INFO, "ebitda": 0}).net_debt_ebitda is None
        assert from_info({**FULL_INFO, "ebitda": -5e9}).net_debt_ebitda is None
        no_debt = {k: v for k, v in FULL_INFO.items() if k != "totalDebt"}
        assert from_info(no_debt).net_debt_ebitda is None

    def test_missing_cash_is_treated_as_zero_cash(self):
        no_cash = {k: v for k, v in FULL_INFO.items() if k != "totalCash"}

        assert from_info(no_cash).net_debt_ebitda == pytest.approx(100e9 / 30e9)

    def test_net_cash_goes_negative(self):
        snapshot = from_info({**FULL_INFO, "totalCash": 160e9})

        assert snapshot.net_debt_ebitda == pytest.approx(-2.0)

    def test_negative_forward_pe_is_missing_not_cheap(self):
        assert from_info({**FULL_INFO, "forwardPE": -8.0}).fwd_pe is None

    def test_non_numeric_values_are_ignored(self):
        snapshot = from_info({**FULL_INFO, "operatingMargins": "Infinity", "returnOnEquity": None})

        assert snapshot.op_margin is None
        assert snapshot.roe is None

    def test_fcf_margin_needs_positive_revenue(self):
        assert from_info({**FULL_INFO, "totalRevenue": 0}).fcf_margin is None


class TestFetchFundamentals:
    def test_fetches_every_symbol(self):
        def fetcher(symbol):
            return from_info(FULL_INFO)

        results = fundamentals.fetch_fundamentals(
            ["AAA", "BBB"], fetcher=fetcher, sleeper=lambda _: None
        )

        assert sorted(results) == ["AAA", "BBB"]
        assert results["AAA"].op_margin == pytest.approx(0.25)

    def test_a_symbol_that_keeps_failing_gets_an_empty_snapshot(self):
        def fetcher(symbol):
            if symbol == "BBB":
                raise ConnectionError("yahoo down")
            return from_info(FULL_INFO)

        results = fundamentals.fetch_fundamentals(
            ["AAA", "BBB"], fetcher=fetcher, sleeper=lambda _: None
        )

        assert results["BBB"] == Fundamentals()
        assert results["AAA"].is_empty is False

    def test_empty_payloads_are_retried_like_errors(self):
        attempts = []

        def fetcher(symbol):
            attempts.append(symbol)
            if len(attempts) == 1:
                return Fundamentals()  # yfinance-style silent failure
            return from_info(FULL_INFO)

        results = fundamentals.fetch_fundamentals(
            ["AAA"],
            fetcher=fetcher,
            throttle=Throttle(sleeper=lambda _: None),
            sleeper=lambda _: None,
        )

        assert len(attempts) == 2
        assert results["AAA"].is_empty is False
