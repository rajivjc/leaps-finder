"""Pipeline tests (SPEC.md §3, §9). No network and no database: both are faked."""

import json
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from leaps_scanner import db, indicators, run_scan, scoring, universe
from leaps_scanner.fundamentals import Fundamentals
from leaps_scanner.indicators import Signals
from leaps_scanner.options import OptionChain
from leaps_scanner.universe import SeedEntry

VALID_ENV = {
    "SUPABASE_URL": "https://example.supabase.co",
    "SUPABASE_SERVICE_KEY": "service-key-value",
}

GOLDEN_DIR = Path(__file__).parent / "golden"

AS_OF = date(2026, 8, 14)
LEAP_EXPIRY = date(2027, 8, 20)  # 371 days after AS_OF
NEAR_EXPIRY = date(2026, 9, 4)  # 21 days
FAR_EXPIRY = date(2026, 10, 2)  # 49 days


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, data):
        self.data = data


class FakeTable:
    """Records the PostgREST calls the scanner makes, in order."""

    def __init__(self, name, log, fail_on=None, select_rows=None):
        self._name = name
        self._log = log
        self._fail_on = fail_on
        self._select_rows = select_rows if select_rows is not None else []
        self._pending = None
        self._range = None
        self._limit = None

    def insert(self, rows):
        self._pending = ("insert", rows)
        return self

    def upsert(self, rows, on_conflict=None):
        self._pending = ("upsert", rows)
        return self

    def update(self, values):
        self._pending = ("update", values)
        return self

    def select(self, columns="*"):
        self._pending = ("select", columns)
        return self

    def eq(self, column, value):
        return self

    def gte(self, column, value):
        return self

    def lte(self, column, value):
        return self

    def order(self, column, desc=False):
        return self

    def limit(self, count):
        self._limit = count
        return self

    @property
    def not_(self):
        return self

    def is_(self, column, value):
        return self

    def range(self, start, end):
        self._range = (start, end)
        return self

    def execute(self):
        verb, payload = self._pending
        if self._fail_on == (self._name, verb):
            raise RuntimeError(f"PostgREST 503 on {self._name}.{verb}")
        self._log.append((self._name, verb, payload))
        if verb == "select":
            rows = self._select_rows
            if self._range is not None:
                rows = rows[self._range[0] : self._range[1] + 1]
            if self._limit is not None:
                rows = rows[: self._limit]
            return FakeResponse(rows)
        if self._name == "scans" and verb == "insert":
            return FakeResponse([{"id": 7}])
        return FakeResponse(payload if isinstance(payload, list) else [payload])


class FakeClient:
    def __init__(self, fail_on=None):
        self.calls = []
        self.select_rows = {}
        self._fail_on = fail_on

    def table(self, name):
        return FakeTable(name, self.calls, self._fail_on, self.select_rows.get(name))

    def rows(self, table, verb):
        return [payload for name, v, payload in self.calls if name == table and v == verb]


def rising_frame(sessions=500, end="2026-08-14", start=100.0, stop=200.0) -> pd.DataFrame:
    values = np.linspace(start, stop, sessions)
    index = pd.bdate_range(end=end, periods=sessions)
    return pd.DataFrame(
        {
            "Open": values,
            "High": values + 1.0,
            "Low": values - 1.0,
            "Close": values,
            "Volume": np.full(sessions, 2_000_000.0),
        },
        index=index,
    )


def golden_frame() -> pd.DataFrame:
    """A two-year path that ends trend-pass, in-zone and turning up: a long
    rise, a six-week pullback, then a three-week recovery."""
    values = np.concatenate(
        [np.linspace(100, 200, 455), np.linspace(200, 184, 30), np.linspace(184, 196, 15)]
    )
    index = pd.bdate_range(end="2026-08-14", periods=len(values))
    return pd.DataFrame(
        {
            "Open": values,
            "High": values * 1.01,
            "Low": values * 0.99,
            "Close": values,
            "Volume": np.full(len(values), 2_000_000.0),
        },
        index=index,
    )


def batch_downloader(frame: pd.DataFrame):
    """Wrap one per-symbol frame as a yfinance-style batched response."""

    def downloader(symbols, period):
        columns = pd.MultiIndex.from_product(
            [list(symbols), ["Open", "High", "Low", "Close", "Volume"]]
        )
        return pd.DataFrame(
            np.tile(frame.values, (1, len(symbols))), index=frame.index, columns=columns
        )

    return downloader


def calls_frame(rows) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["strike", "bid", "ask", "openInterest", "impliedVolatility"])


def fake_fundamentals(symbol) -> Fundamentals:
    return Fundamentals(
        op_margin=0.25,
        roe=0.30,
        net_debt_ebitda=2.0,
        rev_growth=0.12,
        fcf_margin=0.20,
        fwd_pe=22.5,
        analyst_target=235.2,
        dividend_yield=0.0125,
        next_earnings=date(2026, 9, 25),
    )


def fake_expiries(symbol):
    return [LEAP_EXPIRY, NEAR_EXPIRY, FAR_EXPIRY]


def fake_chain(symbol, expiry) -> OptionChain:
    if expiry == LEAP_EXPIRY:
        mids = {140.0: 62.0, 160.0: 48.0, 180.0: 34.0, 196.0: 26.0, 220.0: 17.0}
        return OptionChain(
            calls=calls_frame([(k, m - 0.5, m + 0.5, 1500, 0.30) for k, m in mids.items()]),
            puts=calls_frame([(180.0, 9.5, 10.5, 900, 0.31)]),
        )
    if expiry == NEAR_EXPIRY:
        return OptionChain(
            calls=calls_frame([(196.0, 4.5, 5.0, 400, 0.27)]),
            puts=calls_frame([(196.0, 4.0, 4.5, 400, 0.29)]),
        )
    return OptionChain(
        calls=calls_frame([(196.0, 7.5, 8.0, 400, 0.31)]),
        puts=calls_frame([(196.0, 7.0, 7.5, 400, 0.33)]),
    )


