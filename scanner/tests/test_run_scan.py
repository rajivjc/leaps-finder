"""Pipeline tests (SPEC.md §3, §9). No network and no database: both are faked."""

import numpy as np
import pandas as pd
import pytest

from leaps_scanner import db, run_scan, universe
from leaps_scanner.indicators import Signals
from leaps_scanner.universe import SeedEntry

VALID_ENV = {
    "SUPABASE_URL": "https://example.supabase.co",
    "SUPABASE_SERVICE_KEY": "service-key-value",
}


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, data):
        self.data = data


class FakeTable:
    """Records the PostgREST calls the scanner makes, in order."""

    def __init__(self, name, log, fail_on=None):
        self._name = name
        self._log = log
        self._fail_on = fail_on
        self._pending = None

    def insert(self, rows):
        self._pending = ("insert", rows)
        return self

    def upsert(self, rows, on_conflict=None):
        self._pending = ("upsert", rows)
        return self

    def update(self, values):
        self._pending = ("update", values)
        return self

    def eq(self, column, value):
        return self

    def execute(self):
        verb, payload = self._pending
        if self._fail_on == (self._name, verb):
            raise RuntimeError(f"PostgREST 503 on {self._name}.{verb}")
        self._log.append((self._name, verb, payload))
        if self._name == "scans" and verb == "insert":
            return FakeResponse([{"id": 7}])
        return FakeResponse(payload if isinstance(payload, list) else [payload])


class FakeClient:
    def __init__(self, fail_on=None):
        self.calls = []
        self._fail_on = fail_on

    def table(self, name):
        return FakeTable(name, self.calls, self._fail_on)

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


def signals(**overrides) -> Signals:
    base = dict(
        as_of_date=pd.Timestamp("2026-08-14").date(),
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
    )
    return Signals(**{**base, **overrides})


# --------------------------------------------------------------------------
# Pure logic
# --------------------------------------------------------------------------


class TestIsMatch:
    def test_all_implemented_filters_must_pass(self):
        assert run_scan.is_match(signals()) is True

    @pytest.mark.parametrize("failing", ["trend_pass", "in_zone", "turning_up"])
    def test_any_single_failure_disqualifies(self, failing):
        assert run_scan.is_match(signals(**{failing: False})) is False


class TestBuildScanRow:
    def test_carries_every_m2_signal(self):
        row = run_scan.build_scan_row(7, "AAPL", signals())

        assert row["scan_id"] == 7
        assert row["symbol"] == "AAPL"
        assert row["stoch_k"] == 45.0
        assert row["stoch_k_prev"] == 42.0
        assert row["trend_pass"] is True

    def test_leaves_later_milestone_columns_unset(self):
        # Writing nulls for scoring columns would be harmless; writing zeros
        # would not. Nothing this milestone cannot compute gets a value.
        row = run_scan.build_scan_row(7, "AAPL", signals())

        for column in ("score", "s_trend", "opt_strike", "iv_rank", "passes_strict"):
            assert column not in row


