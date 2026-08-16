"""B4 tests: SPEC-BACKTEST.md §6.2, §6.3 and §9, acceptance criteria 5 and 6.

Two things here are load-bearing rather than incidental.

`TestCircuitBreaker` drives the sleeve until it actually trips, including the
case §6.2 singles out: every position closed, so there is no open position to
take a denominator from. That is the moment a naive breaker goes silent — no
open positions, no `account_equity_at_entry`, no loss fraction — and it is
precisely the moment a four-week entry ban is most warranted.

`TestEntryRules` pins each of §6.2's five refusal causes on `decide_entry`
directly. Contriving a ten-year price path that trips the exposure cap without
first tripping the slot cap is possible but would test the fixture rather than
the rule; the walk-level tests below cover the causes that arise naturally.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from test_backtest_engine import business_days, rate_frame

from leaps_scanner import risk
from leaps_scanner.backtest import data, engine, sleeve, synthetic

REPO_ROOT = Path(__file__).resolve().parents[2]

FIXTURE_START = date(2020, 1, 1)
FIXTURE_END = date(2022, 12, 30)
WINDOW = data.Window(start=date(2021, 1, 4), end=date(2022, 12, 30), fetch_start=FIXTURE_START)


def make_trade(
    chain: str,
    entry: date,
    exit_: date,
    *,
    symbol: str | None = None,
    slow_k: float = 35.0,
    entry_cost: float = 10.0,
    exit_value: float = 12.0,
    spot: float = 100.0,
    exit_price: float = 110.0,
    exit_reason: str = "trend_break",
) -> synthetic.SyntheticTrade:
    """One overlay trade, built directly rather than replayed.

    The sleeve consumes finished `SyntheticTrade`s, so a test of §6.2's
    discipline can hand it exactly the candidate set it wants to reason about.
    Driving the engine to produce a specific slow-K collision on a specific day
    would test the price path, not the tie-break.
    """
    trade = engine.Trade(
        chain=chain,
        symbol=symbol or chain,
        signal_date=entry - timedelta(days=1),
        entry_date=entry,
        entry_price=spot,
        exit_date=exit_,
        exit_price=exit_price,
        exit_reason=exit_reason,
        exit_signal_date=None,
        entry_slow_k=slow_k,
    )
    return synthetic.SyntheticTrade(
        trade=trade,
        strike=spot * 0.95,
        sigma=0.30,
        rate=0.01,
        dividend_yield=0.0,
        expiry=entry + timedelta(days=synthetic.TENOR_DAYS),
        entry_cost=entry_cost,
        exit_value=exit_value,
    )


def flat_cache(root: Path, chains: list[str], *, price: float = 100.0) -> data.PriceCache:
    """A cache of flat series, so every daily mark is a known constant."""
    days = business_days(FIXTURE_START, FIXTURE_END)
    cache = data.PriceCache(root / "cache")
    frame = pd.DataFrame(
        {
            "Open": np.full(len(days), price),
            "High": np.full(len(days), price),
            "Low": np.full(len(days), price),
            "Close": np.full(len(days), price),
            "Adj Close": np.full(len(days), price),
            "Volume": np.full(len(days), 1_000_000.0),
        },
        index=pd.DatetimeIndex([pd.Timestamp(day) for day in days]),
    )
    for chain in chains:
        cache.store(chain, frame)
    cache.store(data.BENCHMARK_SYMBOL, frame)
    cache.store(data.RATE_SYMBOL, rate_frame(days))
    return cache


#: `run_sleeve`'s default gives every symbol a sector of its own, so the
#: 2-per-sector cap never fires unless a test is about the 2-per-sector cap.
#: An empty map is a *choice* a test makes, not the absence of one — it puts
#: every name in the `Unknown` bucket, which is a different scenario entirely.
DISTINCT_SECTORS: dict[str, str] = {}


def run_sleeve(
    root: Path,
    trades: list[synthetic.SyntheticTrade],
    *,
    sectors: dict[str, str] = DISTINCT_SECTORS,
    config: sleeve.SleeveConfig = sleeve.CONFIGS[0],
) -> sleeve.SleeveResult:
    chains = sorted({trade.trade.chain for trade in trades})
    if sectors is DISTINCT_SECTORS:
        sectors = {trade.trade.symbol: f"Sector::{trade.trade.symbol}" for trade in trades}
    cache = flat_cache(root, chains)
    labels = sleeve.SectorLabels(sectors)
    return sleeve.simulate(trades, cache, WINDOW, config=config, sectors=labels)


def causes(result: sleeve.SleeveResult) -> dict[str, int]:
    return {reason: count for reason, count in result.skipped_counts.items() if count}


class TestTieBreak:
    """§9 / P10: three signals, one slot, resolved deterministically."""

    def test_the_closest_slow_k_to_35_wins_the_last_slot(self, tmp_path: Path) -> None:
        entry = date(2021, 3, 1)
        exit_ = date(2021, 6, 1)
        # Four already-open positions leave exactly one slot for the three
        # competitors that arrive together.
        occupied = [
            make_trade(f"HOLD{index}", date(2021, 2, 1), date(2021, 9, 1)) for index in range(4)
        ]
        competitors = [
            make_trade("FAR", entry, exit_, slow_k=60.0),
            make_trade("NEAR", entry, exit_, slow_k=33.0),
            make_trade("MID", entry, exit_, slow_k=45.0),
        ]
        result = run_sleeve(tmp_path, [*occupied, *competitors])

        funded = {position.chain for position in result.positions}
        assert "NEAR" in funded, "|33 − 35| = 2 is the smallest distance"
        assert not {"FAR", "MID"} & funded
        assert causes(result) == {sleeve.SKIP_SLOTS: 2}

    def test_an_exact_tie_falls_back_to_the_chain_alphabetically(self, tmp_path: Path) -> None:
        entry = date(2021, 3, 1)
        exit_ = date(2021, 6, 1)
        occupied = [
            make_trade(f"HOLD{index}", date(2021, 2, 1), date(2021, 9, 1)) for index in range(4)
        ]
        # Equidistant from 35 on either side, so only the alphabetical rule can
        # separate them — and it must, or the result depends on input order.
        competitors = [
            make_trade("ZZZ", entry, exit_, slow_k=30.0),
            make_trade("AAA", entry, exit_, slow_k=40.0),
        ]
        result = run_sleeve(tmp_path, [*occupied, *competitors])
        assert {p.chain for p in result.positions} & {"AAA", "ZZZ"} == {"AAA"}

    def test_the_order_does_not_depend_on_the_input_sequence(self, tmp_path: Path) -> None:
        entry = date(2021, 3, 1)
        exit_ = date(2021, 6, 1)
        occupied = [
            make_trade(f"HOLD{index}", date(2021, 2, 1), date(2021, 9, 1)) for index in range(4)
        ]
        competitors = [
            make_trade("BBB", entry, exit_, slow_k=36.0),
            make_trade("CCC", entry, exit_, slow_k=34.0),
            make_trade("AAA", entry, exit_, slow_k=50.0),
        ]
        forward = run_sleeve(tmp_path / "a", [*occupied, *competitors])
        reversed_ = run_sleeve(tmp_path / "b", [*occupied, *reversed(competitors)])
        assert [p.chain for p in forward.positions] == [p.chain for p in reversed_.positions]

    def test_a_signal_with_no_recorded_slow_k_sorts_last(self, tmp_path: Path) -> None:
        # NaN compares False against everything; unsorted it would land wherever
        # the input happened to put it, which §10.1's determinism forbids.
        entry = date(2021, 3, 1)
        exit_ = date(2021, 6, 1)
        occupied = [
            make_trade(f"HOLD{index}", date(2021, 2, 1), date(2021, 9, 1)) for index in range(4)
        ]
        competitors = [
            make_trade("AAA", entry, exit_, slow_k=float("nan")),
            make_trade("ZZZ", entry, exit_, slow_k=70.0),
        ]
        result = run_sleeve(tmp_path, [*occupied, *competitors])
        assert {p.chain for p in result.positions} & {"AAA", "ZZZ"} == {"ZZZ"}


class TestCaps:
    """§6.2's caps, each with its own counted cause."""

    def test_at_most_five_positions_are_open_at_once(self, tmp_path: Path) -> None:
        trades = [
            make_trade(f"C{index}", date(2021, 3, 1), date(2021, 9, 1), symbol=f"C{index}")
            for index in range(8)
        ]
        sectors = {f"C{index}": f"Sector{index}" for index in range(8)}
        result = run_sleeve(tmp_path, trades, sectors=sectors)

        assert len(result.positions) == sleeve.MAX_OPEN_POSITIONS
        assert causes(result) == {sleeve.SKIP_SLOTS: 3}
        assert max(result.curve_equity) > 0

    def test_at_most_two_positions_per_sector(self, tmp_path: Path) -> None:
        trades = [
            make_trade(f"T{index}", date(2021, 3, 1), date(2021, 9, 1), symbol=f"T{index}")
            for index in range(4)
        ]
        sectors = dict.fromkeys([f"T{index}" for index in range(4)], "Information Technology")
        result = run_sleeve(tmp_path, trades, sectors=sectors)

        assert len(result.positions) == sleeve.MAX_PER_SECTOR
        assert causes(result) == {sleeve.SKIP_SECTOR: 2}

    def test_unlabelled_names_share_one_bucket_and_the_cap_binds_across_them(
        self, tmp_path: Path
    ) -> None:
        """RC's 2026-08-16 call: the `Unknown` bucket tightens the cap.

        Four unrelated companies with no sector label are held to two positions
        between them, which is deliberately conservative — the alternative
        (exempting them) would let the sleeve hold more than §7's discipline
        allows and flatter the result.
        """
        trades = [
            make_trade(f"U{index}", date(2021, 3, 1), date(2021, 9, 1), symbol=f"U{index}")
            for index in range(4)
        ]
        result = run_sleeve(tmp_path, trades, sectors={})

        assert {position.sector for position in result.positions} == {sleeve.SECTOR_UNKNOWN}
        assert len(result.positions) == sleeve.MAX_PER_SECTOR
        assert causes(result) == {sleeve.SKIP_SECTOR: 2}
        assert result.unlabelled_symbols == ("U0", "U1", "U2", "U3")

    def test_a_labelled_name_is_not_pulled_into_the_unknown_bucket(self, tmp_path: Path) -> None:
        trades = [
            make_trade("U0", date(2021, 3, 1), date(2021, 9, 1), symbol="U0"),
            make_trade("U1", date(2021, 3, 1), date(2021, 9, 1), symbol="U1"),
            make_trade("K0", date(2021, 3, 1), date(2021, 9, 1), symbol="K0"),
        ]
        result = run_sleeve(tmp_path, trades, sectors={"K0": "Utilities"})
        assert len(result.positions) == 3
        assert {p.chain: p.sector for p in result.positions}["K0"] == "Utilities"