def signals(**overrides) -> Signals:
    base = dict(
        as_of_date=AS_OF,
        spot=150.0,
        sma50=140.0,
        sma200=130.0,
        trend_pass=True,
        stoch_k=45.0,
        stoch_d=40.0,
        stoch_k_prev=42.0,
        in_zone=True,
        turning_up=True,
        pct_off_52w_high=-0.05,
        avg_volume_30d=1_000_000.0,
        share_above_sma50_60d=0.9,
        weeks_since_cross_up=1,
    )
    return Signals(**{**base, **overrides})


# --------------------------------------------------------------------------
# Pure logic
# --------------------------------------------------------------------------


class TestEarningsDistance:
    def test_days_from_the_scan_date(self):
        assert run_scan.earnings_distance(date(2026, 9, 25), AS_OF) == 42

    def test_unknown_date_is_none(self):
        assert run_scan.earnings_distance(None, AS_OF) is None

    def test_stale_past_date_is_treated_as_unknown(self):
        assert run_scan.earnings_distance(date(2026, 8, 1), AS_OF) is None
        assert run_scan.earnings_distance(AS_OF, AS_OF) is None


class TestBuildScanRow:
    def build(self, **overrides):
        arguments = dict(
            fundamentals=fake_fundamentals("AAA"),
            options_result=None,
            iv_context=run_scan.IvContext(value=50.0, status=scoring.IV_RANK_STATUS_WARMING),
            fwd_pe_percentile=50.0,
            as_of=AS_OF,
        )
        arguments.update(overrides)
        return run_scan.build_scan_row(7, "AAA", signals(), **arguments)

    def test_carries_every_m2_signal(self):
        row = self.build()

        assert row["scan_id"] == 7
        assert row["symbol"] == "AAA"
        assert row["stoch_k"] == 45.0
        assert row["stoch_k_prev"] == 42.0
        assert row["trend_pass"] is True

    def test_scores_are_computed_and_composite_is_their_weighted_sum(self):
        row = self.build()

        # No contract: option subscore and composite must be null, the other
        # four subscores real.
        assert row["s_option"] is None
        assert row["score"] is None
        for column in ("s_trend", "s_quality", "s_valuation", "s_entry"):
            assert row[column] is not None

    def test_no_options_result_leaves_option_columns_null_and_fails_presets(self):
        row = self.build(options_result=None)

        for column in ("opt_strike", "opt_expiry", "opt_delta", "breakeven", "cost_pct_spot"):
            assert row[column] is None
        assert row["passes_strict"] is False
        assert row["passes_balanced"] is False
        assert row["passes_wide"] is False

    def test_warming_up_badge_and_substitute_are_stored(self):
        row = self.build()

        assert row["iv_rank"] == 50.0
        assert row["iv_rank_status"] == scoring.IV_RANK_STATUS_WARMING

    def test_fundamentals_snapshot_is_written_through(self):
        row = self.build()

        assert row["op_margin"] == 0.25
        assert row["fwd_pe"] == 22.5
        assert row["upside_adj"] == pytest.approx(0.6 * (235.2 / 150.0 - 1.0))
        assert row["next_earnings"] == "2026-09-25"
        assert row["earnings_dte"] == 42

    def test_checklist_booleans_use_the_documented_thresholds(self):
        row = self.build()

        assert row["quality_pass"] is True  # s_quality ≈ 74.7 ≥ 45
        assert row["iv_pass"] is True  # substitute 50 ≤ 50
        assert row["valuation_pass"] is True  # upside_adj > 0

        no_target = self.build(fundamentals=Fundamentals(next_earnings=date(2026, 9, 25)))
        assert no_target["valuation_pass"] is False
        assert no_target["quality_pass"] is False


class TestResolveAsOfDate:
    def test_uses_the_week_most_symbols_completed(self):
        early = date(2026, 8, 7)

        resolved = run_scan.resolve_as_of_date(
            {
                "A": signals(as_of_date=AS_OF),
                "B": signals(as_of_date=AS_OF),
                "C": signals(as_of_date=early),  # halted into Friday
            }
        )

        assert resolved == AS_OF

    def test_ties_break_to_the_later_week(self):
        early = date(2026, 8, 7)

        resolved = run_scan.resolve_as_of_date(
            {"A": signals(as_of_date=early), "B": signals(as_of_date=AS_OF)}
        )

        assert resolved == AS_OF

    def test_no_signals_is_an_error_not_a_guess(self):
        with pytest.raises(ValueError):
            run_scan.resolve_as_of_date({})