class TestResolveAsOfDate:
    def test_uses_the_week_most_symbols_completed(self):
        early = pd.Timestamp("2026-08-07").date()
        current = pd.Timestamp("2026-08-14").date()

        resolved = run_scan.resolve_as_of_date(
            {
                "A": signals(as_of_date=current),
                "B": signals(as_of_date=current),
                "C": signals(as_of_date=early),  # halted into Friday
            }
        )

        assert resolved == current

    def test_ties_break_to_the_later_week(self):
        early = pd.Timestamp("2026-08-07").date()
        current = pd.Timestamp("2026-08-14").date()

        resolved = run_scan.resolve_as_of_date(
            {"A": signals(as_of_date=early), "B": signals(as_of_date=current)}
        )

        assert resolved == current

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
        def default_downloader(symbols, period):
            columns = pd.MultiIndex.from_product(
                [list(symbols), ["Open", "High", "Low", "Close", "Volume"]]
            )
            frame = rising_frame()
            return pd.DataFrame(
                np.tile(frame.values, (1, len(symbols))), index=frame.index, columns=columns
            )

        return run_scan.run_full_scan(
            client,
            seed=self.SEED,
            market_cap_fetcher=self.CAPS.get,
            downloader=downloader or default_downloader,
            **kwargs,
        )

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

    def test_average_volume_is_written_back_to_tickers(self):
        client = FakeClient()

        self.run(client)

        volume_rows = client.rows("tickers", "upsert")[1]
        assert all(row["avg_volume_30d"] == pytest.approx(2_000_000.0) for row in volume_rows)

    def test_losing_most_of_the_universe_fails_the_scan(self):
        client = FakeClient()

        def broken(symbols, period):
            raise ConnectionError("yahoo down")

        report = self.run(client, downloader=broken)

        assert report.status == db.STATUS_FAILED
        assert report.ok is False
        assert client.rows("scans", "update")[0]["status"] == db.STATUS_FAILED

    def test_thin_history_is_excluded_rather_than_evaluated(self):
        client = FakeClient()

        def short(symbols, period):
            columns = pd.MultiIndex.from_product(
                [list(symbols), ["Open", "High", "Low", "Close", "Volume"]]
            )
            frame = rising_frame(sessions=60)
            return pd.DataFrame(
                np.tile(frame.values, (1, len(symbols))), index=frame.index, columns=columns
            )

        report = self.run(client, downloader=short)

        assert report.evaluated_count == 0
        assert report.status == db.STATUS_FAILED

    def test_empty_universe_fails_without_touching_scan_results(self):
        client = FakeClient()

        report = run_scan.run_full_scan(
            client,
            seed=self.SEED,
            market_cap_fetcher=lambda _: None,
            downloader=lambda symbols, period: pd.DataFrame(),
        )

        assert report.status == db.STATUS_FAILED
        # Nothing to write means no request at all, not an empty one.
        assert client.rows("scan_results", "upsert") == []


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

    def test_refresh_is_still_a_later_milestone(self, monkeypatch):
        for key, value in VALID_ENV.items():
            monkeypatch.setenv(key, value)

        with pytest.raises(NotImplementedError):
            run_scan.main(["refresh"])

    def test_full_scan_returns_nonzero_when_the_scan_failed(self, monkeypatch):
        monkeypatch.setattr(db, "service_client", lambda: FakeClient())
        monkeypatch.setattr(
            run_scan,
            "run_full_scan",
            lambda client: run_scan.ScanReport(
                as_of_date=pd.Timestamp("2026-08-14").date(),
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
                as_of_date=pd.Timestamp("2026-08-14").date(),
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

        def downloader(symbols, period):
            columns = pd.MultiIndex.from_product(
                [list(symbols), ["Open", "High", "Low", "Close", "Volume"]]
            )
            frame = rising_frame()
            return pd.DataFrame(
                np.tile(frame.values, (1, len(symbols))), index=frame.index, columns=columns
            )

        report = run_scan.run_full_scan(
            client, seed=seed, market_cap_fetcher=caps.get, downloader=downloader
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

    def test_scan_row_is_opened_before_any_fetching(self):
        client = FakeClient()

        def downloader(symbols, period):
            # By the time a fetch happens, the scans row must already exist.
            assert ("scans", "insert") in [(n, v) for n, v, _ in client.calls]
            raise ConnectionError("yahoo down")

        run_scan.run_full_scan(
            client,
            seed=self.SEED,
            market_cap_fetcher={"AAA": 900e9}.get,
            downloader=downloader,
        )

    def test_a_crash_mid_scan_still_closes_the_row_as_failed(self):
        # Supabase goes down after the scan row is open. The run must not leave
        # a row stuck at 'running' forever.
        client = FakeClient(fail_on=("scan_results", "upsert"))

        def downloader(symbols, period):
            columns = pd.MultiIndex.from_product(
                [list(symbols), ["Open", "High", "Low", "Close", "Volume"]]
            )
            frame = rising_frame()
            return pd.DataFrame(
                np.tile(frame.values, (1, len(symbols))), index=frame.index, columns=columns
            )

        with pytest.raises(RuntimeError):
            run_scan.run_full_scan(
                client,
                seed=self.SEED,
                market_cap_fetcher={"AAA": 900e9}.get,
                downloader=downloader,
            )

        update = client.rows("scans", "update")[0]
        assert update["status"] == db.STATUS_FAILED
        assert "RuntimeError" in update["notes"]

    def test_finish_corrects_the_provisional_date_from_the_signals(self):
        client = FakeClient()

        def downloader(symbols, period):
            columns = pd.MultiIndex.from_product(
                [list(symbols), ["Open", "High", "Low", "Close", "Volume"]]
            )
            frame = rising_frame()
            return pd.DataFrame(
                np.tile(frame.values, (1, len(symbols))), index=frame.index, columns=columns
            )

        report = run_scan.run_full_scan(
            client,
            seed=self.SEED,
            market_cap_fetcher={"AAA": 900e9}.get,
            downloader=downloader,
        )

        assert client.rows("scans", "update")[0]["as_of_date"] == report.as_of_date.isoformat()