class TestSizing:
    """P9: integer contracts, and what granularity costs at small equity."""

    def test_contracts_follow_spec_md_7s_formula(self) -> None:
        # 3% of 100,000 = 3,000; a $10.34 premium costs $1,034 a contract.
        assert sleeve._contracts(100_000.0, 10.34) == 2
        assert sleeve._contracts(100_000.0, 30.0) == 1
        assert sleeve._contracts(100_000.0, 30.01) == 0

    def test_zero_contracts_skips_the_entry_and_counts_it(self, tmp_path: Path) -> None:
        # $50 a share is $5,000 a contract against a $3,000 budget.
        trades = [make_trade("RICH", date(2021, 3, 1), date(2021, 9, 1), entry_cost=50.0)]
        result = run_sleeve(tmp_path, trades)

        assert result.positions == ()
        assert causes(result) == {sleeve.SKIP_GRANULARITY: 1}
        assert result.final_equity == pytest.approx(sleeve.DEFAULT_START_EQUITY)

    def test_the_250k_sensitivity_funds_what_100k_cannot(self, tmp_path: Path) -> None:
        """P9's E₀ = $250k run exists to probe exactly this."""
        trades = [make_trade("RICH", date(2021, 3, 1), date(2021, 9, 1), entry_cost=50.0)]
        small = run_sleeve(tmp_path / "small", trades, config=sleeve.CONFIGS[0])
        large = run_sleeve(tmp_path / "large", trades, config=sleeve.CONFIGS[1])

        assert small.positions == ()
        assert len(large.positions) == 1
        assert large.positions[0].contracts == 1  # 3% of 250k = 7,500 / 5,000