class TestAssess:
    def test_full_coverage_is_ok(self):
        status, _ = run_scan.assess(100, 100)

        assert status == db.STATUS_OK

    def test_exactly_twenty_percent_missing_still_passes(self):
        # SPEC.md §9 fails above 20%, not at it.
        status, _ = run_scan.assess(100, 80)

        assert status == db.STATUS_OK

    def test_more_than_twenty_percent_missing_fails(self):
        status, notes = run_scan.assess(100, 79)

        assert status == db.STATUS_FAILED
        assert "21/100" in notes

    def test_option_failures_count_toward_the_ceiling(self):
        # 10 without prices + 11 evaluated without chains = 21% incomplete.
        status, notes = run_scan.assess(100, 90, option_failures=11)

        assert status == db.STATUS_FAILED
        assert "21/100" in notes

    def test_option_failures_within_the_ceiling_still_pass(self):
        status, notes = run_scan.assess(100, 95, option_failures=10)

        assert status == db.STATUS_OK
        assert "10 evaluated without option data" in notes

    def test_fundamentals_failures_count_toward_the_ceiling(self):
        status, notes = run_scan.assess(100, 100, fundamentals_failures=21)

        assert status == db.STATUS_FAILED
        assert "21 without fundamentals" in notes

    def test_deduplicated_data_failures_override_the_sum(self):
        # A symbol that failed both fetches is one incomplete symbol, not two.
        status, _ = run_scan.assess(
            100, 100, option_failures=15, fundamentals_failures=15, data_failures=15
        )

        assert status == db.STATUS_OK

    def test_empty_universe_fails(self):
        status, notes = run_scan.assess(0, 0)

        assert status == db.STATUS_FAILED
        assert "empty universe" in notes


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------


class TestRunFullScan:
    SEED = [
        SeedEntry("AAA", "Alpha", "Information Technology", "Software"),
        SeedEntry("BBB", "Beta", "Health Care", "Pharma"),
        SeedEntry("TINY", "Tiny", "Industrials", "Widgets"),
    ]
    CAPS = {"AAA": 900e9, "BBB": 120e9, "TINY": 1e9}

    def run(self, client, downloader=None, **kwargs):
        arguments = dict(
            seed=self.SEED,
            market_cap_fetcher=self.CAPS.get,
            downloader=downloader or batch_downloader(rising_frame()),
            fundamentals_fetcher=fake_fundamentals,
            expiries_fetcher=fake_expiries,
            chain_fetcher=fake_chain,
            risk_free_fetcher=lambda: 0.04,
            sleeper=lambda _: None,
        )
        arguments.update(kwargs)
        return run_scan.run_full_scan(client, **arguments)

    def test_applies_the_market_cap_floor(self):
        client = FakeClient()

        report = self.run(client)

        assert report.universe_count == 2  # TINY is below $50B

    def test_writes_tickers_before_scan_results(self):
        # scan_results.symbol has a foreign key to tickers.
        client = FakeClient()

        self.run(client)

        tables = [name for name, _, _ in client.calls]
        assert tables.index("tickers") < tables.index("scan_results")

    def test_writes_one_result_row_per_evaluated_symbol(self):
        client = FakeClient()

        self.run(client)

        rows = client.rows("scan_results", "upsert")[0]
        assert sorted(row["symbol"] for row in rows) == ["AAA", "BBB"]
        assert all(row["scan_id"] == 7 for row in rows)

    def test_records_the_scan_as_ok_with_its_counts(self):
        client = FakeClient()

        report = self.run(client)

        update = client.rows("scans", "update")[0]
        assert update["status"] == db.STATUS_OK
        assert update["universe_count"] == 2
        assert update["matches_count"] == report.matches_count
        assert report.ok is True

    def test_matches_count_is_the_wide_preset(self):
        client = FakeClient()

        report = self.run(client)

        rows = client.rows("scan_results", "upsert")[0]
        assert report.matches_count == sum(1 for row in rows if row["passes_wide"])

    def test_weekly_bars_are_written_after_the_tickers_they_reference(self):
        # weekly_bars.symbol has a foreign key to tickers.
        client = FakeClient()

        self.run(client)

        tables = [name for name, _, _ in client.calls]
        assert tables.index("tickers") < tables.index("weekly_bars")

    def test_weekly_bars_cover_the_evaluated_symbols_and_end_at_the_scan_week(self):
        client = FakeClient()

        self.run(client)

        rows = [row for chunk in client.rows("weekly_bars", "upsert") for row in chunk]
        by_symbol: dict[str, list[dict]] = {}
        for row in rows:
            by_symbol.setdefault(row["symbol"], []).append(row)

        assert sorted(by_symbol) == ["AAA", "BBB"]
        for bars in by_symbol.values():
            # The chart's last candle is the week the scan speaks for, so the
            # chart and the row beside it cannot disagree.
            assert max(bar["week_ending"] for bar in bars) == AS_OF.isoformat()
            assert len(bars) <= indicators.WEEKLY_HISTORY_WEEKS

    def test_average_volume_is_written_back_to_tickers(self):
        client = FakeClient()

        self.run(client)

        volume_rows = client.rows("tickers", "upsert")[1]
        assert all(row["avg_volume_30d"] == pytest.approx(2_000_000.0) for row in volume_rows)

    def test_iv_snapshots_are_written_before_the_history_read(self):
        # The current snapshot must be inside its own 252-snapshot window.
        client = FakeClient()

        self.run(client)

        events = [(name, verb) for name, verb, _ in client.calls]
        write = events.index(("iv_snapshots", "upsert"))
        read = events.index(("iv_snapshots", "select"))
        assert write < read

        snapshot_rows = client.rows("iv_snapshots", "upsert")[0]
        by_symbol = {row["symbol"]: row for row in snapshot_rows}
        assert sorted(by_symbol) == ["AAA", "BBB"]
        assert by_symbol["AAA"]["snap_date"] == AS_OF.isoformat()
        # 21d at 0.28 and 49d at 0.32 interpolate to 0.28 + 9/28 · 0.04.
        assert by_symbol["AAA"]["iv30"] == pytest.approx(0.28 + 9.0 / 28.0 * 0.04)
        assert by_symbol["AAA"]["rv20"] is not None

    def test_first_scan_is_warming_up_with_the_substitute_percentile(self):
        # SPEC.md §6: no snapshot history yet, so every symbol is warming_up
        # and the iv30/rv20 percentile stands in for IV rank. Two identical
        # symbols tie, and a tie sits at the 50th percentile.
        client = FakeClient()

        self.run(client)

        rows = client.rows("scan_results", "upsert")[0]
        assert all(row["iv_rank_status"] == scoring.IV_RANK_STATUS_WARMING for row in rows)
        assert all(row["iv_rank"] == pytest.approx(50.0) for row in rows)

    def test_established_history_graduates_to_a_real_iv_rank(self):
        client = FakeClient()
        # 130 prior snapshots for AAA spanning 0.20-0.40; BBB has none.
        client.select_rows["iv_snapshots"] = [
            {
                "symbol": "AAA",
                "snap_date": (AS_OF - timedelta(days=200 - i)).isoformat(),
                "iv30": 0.20 + 0.20 * i / 129,
            }
            for i in range(130)
        ]

        self.run(client)

        rows = {row["symbol"]: row for row in client.rows("scan_results", "upsert")[0]}
        iv30 = 0.28 + 9.0 / 28.0 * 0.04
        assert rows["AAA"]["iv_rank_status"] == scoring.IV_RANK_STATUS_OK
        assert rows["AAA"]["iv_rank"] == pytest.approx(100.0 * (iv30 - 0.20) / 0.20)
        assert rows["BBB"]["iv_rank_status"] == scoring.IV_RANK_STATUS_WARMING

    def test_rich_history_without_a_current_iv30_is_not_stamped_ok(self):
        # §6 defines the rank over the *current* iv30. When this scan produced
        # none, last week's rank must not be asserted as an 'ok' IV fact.
        client = FakeClient()
        client.select_rows["iv_snapshots"] = [
            {
                "symbol": "AAA",
                "snap_date": (AS_OF - timedelta(days=200 - i)).isoformat(),
                "iv30": 0.20 + 0.20 * i / 129,
            }
            for i in range(130)
        ]

        def no_expiries(symbol):
            raise ConnectionError("yahoo down")

        self.run(client, expiries_fetcher=no_expiries)

        rows = {row["symbol"]: row for row in client.rows("scan_results", "upsert")[0]}
        assert rows["AAA"]["iv_rank_status"] == scoring.IV_RANK_STATUS_WARMING
        assert rows["AAA"]["iv_rank"] is None
        assert rows["AAA"]["iv_pass"] is False
        assert rows["AAA"]["s_option"] is None

    def test_losing_most_of_the_universe_fails_the_scan(self):
        client = FakeClient()

        def broken(symbols, period):
            raise ConnectionError("yahoo down")

        report = self.run(client, downloader=broken)

        assert report.status == db.STATUS_FAILED
        assert report.ok is False
        assert client.rows("scans", "update")[0]["status"] == db.STATUS_FAILED

    def test_losing_every_option_chain_fails_the_scan(self):
        # Prices arrive but chains never do: partial data must not look done.
        client = FakeClient()

        def no_expiries(symbol):
            raise ConnectionError("yahoo down")

        report = self.run(client, expiries_fetcher=no_expiries)

        assert report.status == db.STATUS_FAILED
        assert report.option_failures == 2
        # The rows that could be computed are still written, without options.
        rows = client.rows("scan_results", "upsert")[0]
        assert all(row["opt_strike"] is None for row in rows)

    def test_a_failed_leap_chain_still_accrues_the_iv_snapshot(self):
        # Losing the contract must not also stall the 120-snapshot warm-up.
        client = FakeClient()

        def leap_less_chains(symbol, expiry):
            if expiry == LEAP_EXPIRY:
                raise ConnectionError("yahoo down")
            return fake_chain(symbol, expiry)

        report = self.run(client, chain_fetcher=leap_less_chains)

        assert report.option_failures == 2
        snapshot_rows = client.rows("iv_snapshots", "upsert")[0]
        assert all(row["iv30"] is not None for row in snapshot_rows)

    def test_losing_every_fundamentals_payload_fails_the_scan(self):
        # A wholesale info-endpoint outage is partial data, not a universe of
        # companies without financials.
        client = FakeClient()

        def no_fundamentals(symbol):
            raise ConnectionError("yahoo down")

        report = self.run(client, fundamentals_fetcher=no_fundamentals)

        assert report.status == db.STATUS_FAILED
        assert report.fundamentals_failures == 2
        # Rows are still written, with the quality columns honestly null.
        rows = client.rows("scan_results", "upsert")[0]
        assert all(row["s_quality"] is None for row in rows)

    def test_unavailable_risk_free_rate_aborts_the_scan_as_failed(self):
        client = FakeClient()

        with pytest.raises(RuntimeError, match="risk-free"):
            self.run(client, risk_free_fetcher=lambda: None)

        update = client.rows("scans", "update")[0]
        assert update["status"] == db.STATUS_FAILED

    def test_thin_history_is_excluded_rather_than_evaluated(self):
        client = FakeClient()

        report = self.run(client, downloader=batch_downloader(rising_frame(sessions=60)))

        assert report.evaluated_count == 0
        assert report.status == db.STATUS_FAILED

    def test_empty_universe_fails_without_touching_scan_results(self):
        client = FakeClient()

        report = self.run(client, market_cap_fetcher=lambda _: None)

        assert report.status == db.STATUS_FAILED
        # Nothing to write means no request at all, not an empty one.
        assert client.rows("scan_results", "upsert") == []