class TestEntryRules:
    """§6.2's rule table, pinned directly — including the order of the checks."""

    BASE = dict(
        trade=make_trade("X", date(2021, 3, 1), date(2021, 9, 1)),
        sector="Utilities",
        today=date(2021, 3, 1),
        equity=100_000.0,
        cash=100_000.0,
        open_cost=0.0,
        open_positions=(),
        banned_through=None,
    )

    def decide(self, **overrides: object) -> tuple[str | None, int]:
        return sleeve.decide_entry(**{**self.BASE, **overrides})  # type: ignore[arg-type]

    def test_a_clean_entry_is_funded(self) -> None:
        assert self.decide() == (None, 3)  # 3,000 budget / $1,000 a contract

    def test_the_exposure_cap_declines_when_open_premium_would_exceed_15_percent(self) -> None:
        # 13,000 already at cost plus 3,000 more is 16% of equity.
        assert self.decide(open_cost=13_000.0)[0] == sleeve.SKIP_EXPOSURE
        # Exactly 15% is inside the cap: §6.2 writes it as "≤".
        assert self.decide(open_cost=12_000.0)[0] is None

    def test_the_sleeve_cannot_spend_cash_it_does_not_hold(self) -> None:
        assert self.decide(cash=100.0)[0] == sleeve.SKIP_EXPOSURE

    def test_the_breaker_bans_entries_through_its_last_day_inclusive(self) -> None:
        assert self.decide(banned_through=date(2021, 3, 1))[0] == sleeve.SKIP_BREAKER
        assert self.decide(banned_through=date(2021, 2, 28))[0] is None

    def test_the_breaker_outranks_every_cap(self) -> None:
        """A banned entry is recorded as banned, not as whatever else also held.

        Otherwise the breaker's own count would read as zero in exactly the runs
        where it mattered most — the ones where the sleeve was also full.
        """
        reason, _ = self.decide(
            banned_through=date(2021, 6, 1),
            open_cost=90_000.0,
            cash=0.0,
        )
        assert reason == sleeve.SKIP_BREAKER

    def test_a_declined_entry_sizes_nothing(self) -> None:
        for overrides in (
            {"banned_through": date(2021, 6, 1)},
            {"open_cost": 90_000.0},
            {"trade": make_trade("X", date(2021, 3, 1), date(2021, 9, 1), entry_cost=50.0)},
        ):
            assert self.decide(**overrides)[1] == 0