class TestFetchIvHistory:
    def test_reads_past_the_page_size_until_an_empty_page(self):
        # PostgREST's max-rows truncates pages silently, so a short page is
        # not proof of exhaustion — only an empty one is.
        client = FakeClient()
        client.select_rows["iv_snapshots"] = [
            {
                "symbol": "AAA",
                "snap_date": (AS_OF - timedelta(days=1400 - i)).isoformat(),
                "iv30": 0.20,
            }
            for i in range(1250)
        ]

        history = db.fetch_iv_history(client, since=AS_OF - timedelta(days=2000), until=AS_OF)

        assert len(history["AAA"]) == 1250
        selects = [payload for name, verb, payload in client.calls if verb == "select"]
        assert len(selects) == 3  # 1000 + 250 + the confirming empty page


# --------------------------------------------------------------------------
# Golden row (SPEC.md §10 integration)
# --------------------------------------------------------------------------


class TestGoldenRow:
    """One full scan over a fixed fixture must reproduce the checked-in row.

    Regenerate deliberately with:
        .venv/bin/python tests/regenerate_golden.py
    then hand-verify the diff before committing it.
    """

    def scan_row(self):
        client = FakeClient()
        run_scan.run_full_scan(
            client,
            seed=[SeedEntry("AAA", "Alpha", "Information Technology", "Software")],
            market_cap_fetcher={"AAA": 900e9}.get,
            downloader=batch_downloader(golden_frame()),
            fundamentals_fetcher=fake_fundamentals,
            expiries_fetcher=fake_expiries,
            chain_fetcher=fake_chain,
            risk_free_fetcher=lambda: 0.04,
            sleeper=lambda _: None,
        )
        return client.rows("scan_results", "upsert")[0][0]

    def test_matches_the_golden_file(self):
        row = self.scan_row()
        golden = json.loads((GOLDEN_DIR / "scan_row.json").read_text())

        assert sorted(row) == sorted(golden)
        for key, expected in golden.items():
            actual = row[key]
            if isinstance(expected, float):
                assert actual == pytest.approx(expected, rel=1e-9), key
            else:
                assert actual == expected, key

    def test_economics_reproduce_hand_calculations(self):
        # §10 acceptance: breakeven, cost %, spread math to the cent — checked
        # against the fixture chain (strike 180, bid 33.5, ask 34.5) directly,
        # independently of the golden file.
        row = self.scan_row()

        assert row["opt_strike"] == 180.0
        assert row["opt_mid"] == pytest.approx(34.0)
        assert row["breakeven"] == pytest.approx(214.0)
        assert row["breakeven_pct"] == pytest.approx(214.0 / 196.0 - 1.0)
        assert row["cost_pct_spot"] == pytest.approx(34.0 / 196.0)
        assert row["opt_spread_pct"] == pytest.approx(1.0 / 34.0)
        assert row["opt_dte"] == 371

    def test_composite_is_the_weighted_sum_of_its_parts(self):
        row = self.scan_row()

        assert row["score"] == pytest.approx(
            0.25 * row["s_trend"]
            + 0.25 * row["s_quality"]
            + 0.20 * row["s_option"]
            + 0.15 * row["s_valuation"]
            + 0.15 * row["s_entry"]
        )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


class TestCli:
    @pytest.mark.parametrize("command", ["full", "refresh"])
    def test_parser_accepts_both_commands(self, command):
        assert run_scan._build_parser().parse_args([command]).command == command

    def test_parser_rejects_unknown_command(self):
        with pytest.raises(SystemExit) as excinfo:
            run_scan._build_parser().parse_args(["backtest"])

        assert excinfo.value.code == 2

    def test_main_exits_nonzero_when_config_is_missing(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setattr("leaps_scanner.config.DEFAULT_ENV_FILE", tmp_path / "absent.env")
        monkeypatch.delenv("SUPABASE_URL", raising=False)
        monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)

        assert run_scan.main(["full"]) == 2
        assert "SUPABASE_URL" in capsys.readouterr().err

    def test_refresh_returns_nonzero_when_the_run_failed(self, monkeypatch):
        monkeypatch.setattr(db, "service_client", lambda: FakeClient())
        monkeypatch.setattr(
            run_scan,
            "run_daily_refresh",
            lambda client: run_scan.RefreshReport(
                as_of_date=AS_OF,
                universe_count=10,
                priced_count=10,
                iv_failures=0,
                positions_count=1,
                marks_count=0,
                alerts_count=0,
                status=db.STATUS_FAILED,
                notes="premium stop not evaluated",
            ),
        )

        assert run_scan.daily_refresh() == 1

    def test_refresh_returns_zero_on_a_complete_run(self, monkeypatch):
        monkeypatch.setattr(db, "service_client", lambda: FakeClient())
        monkeypatch.setattr(
            run_scan,
            "run_daily_refresh",
            lambda client: run_scan.RefreshReport(
                as_of_date=AS_OF,
                universe_count=10,
                priced_count=10,
                iv_failures=0,
                positions_count=1,
                marks_count=1,
                alerts_count=0,
                status=db.STATUS_OK,
                notes="",
            ),
        )

        assert run_scan.daily_refresh() == 0

    def test_full_scan_returns_nonzero_when_the_scan_failed(self, monkeypatch):
        monkeypatch.setattr(db, "service_client", lambda: FakeClient())
        monkeypatch.setattr(
            run_scan,
            "run_full_scan",
            lambda client: run_scan.ScanReport(
                as_of_date=AS_OF,
                universe_count=10,
                evaluated_count=1,
                missing_count=9,
                matches_count=0,
                status=db.STATUS_FAILED,
                notes="most of the universe is missing",
            ),
        )

        assert run_scan.full_scan() == 1

    def test_full_scan_returns_zero_on_a_complete_run(self, monkeypatch):
        monkeypatch.setattr(db, "service_client", lambda: FakeClient())
        monkeypatch.setattr(
            run_scan,
            "run_full_scan",
            lambda client: run_scan.ScanReport(
                as_of_date=AS_OF,
                universe_count=10,
                evaluated_count=10,
                missing_count=0,
                matches_count=3,
                status=db.STATUS_OK,
                notes="",
            ),
        )

        assert run_scan.full_scan() == 0


class TestMinMarketCapWiring:
    def test_pipeline_applies_the_spec_floor_when_none_is_passed(self):
        # Caps straddling $50B: only the one at or above it survives a call that
        # does not override min_market_cap.
        client = FakeClient()
        seed = [
            SeedEntry("BIG", "Big", "Information Technology", "Software"),
            SeedEntry("SMALL", "Small", "Industrials", "Widgets"),
        ]
        caps = {"BIG": universe.MIN_MARKET_CAP, "SMALL": universe.MIN_MARKET_CAP - 1}

        report = run_scan.run_full_scan(
            client,
            seed=seed,
            market_cap_fetcher=caps.get,
            downloader=batch_downloader(rising_frame()),
            fundamentals_fetcher=fake_fundamentals,
            expiries_fetcher=fake_expiries,
            chain_fetcher=fake_chain,
            risk_free_fetcher=lambda: 0.04,
            sleeper=lambda _: None,
        )

        assert report.universe_count == 1
        assert [r["symbol"] for r in client.rows("scan_results", "upsert")[0]] == ["BIG"]

    def test_the_floor_is_fifty_billion(self):
        assert universe.MIN_MARKET_CAP == 50e9


class TestExpectedAsOfDate:
    @pytest.mark.parametrize(
        ("today", "expected"),
        [
            ("2026-08-15", "2026-08-14"),  # Saturday, the cron slot
            ("2026-08-14", "2026-08-14"),  # Friday itself
            ("2026-08-17", "2026-08-14"),  # Monday
            ("2026-08-13", "2026-08-07"),  # Thursday: last week's Friday
        ],
    )
    def test_resolves_to_the_most_recent_friday(self, today, expected):
        resolved = run_scan.expected_as_of_date(pd.Timestamp(today).date())

        assert resolved.isoformat() == expected
        assert resolved.weekday() == 4


class TestScanRowLifecycle:
    SEED = [SeedEntry("AAA", "Alpha", "Information Technology", "Software")]

    def run(self, client, downloader):
        return run_scan.run_full_scan(
            client,
            seed=self.SEED,
            market_cap_fetcher={"AAA": 900e9}.get,
            downloader=downloader,
            fundamentals_fetcher=fake_fundamentals,
            expiries_fetcher=fake_expiries,
            chain_fetcher=fake_chain,
            risk_free_fetcher=lambda: 0.04,
            sleeper=lambda _: None,
        )

    def test_scan_row_is_opened_before_any_fetching(self):
        client = FakeClient()

        def downloader(symbols, period):
            # By the time a fetch happens, the scans row must already exist.
            assert ("scans", "insert") in [(n, v) for n, v, _ in client.calls]
            raise ConnectionError("yahoo down")

        self.run(client, downloader)

    def test_a_crash_mid_scan_still_closes_the_row_as_failed(self):
        # Supabase goes down after the scan row is open. The run must not leave
        # a row stuck at 'running' forever.
        client = FakeClient(fail_on=("scan_results", "upsert"))

        with pytest.raises(RuntimeError):
            self.run(client, batch_downloader(rising_frame()))

        update = client.rows("scans", "update")[0]
        assert update["status"] == db.STATUS_FAILED
        assert "RuntimeError" in update["notes"]

    def test_finish_corrects_the_provisional_date_from_the_signals(self):
        client = FakeClient()

        report = self.run(client, batch_downloader(rising_frame()))

        assert client.rows("scans", "update")[0]["as_of_date"] == report.as_of_date.isoformat()


# --------------------------------------------------------------------------
# Daily refresh (SPEC.md §3 `refresh`, §7)
# --------------------------------------------------------------------------


OWNER = "11111111-1111-1111-1111-111111111111"


def position_row(**overrides) -> dict:
    base = {
        "id": 1,
        "user_id": OWNER,
        "symbol": "AAA",
        "opened_on": "2026-05-01",
        "expiry": LEAP_EXPIRY.isoformat(),
        "strike": 180.0,
        "contracts": 2,
        "entry_premium": 34.0,
        "account_equity_at_entry": 250_000.0,
        "status": "open",
        "closed_on": None,
        "exit_premium": None,
    }
    base.update(overrides)
    return base


class TestResolveSessionDate:
    def test_the_majority_session_wins(self):
        frames = {
            "AAA": rising_frame(end="2026-08-14"),
            "BBB": rising_frame(end="2026-08-14"),
            "HALTED": rising_frame(end="2026-08-11"),
        }

        assert run_scan.resolve_session_date(frames, date(2000, 1, 1)) == date(2026, 8, 14)

    def test_no_frames_falls_back(self):
        assert run_scan.resolve_session_date({}, AS_OF) == AS_OF