class TestCircuitBreaker:
    """§6.2's breaker, on `risk.py`'s arithmetic (§9's named fixture)."""

    LOSS = 0.02  # exits at 2% of cost: a near-total loss on each position

    def losing_trades(self, entries: list[date], *, held_days: int = 5) -> list:
        return [
            make_trade(
                f"L{index}",
                entry,
                entry + timedelta(days=held_days),
                symbol=f"L{index}",
                entry_cost=10.0,
                exit_value=10.0 * self.LOSS,
                exit_reason="premium_stop",
            )
            for index, entry in enumerate(entries)
        ]

    def sectors(self, count: int) -> dict[str, str]:
        # One sector each, so the 2-per-sector cap never masks the breaker.
        return {f"L{index}": f"Sector{index}" for index in range(count)}

    def test_it_trips_on_realized_losses_with_every_position_closed(self, tmp_path: Path) -> None:
        """The all-positions-closed case §6.2 calls out by name.

        Three positions sized at 3% of equity each and closed at 2% of cost
        realize about 8.8% of starting equity. Nothing is open when the breaker
        is evaluated, so its denominator has to come from `risk.breaker_equity`'s
        recently-closed half — a breaker reading only open positions would be
        silent here.
        """
        entries = [date(2021, 3, 1), date(2021, 3, 2), date(2021, 3, 3)]
        trades = self.losing_trades(entries)
        result = run_sleeve(tmp_path, trades, sectors=self.sectors(3))

        assert len(result.positions) == 3, "all three must be funded before any can lose"
        assert result.breaker_dates, "an ~8.8% realized loss is past the 8% breaker"
        # Evaluated at the close, so the ban starts the following session.
        assert min(result.breaker_dates) >= max(entries)

    def test_a_tripped_breaker_blocks_the_next_entry(self, tmp_path: Path) -> None:
        entries = [date(2021, 3, 1), date(2021, 3, 2), date(2021, 3, 3)]
        trades = self.losing_trades(entries)
        # A clean signal a fortnight later, well inside the 28-day ban.
        blocked = make_trade("AFTER", date(2021, 3, 22), date(2021, 6, 1), symbol="AFTER")
        result = run_sleeve(
            tmp_path, [*trades, blocked], sectors={**self.sectors(3), "AFTER": "Energy"}
        )

        assert result.breaker_dates
        assert "AFTER" not in {position.chain for position in result.positions}
        assert causes(result).get(sleeve.SKIP_BREAKER, 0) >= 1

    def test_the_ban_lifts_after_28_days(self, tmp_path: Path) -> None:
        entries = [date(2021, 3, 1), date(2021, 3, 2), date(2021, 3, 3)]
        trades = self.losing_trades(entries)
        result_blocked = run_sleeve(tmp_path / "blocked", trades, sectors=self.sectors(3))
        last_trip = max(result_blocked.breaker_dates)

        # One session past the ban, and past the 28-day realized window too, so
        # the losses no longer count and cannot re-trip it.
        later = last_trip + timedelta(days=risk.CIRCUIT_BREAKER_DAYS + 3)
        allowed = make_trade("AFTER", later, later + timedelta(days=60), symbol="AFTER")
        result = run_sleeve(
            tmp_path / "lifted", [*trades, allowed], sectors={**self.sectors(3), "AFTER": "Energy"}
        )
        assert "AFTER" in {position.chain for position in result.positions}

    def test_losses_outside_the_28_day_window_do_not_trip_it(self, tmp_path: Path) -> None:
        """The trailing window is what makes it a circuit breaker, not a latch.

        The same three losses spread over nine months never put 8% inside any
        28-day window, so the sleeve stays open for business.
        """
        entries = [date(2021, 3, 1), date(2021, 7, 1), date(2021, 11, 1)]
        result = run_sleeve(tmp_path, self.losing_trades(entries), sectors=self.sectors(3))

        assert len(result.positions) == 3
        assert result.breaker_dates == ()

    def test_an_empty_sleeve_has_no_denominator_and_no_breaker(self, tmp_path: Path) -> None:
        # No position ever carried an equity snapshot, so the loss fraction is
        # undefined — which must read as "not measurable", never as a trip.
        result = run_sleeve(tmp_path, [])
        assert result.breaker_dates == ()
        assert result.positions == ()
        assert result.win_rate is None, "no closed positions is not a 0% win rate"
        assert result.final_equity == pytest.approx(sleeve.DEFAULT_START_EQUITY)