class TestAssessRefresh:
    def test_a_clean_run_is_ok(self):
        status, notes = run_scan.assess_refresh(100, 100, 0, 1, [])

        assert status == db.STATUS_OK
        assert "100/100" in notes

    def test_an_unevaluated_exit_rule_fails_the_run_with_no_ceiling(self):
        # One position out of one, and the universe half of the job was
        # flawless — the run still fails. A missed stop is worse than a
        # reported one.
        status, notes = run_scan.assess_refresh(
            100, 100, 0, 1, ["position 1 (AAA): premium_stop not evaluated"]
        )

        assert status == db.STATUS_FAILED
        assert "premium_stop not evaluated" in notes

    def test_the_twenty_percent_ceiling_still_applies_to_the_universe(self):
        status, notes = run_scan.assess_refresh(100, 75, 0, 0, [])

        assert status == db.STATUS_FAILED
        assert "25.0%" in notes

    def test_iv_failures_count_toward_the_ceiling(self):
        status, _ = run_scan.assess_refresh(100, 100, 25, 0, [])

        assert status == db.STATUS_FAILED

    def test_an_empty_universe_says_to_run_a_full_scan(self):
        status, notes = run_scan.assess_refresh(0, 0, 0, 0, [])

        assert status == db.STATUS_FAILED
        assert "full scan" in notes


class TestRunDailyRefresh:
    def client_with(self, positions=(), alerts=(), symbols=("AAA", "BBB")):
        client = FakeClient()
        client.select_rows["tickers"] = [{"symbol": symbol} for symbol in symbols]
        client.select_rows["positions"] = list(positions)
        client.select_rows["alerts"] = list(alerts)
        client.select_rows["scans"] = [{"id": 5, "kind": "full", "as_of_date": AS_OF.isoformat()}]
        client.select_rows["scan_results"] = [
            {"symbol": "AAA", "next_earnings": (AS_OF + timedelta(days=10)).isoformat()}
        ]
        return client

    def run(self, client, **kwargs):
        arguments = dict(
            downloader=batch_downloader(rising_frame()),
            market_cap_fetcher=lambda symbol: 900e9,
            expiries_fetcher=fake_expiries,
            chain_fetcher=fake_chain,
            sleeper=lambda _: None,
            today=AS_OF,
        )
        arguments.update(kwargs)
        return run_scan.run_daily_refresh(client, **arguments)

    def test_it_refreshes_quotes_and_appends_snapshots_without_rescoring(self):
        client = self.client_with()

        report = self.run(client)

        assert report.status == db.STATUS_OK
        assert report.priced_count == 2
        # §3: "No rescoring" — the refresh must never touch scan_results.
        assert client.rows("scan_results", "upsert") == []
        assert client.rows("weekly_bars", "upsert") == []

        snapshots = client.rows("iv_snapshots", "upsert")[0]
        assert {row["symbol"] for row in snapshots} == {"AAA", "BBB"}
        assert all(row["snap_date"] == AS_OF.isoformat() for row in snapshots)

        tickers = client.rows("tickers", "upsert")[0]
        assert all(row["market_cap"] == 900e9 for row in tickers)

    def test_the_scan_row_is_a_refresh_and_never_claims_matches(self):
        client = self.client_with()

        self.run(client)

        opened = client.rows("scans", "insert")[0]
        assert opened["kind"] == "refresh"
        closed = client.rows("scans", "update")[0]
        assert closed["status"] == db.STATUS_OK
        # A refresh evaluates no filters, so a match count would be invented.
        assert closed["matches_count"] == 0

    def test_a_held_contract_is_marked_and_the_mark_is_kept(self):
        client = self.client_with(positions=[position_row()])

        report = self.run(client)

        assert report.marks_count == 1
        mark = client.rows("position_marks", "upsert")[0][0]
        assert mark["position_id"] == 1
        assert mark["mark_date"] == AS_OF.isoformat()
        # The 180 strike in the LEAP fixture quotes 33.5 / 34.5.
        assert mark["mid"] == pytest.approx(34.0)
        # Provenance: the mark names the run that took it.
        assert mark["scan_id"] == 7

    def test_a_halved_premium_fires_a_stop_that_quotes_its_mark(self):
        # Entry at 80 against a fixture mid of 34 — comfortably past the stop.
        client = self.client_with(positions=[position_row(entry_premium=80.0)])

        report = self.run(client)

        assert report.alerts_count >= 1
        alerts = client.rows("alerts", "insert")[0]
        stop = next(row for row in alerts if row["kind"] == "premium_stop")
        assert stop["as_of_date"] == AS_OF.isoformat()
        assert stop["scan_id"] == 7
        assert stop["user_id"] == OWNER
        assert "34.00" in stop["message"]
        assert AS_OF.isoformat() in stop["message"]

    def test_the_weekly_rule_does_not_run_on_the_daily_cadence(self):
        client = self.client_with(positions=[position_row()])

        self.run(client)

        written = client.rows("alerts", "insert")
        kinds = {row["kind"] for batch in written for row in batch}
        assert "stoch_below_20" not in kinds

    def test_an_unmarkable_holding_fails_the_run(self):
        # A strike that is not in the chain: the premium stop cannot be
        # evaluated, so the refresh must not report itself as complete.
        client = self.client_with(positions=[position_row(strike=999.0)])

        report = self.run(client)

        assert report.status == db.STATUS_FAILED
        assert "premium_stop not evaluated" in report.notes

    def test_an_existing_unacknowledged_alert_is_not_repeated(self):
        client = self.client_with(
            positions=[position_row(entry_premium=80.0)],
            alerts=[
                {
                    "id": 1,
                    "kind": "premium_stop",
                    "created_at": (AS_OF - timedelta(days=1)).isoformat(),
                    "position_id": 1,
                    "as_of_date": (AS_OF - timedelta(days=1)).isoformat(),
                    "acknowledged": False,
                }
            ],
        )

        self.run(client)

        written = client.rows("alerts", "insert")
        kinds = {row["kind"] for batch in written for row in batch}
        assert "premium_stop" not in kinds

    def test_an_unknown_earnings_date_does_not_fail_the_run(self):
        # §7 calls the earnings heads-up informational, and a held name outside
        # the last full scan has no stored date. Losing the heads-up is not
        # losing an exit, and a build that goes red over it gets ignored.
        client = self.client_with(positions=[position_row(symbol="GONE")], symbols=("AAA",))

        report = self.run(client)

        assert report.status == db.STATUS_OK

    def test_a_held_name_missing_from_the_universe_is_still_priced(self):
        client = self.client_with(positions=[position_row(symbol="GONE")], symbols=("AAA",))

        self.run(client)

        # Priced and evaluated, but not written back into `tickers`: universe
        # membership is the full scan's decision.
        assert {row["symbol"] for row in client.rows("tickers", "upsert")[0]} == {"AAA"}

    def test_a_total_price_failure_is_recorded_rather_than_raised(self):
        def exploding_downloader(symbols, period):
            raise RuntimeError("Yahoo is down")

        client = self.client_with()
        report = self.run(client, downloader=exploding_downloader)

        # yfinance failures are absorbed by the retry loop, so the run finishes
        # — but it finishes `failed`, over §9's ceiling.
        assert report.status == db.STATUS_FAILED
        assert report.priced_count == 0
        assert client.rows("scans", "update")[0]["status"] == db.STATUS_FAILED

    def test_a_crashed_refresh_still_closes_its_scan_row(self):
        # A database read that dies mid-run must leave a `failed` row, not a
        # row stuck on `running` forever.
        client = self.client_with()
        client._fail_on = ("tickers", "select")

        with pytest.raises(RuntimeError):
            self.run(client)

        closed = client.rows("scans", "update")[0]
        assert closed["status"] == db.STATUS_FAILED
        assert "refresh aborted" in closed["notes"]

    def test_no_positions_means_no_alerts_and_a_clean_run(self):
        client = self.client_with()

        report = self.run(client)

        assert report.positions_count == 0
        assert report.alerts_count == 0
        assert client.rows("alerts", "insert") == []
        assert report.status == db.STATUS_OK