class TestSleeveResult:
    """The reported figures, and the null-is-not-zero rule they follow."""

    def test_positions_are_a_strict_subset_of_the_overlay_trades(self, tmp_path: Path) -> None:
        """The filter reading of §6.2, asserted rather than assumed."""
        trades = [
            make_trade(f"C{index}", date(2021, 3, 1), date(2021, 9, 1), symbol=f"C{index}")
            for index in range(8)
        ]
        result = run_sleeve(tmp_path, trades, sectors={f"C{i}": f"S{i}" for i in range(8)})

        offered = {(trade.trade.chain, trade.trade.entry_date) for trade in trades}
        funded = {(position.chain, position.entry_date) for position in result.positions}
        assert funded < offered
        assert len(funded) + len(result.skipped) == len(offered)

    def test_a_winning_position_moves_equity_by_its_own_p_and_l(self, tmp_path: Path) -> None:
        # 3% of 100,000 is 3,000; at $10 a share that is 3 contracts costing
        # $3,000, sold for $12 a share = $3,600. The sleeve gains exactly $600.
        trades = [make_trade("WIN", date(2021, 3, 1), date(2021, 9, 1))]
        result = run_sleeve(tmp_path, trades)

        (position,) = result.positions
        assert position.contracts == 3
        assert position.cost_basis == pytest.approx(3_000.0)
        assert position.proceeds == pytest.approx(3_600.0)
        assert result.final_equity == pytest.approx(100_600.0)
        assert result.wins == 1
        assert result.win_rate == pytest.approx(1.0)

    def test_every_cause_is_reported_even_when_it_never_fired(self, tmp_path: Path) -> None:
        """Absent and zero are different claims about a rule."""
        result = run_sleeve(tmp_path, [make_trade("A", date(2021, 3, 1), date(2021, 9, 1))])
        assert set(result.skipped_counts) == set(sleeve.SKIP_REASONS)
        assert result.skipped_counts[sleeve.SKIP_SECTOR] == 0

    def test_an_empty_sleeve_reports_nulls_not_zeroes(self, tmp_path: Path) -> None:
        result = run_sleeve(tmp_path, [])
        assert result.win_rate is None
        assert result.total_return == pytest.approx(0.0), "equity really did not move"
        assert result.cagr == pytest.approx(0.0)
        assert result.max_drawdown == pytest.approx(0.0)

    def test_the_low_coverage_warning_rides_on_the_result(self, tmp_path: Path) -> None:
        """Acceptance 2: the sleeve's table is a headline table."""
        thin = data.Coverage(
            window=WINDOW,
            run_date=date(2022, 12, 31),
            snapshot_date="2022-12-31",
            total_member_weeks=1000,
            covered_member_weeks=830,
            members=10,
            no_data_members=2,
        )
        cache = flat_cache(tmp_path, ["A"])
        result = sleeve.simulate(
            [make_trade("A", date(2021, 3, 1), date(2021, 9, 1))],
            cache,
            WINDOW,
            sectors=sleeve.SectorLabels({}),
            coverage=thin,
        )
        assert result.low_coverage_warning is True
        assert result.coverage_ratio == pytest.approx(0.83)

    def test_unmeasured_coverage_is_null_not_false(self, tmp_path: Path) -> None:
        result = run_sleeve(tmp_path, [])
        assert result.coverage_ratio is None
        assert result.low_coverage_warning is None

    def test_calendar_years_are_measured_off_the_daily_curve(self, tmp_path: Path) -> None:
        trades = [make_trade("WIN", date(2021, 3, 1), date(2021, 9, 1))]
        result = run_sleeve(tmp_path, trades)
        assert list(result.calendar_year_returns) == ["2021", "2022"]
        assert result.calendar_year_returns["2021"] == pytest.approx(0.006, abs=1e-9)
        assert result.calendar_year_returns["2022"] == pytest.approx(0.0, abs=1e-9)