class TestFullScanExitMonitor:
    """SPEC.md §3.7: the weekly scan runs §7's weekly rule and only that one."""

    def breaking_frame(self) -> pd.DataFrame:
        """A long rise, then three weeks down hard enough that the weekly slow
        %K crosses below 20 on the final completed bar."""
        values = np.concatenate([np.linspace(100, 200, 455), np.linspace(200, 170, 15)])
        index = pd.bdate_range(end="2026-08-14", periods=len(values))
        return pd.DataFrame(
            {
                "Open": values,
                "High": values * 1.01,
                "Low": values * 0.99,
                "Close": values,
                "Volume": np.full(len(values), 2_000_000.0),
            },
            index=index,
        )

    def client_holding(self, **overrides):
        client = FakeClient()
        client.select_rows["positions"] = [position_row(**overrides)]
        client.select_rows["alerts"] = []
        return client

    def run(self, client, frame):
        return run_scan.run_full_scan(
            client,
            seed=[SeedEntry("AAA", "Alpha", "Information Technology", "Software")],
            market_cap_fetcher=lambda symbol: 900e9,
            downloader=batch_downloader(frame),
            fundamentals_fetcher=fake_fundamentals,
            expiries_fetcher=fake_expiries,
            chain_fetcher=fake_chain,
            risk_free_fetcher=lambda: 0.04,
            sleeper=lambda _: None,
        )

    def test_a_weekly_cross_below_20_writes_an_alert(self):
        client = self.client_holding()

        self.run(client, self.breaking_frame())

        written = client.rows("alerts", "insert")[0]
        assert [row["kind"] for row in written] == ["stoch_below_20"]
        alert = written[0]
        assert alert["position_id"] == 1
        assert alert["user_id"] == OWNER
        assert alert["scan_id"] == 7
        # The alert speaks for the same week the scan does.
        assert alert["as_of_date"] == AS_OF.isoformat()
        assert "crossed below 20" in alert["message"]

    def test_a_healthy_week_writes_nothing(self):
        client = self.client_holding()

        self.run(client, rising_frame())

        assert client.rows("alerts", "insert") == []

    def test_the_daily_rules_are_left_to_the_refresh(self):
        # This contract expires in 10 days and the entry premium is far above
        # any plausible mark — both daily rules would fire. §7 gives them a
        # daily cadence, and the refresh is where they run.
        client = self.client_holding(expiry=(AS_OF + timedelta(days=10)).isoformat())

        self.run(client, rising_frame())

        assert client.rows("alerts", "insert") == []

    def test_a_held_name_the_scan_never_priced_fails_the_scan(self):
        client = self.client_holding(symbol="ABSENT")

        report = self.run(client, rising_frame())

        assert report.status == db.STATUS_FAILED
        assert "stoch_below_20 not evaluated" in report.notes

    def test_no_positions_leaves_the_scan_untouched(self):
        client = FakeClient()

        report = self.run(client, rising_frame())

        assert report.status == db.STATUS_OK
        assert client.rows("alerts", "insert") == []