class TestDeterminism:
    """Acceptance 1, at the sleeve's own level."""

    def test_two_runs_over_one_cache_agree_exactly(self, tmp_path: Path) -> None:
        trades = [
            make_trade(f"C{index}", date(2021, 3, 1), date(2021, 9, 1), symbol=f"C{index}")
            for index in range(8)
        ]
        cache = flat_cache(tmp_path, [f"C{index}" for index in range(8)])
        first, second = (
            sleeve.simulate(trades, cache, WINDOW, sectors=sleeve.SectorLabels({}))
            for _ in range(2)
        )
        assert first.as_dict() == second.as_dict()
        assert first.curve_equity == second.curve_equity


class TestChainCloses:
    def test_the_last_usable_close_at_or_before_a_day(self, tmp_path: Path) -> None:
        cache = flat_cache(tmp_path, ["A"], price=42.0)
        closes = data.ChainCloses(cache)
        assert closes.close_on("A", date(2021, 3, 1)) == pytest.approx(42.0)
        # A weekend holds Friday's close rather than reporting nothing.
        assert closes.close_on("A", date(2021, 3, 6)) == pytest.approx(42.0)
        # Before the series starts there is nothing to carry.
        assert closes.close_on("A", date(2019, 1, 1)) is None
        assert closes.close_on("MISSING", date(2021, 3, 1)) is None

    def test_a_non_finite_close_is_dropped_rather_than_marked(self, tmp_path: Path) -> None:
        days = business_days(FIXTURE_START, FIXTURE_END)
        close = np.full(len(days), 50.0)
        hole = days.index(date(2021, 3, 2))
        close[hole] = math.nan
        cache = data.PriceCache(tmp_path / "cache")
        cache.store(
            "A",
            pd.DataFrame(
                {
                    "Open": close,
                    "High": close,
                    "Low": close,
                    "Close": close,
                    "Adj Close": close,
                    "Volume": np.full(len(days), 1.0),
                },
                index=pd.DatetimeIndex([pd.Timestamp(day) for day in days]),
            ),
        )
        closes = data.ChainCloses(cache)
        # The hole holds the previous session's mark instead of marking at NaN.
        assert closes.close_on("A", date(2021, 3, 2)) == pytest.approx(50.0)
