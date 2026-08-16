"""B2 tests: SPEC-BACKTEST.md §9, acceptance criteria 1, 2, 4 and 7.

The load-bearing one is `TestCodePathParity`. §4.1 lets the engine vectorize the
§4 formulas *only* because §10.7 makes equality with `indicators.py` a gate, so
these tests replay `indicators.evaluate` / `daily_trend` / `crosses_below` at
every as-of date of a fixture and demand the panel agree exactly — not to a
tolerance, since both paths run the same arithmetic on the same input series.
"""

from __future__ import annotations

import json
import math
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from leaps_scanner import indicators
from leaps_scanner.backtest import __main__ as backtest_main
from leaps_scanner.backtest import data, engine, metrics, synthetic
from leaps_scanner.backtest.membership import Membership

GOLDEN_DIR = Path(__file__).parent / "golden"

# The fixture universe's calendar. Wide enough that §3.1's warm-up (SMA200,
# RV252, 14 weekly bars) is satisfied well before the evaluation window opens.
FIXTURE_START = date(2015, 1, 1)
FIXTURE_END = date(2020, 12, 31)
FIXTURE_WINDOW = data.Window(
    start=date(2017, 1, 2), end=date(2020, 12, 18), fetch_start=FIXTURE_START
)

# Good Fridays. They are the point of this list: a closed Friday leaves its week
# incomplete at the close §4.2 evaluates, so the fixture exercises the path where
# the reference scanner is one week stale and the engine has to be too.
FIXTURE_GOOD_FRIDAYS = frozenset(
    {
        date(2015, 4, 3),
        date(2016, 3, 25),
        date(2017, 4, 14),
        date(2018, 3, 30),
        date(2019, 4, 19),
        date(2020, 4, 10),
    }
)
# Fixed-date closures, so entry fills never land on New Year's Day.
FIXTURE_FIXED_HOLIDAYS = frozenset({(1, 1), (7, 4), (12, 25)})


def business_days(start: date, end: date) -> list[date]:
    """Mon-Fri minus the fixture's closures — a stand-in NYSE calendar."""
    days: list[date] = []
    day = start
    while day <= end:
        closed = (
            day.weekday() >= 5
            or day in FIXTURE_GOOD_FRIDAYS
            or (day.month, day.day) in FIXTURE_FIXED_HOLIDAYS
        )
        if not closed:
            days.append(day)
        day += timedelta(days=1)
    return days


def price_frame(
    days: list[date],
    *,
    base: float = 100.0,
    drift: float = 0.0007,
    slow_amplitude: float = 0.08,
    slow_period: float = 220.0,
    fast_amplitude: float = 0.07,
    fast_period: float = 55.0,
    phase: float = 0.0,
) -> pd.DataFrame:
    """A closed-form price path: exponential drift under two cycles.

    Deterministic on purpose (§8: "no randomness, no wall-clock dependence").
    Two harmonics rather than one, because a single cycle large enough to swing
    the weekly stochastic across its range also drags the close under its own
    SMA200 at every trough — so `trend_pass` and `in_zone` are never true
    together and the fixture produces no entries at all. The slow harmonic
    supplies the multi-year trend the §4.2 filter reads; the fast one supplies
    the pullbacks that put slowK back in the 20-70 zone while that trend holds,
    which is the setup the strategy exists to buy.
    """
    index = np.arange(len(days), dtype="float64")
    cycles = (
        1.0
        + slow_amplitude * np.sin(index / slow_period * 2 * math.pi + phase)
        + fast_amplitude * np.sin(index / fast_period * 2 * math.pi + phase)
    )
    close = base * np.exp(drift * index) * cycles
    open_ = np.empty_like(close)
    open_[0] = close[0]
    open_[1:] = close[:-1] * 1.0005
    high = np.maximum(open_, close) * 1.004
    low = np.minimum(open_, close) * 0.996

    return pd.DataFrame(
        {
            "Open": open_,
            "High": high,
            "Low": low,
            "Close": close,
            "Adj Close": close,
            "Volume": np.full(len(days), 1_000_000.0),
        },
        index=pd.DatetimeIndex([pd.Timestamp(day) for day in days]),
    )


# The rename date sits deliberately *inside* a trade the fixture holds, which is
# the only arrangement that exercises §4.3a rather than asserting about it.
FIXTURE_RENAME = date(2018, 4, 13)

FIXTURE_MEMBERSHIP = f"""\
# Fixture membership for the B2 engine tests. Not the shipped file.
symbol,added,removed,price_symbol
AAA,2010-01-04,,
BBB,2010-01-04,2019-03-15,
OLD,2010-01-04,{FIXTURE_RENAME.isoformat()},NEW
NEW,{FIXTURE_RENAME.isoformat()},,
"""


def build_fixture(root: Path) -> tuple[Membership, data.PriceCache]:
    """Materialize the fixture universe under `root`.

    A plain function rather than only a pytest fixture, so
    `tests/regenerate_golden.py` can build the identical universe outside a test
    run — a golden file the test harness alone can produce is not reproducible.
    """
    root.mkdir(parents=True, exist_ok=True)
    path = root / "membership.csv"
    path.write_text(FIXTURE_MEMBERSHIP, encoding="utf-8")
    cache = data.PriceCache(root / "cache")
    for symbol, frame in fixture_frames().items():
        cache.store(symbol, frame)
    return Membership.load(path), cache


@pytest.fixture
def fixture_membership(tmp_path: Path) -> Membership:
    path = tmp_path / "membership.csv"
    path.write_text(FIXTURE_MEMBERSHIP, encoding="utf-8")
    return Membership.load(path)


def rate_frame(days: list[date], *, base: float = 1.6, amplitude: float = 0.4) -> pd.DataFrame:
    """A stand-in ^IRX series. `Close` quotes the rate ×100, as Yahoo does."""
    index = np.arange(len(days), dtype="float64")
    close = base + amplitude * np.sin(index / 180.0 * 2 * math.pi)
    return pd.DataFrame(
        {
            "Open": close,
            "High": close,
            "Low": close,
            "Close": close,
            "Adj Close": close,
            "Volume": np.zeros(len(days)),
        },
        index=pd.DatetimeIndex([pd.Timestamp(day) for day in days]),
    )


def with_dividends(frame: pd.DataFrame, *, amount: float, every: int) -> pd.DataFrame:
    """Attach a regular cash dividend, in the `actions=True` column layout."""
    frame = frame.copy()
    payments = np.zeros(len(frame))
    payments[every::every] = amount
    frame[data.DIVIDEND_COLUMN] = payments
    return frame


def fixture_frames() -> dict[str, pd.DataFrame]:
    """The fixture universe's cached series, keyed by the ticker they live under.

    `OLD` has no file of its own: §2.3a's whole point is that Yahoo serves a
    renamed company only under its current ticker, so `NEW` carries the history
    for both spans and §4.3a runs a position straight through the boundary.

    `^IRX` and the dividend column are §5.1's r and q. They are here rather than
    in the overlay's own tests so the integration golden covers a universe with
    a payer (`AAA`), a non-payer (`BBB`) and a renamed chain (`NEW`) in it.
    """
    days = business_days(FIXTURE_START, FIXTURE_END)
    short = [day for day in days if day <= date(2019, 3, 13)]
    return {
        "AAA": with_dividends(price_frame(days, phase=0.0), amount=0.42, every=63),
        # Delists mid-window, three sessions before its membership row closes.
        "BBB": price_frame(short, base=54.0, phase=1.9, fast_period=48.0),
        "NEW": with_dividends(
            price_frame(days, base=210.0, phase=3.6, fast_period=62.0), amount=1.10, every=63
        ),
        data.BENCHMARK_SYMBOL: price_frame(
            days,
            base=180.0,
            drift=0.0004,
            slow_amplitude=0.05,
            slow_period=300.0,
            fast_amplitude=0.02,
        ),
        data.RATE_SYMBOL: rate_frame(days),
    }


@pytest.fixture
def fixture_cache(tmp_path: Path) -> data.PriceCache:
    cache = data.PriceCache(tmp_path / "cache")
    for symbol, frame in fixture_frames().items():
        cache.store(symbol, frame)
    return cache


def evaluation_fridays(frame: pd.DataFrame) -> list[date]:
    """Every Friday the fixture actually traded, past the warm-up."""
    sessions = [stamp.date() for stamp in pd.DatetimeIndex(frame.index)]
    return [day for day in sessions if day.weekday() == 4][60:]


class TestCodePathParity:
    """§10.7 / acceptance 7: the vectorized path *is* `indicators.py`."""

    def test_signals_match_evaluate_at_every_friday(self) -> None:
        frame = fixture_frames()["AAA"]
        panel = engine.build_panel("AAA", frame)
        assert panel is not None
        labels = {day: index for index, day in enumerate(panel.label_dates)}

        compared = 0
        for friday in evaluation_fridays(frame):
            through = frame.loc[: pd.Timestamp(friday)]
            # `today` is the day after the close being evaluated: the reference
            # drops a bar dated today because it is still being written, and a
            # replay is standing at that Friday's close, not during it.
            signals = indicators.evaluate(through, today=friday + timedelta(days=1))
            assert signals.as_of_date == friday  # the fixture trades every Friday it has

            index = labels[friday]
            assert bool(panel.entry_ready[index]) is True
            assert panel.slow_k[index] == signals.stoch_k
            assert panel.stoch_d[index] == signals.stoch_d
            assert bool(panel.turning_up[index]) is signals.turning_up
            session = int(panel.label_session[index])
            assert bool(panel.trend_pass[session]) is signals.trend_pass
            in_zone = bool(
                panel.in_zone_low[index] and panel.slow_k[index] <= engine.BASE_ZONE_HIGH
            )
            assert in_zone is signals.in_zone
            compared += 1

        assert compared > 150  # a real sweep, not an empty loop

    def test_daily_trend_matches_at_every_session(self) -> None:
        frame = fixture_frames()["AAA"]
        panel = engine.build_panel("AAA", frame)
        assert panel is not None

        for index in range(indicators.MIN_DAILY_BARS - 1, len(panel.session_dates), 7):
            through = frame.iloc[: index + 1]
            reference = indicators.daily_trend(through)
            assert bool(panel.trend_pass[index]) is reference.trend_pass
            assert bool(panel.trend_broken[index]) is reference.trend_broken

    def test_completed_bar_trim_matches_at_every_session(self) -> None:
        """`latest_label` reproduces `completed_weekly_bars`' drop rule."""
        frame = fixture_frames()["AAA"]
        panel = engine.build_panel("AAA", frame)
        assert panel is not None

        for index, session in enumerate(panel.session_dates):
            reference = indicators.completed_weekly_bars(
                frame.iloc[: index + 1], today=session + timedelta(days=1)
            )
            latest = int(panel.latest_label[index])
            if reference.empty:
                assert latest < 0
                continue
            assert panel.label_dates[latest] == reference.index[-1].date()

    def test_cross_below_matches_reference_at_every_bar(self) -> None:
        frame = fixture_frames()["NEW"]
        panel = engine.build_panel("NEW", frame)
        assert panel is not None
        weekly = indicators.weekly_bars(frame)
        slow_k = indicators.slow_stochastic(weekly)["slow_k"]

        crossings = 0
        for index in range(len(panel.label_dates)):
            reference = indicators.crosses_below(slow_k.iloc[: index + 1], indicators.EXIT_LEVEL)
            assert bool(panel.cross_below_exit[index]) is reference
            crossings += int(reference)

        assert crossings > 0  # the fixture really does cross the exit level


@pytest.mark.skipif(
    not (data.DEFAULT_CACHE_DIR / "prices").exists(),
    reason="no local backtest cache; §3.4 keeps it out of the repo and out of CI",
)
class TestRealCacheParity:
    """§10.7's secondary sanity check, in the form CI can never fake.

    The fixture parity tests prove the two code paths agree on a smooth
    closed-form series. Real quotes are not smooth — holidays, halts, flat
    ranges that leave the stochastic undefined — so when a cache is present the
    same equality is demanded over actual history. Skipped rather than failed
    without one: §3.4 keeps raw price data out of the repo, so CI has no cache
    and this can only ever be a local check.
    """

    @pytest.mark.parametrize("symbol", ["AAPL", "JPM", "XOM"])
    def test_panel_matches_evaluate_on_cached_history(self, symbol: str) -> None:
        cache = data.PriceCache(data.DEFAULT_CACHE_DIR)
        frame = cache.load(symbol)
        if frame is None or frame.empty:
            pytest.skip(f"{symbol} is not in the local cache")

        panel = engine.build_panel(symbol, frame)
        assert panel is not None

        compared = 0
        for index, label in list(enumerate(panel.label_dates))[-260:]:
            session = int(panel.label_session[index])
            if panel.session_dates[session] != label or not panel.entry_ready[index]:
                continue
            signals = indicators.evaluate(
                frame.loc[: pd.Timestamp(label)], today=label + timedelta(days=1)
            )
            if signals.as_of_date != label:
                continue
            assert panel.slow_k[index] == signals.stoch_k
            assert panel.stoch_d[index] == signals.stoch_d
            assert bool(panel.turning_up[index]) is signals.turning_up
            assert bool(panel.trend_pass[session]) is signals.trend_pass
            compared += 1

        assert compared > 200


class TestCrossingSemantics:
    """§9: crosses-below ≠ is-below, reusing SPEC.md §10's distinction."""

    def test_sitting_below_the_level_is_not_a_crossing(self) -> None:
        values = np.array([40.0, 25.0, 18.0, 12.0, 9.0, 11.0])
        crossed = engine._crosses_below(values, indicators.EXIT_LEVEL)
        assert list(crossed) == [False, False, True, False, False, False]

    def test_touching_the_level_from_above_needs_a_strict_break(self) -> None:
        values = np.array([30.0, 20.0, 20.0, 19.9])
        assert list(engine._crosses_below(values, indicators.EXIT_LEVEL)) == [
            False,
            False,
            False,
            True,
        ]

    def test_undefined_bars_are_skipped_not_treated_as_zero(self) -> None:
        """The reference drops NaN before reading the previous value."""
        values = np.array([30.0, np.nan, 15.0])
        crossed = engine._crosses_below(values, indicators.EXIT_LEVEL)
        assert list(crossed) == [False, False, True]

        series = pd.Series(values)
        assert indicators.crosses_below(series, indicators.EXIT_LEVEL) is True


class TestExitRules:
    """§4.4's table and its pinned priority."""

    def _panel(self) -> engine.Panel:
        panel = engine.build_panel("AAA", fixture_frames()["AAA"])
        assert panel is not None
        return panel

    def test_priority_prefers_trend_break(self) -> None:
        base = self._panel()
        panel = replace(
            base,
            trend_broken=np.ones_like(base.trend_broken),
            cross_below_exit=np.ones_like(base.cross_below_exit),
        )
        entry = panel.session_dates[300] - timedelta(days=400)
        assert engine._exit_reason(panel, 300, entry) == "trend_break"

    def test_priority_prefers_stochastic_over_time(self) -> None:
        base = self._panel()
        panel = replace(
            base,
            trend_broken=np.zeros_like(base.trend_broken),
            cross_below_exit=np.ones_like(base.cross_below_exit),
        )
        entry = panel.session_dates[300] - timedelta(days=400)
        assert engine._exit_reason(panel, 300, entry) == "stoch_below_20"

    def test_time_exit_is_strictly_186_calendar_days(self) -> None:
        base = self._panel()
        panel = replace(
            base,
            trend_broken=np.zeros_like(base.trend_broken),
            cross_below_exit=np.zeros_like(base.cross_below_exit),
        )
        today = panel.session_dates[300]
        assert engine._exit_reason(panel, 300, today - timedelta(days=185)) is None
        assert engine._exit_reason(panel, 300, today - timedelta(days=186)) == "time_exit"

    def test_delisted_forces_an_exit_at_the_last_close(
        self, fixture_membership: Membership, fixture_cache: data.PriceCache
    ) -> None:
        result = engine.run(fixture_membership, fixture_cache, FIXTURE_WINDOW)
        bbb = [trade for trade in result.trades["base"] if trade.chain == "BBB"]
        assert bbb, "the fixture should trade BBB before it delists"

        forced = [trade for trade in bbb if trade.exit_reason == "delisted"]
        assert len(forced) == 1
        assert forced[0].exit_date == date(2019, 3, 13)
        assert forced[0].exit_basis == "close"
        assert forced[0].exit_signal_date is None

    def test_open_trades_are_marked_end_of_window(
        self, fixture_membership: Membership, fixture_cache: data.PriceCache
    ) -> None:
        result = engine.run(fixture_membership, fixture_cache, FIXTURE_WINDOW)
        marked = [t for t in result.trades["base"] if t.exit_reason == "end_of_window"]
        for trade in marked:
            assert trade.exit_date == FIXTURE_WINDOW.end
            assert trade.exit_basis == "close"
            assert trade.exit_signal_date is None

    def test_every_exit_reason_is_one_the_spec_names(
        self, fixture_membership: Membership, fixture_cache: data.PriceCache
    ) -> None:
        allowed = {*engine.EXIT_PRIORITY, "delisted", "end_of_window"}
        result = engine.run(fixture_membership, fixture_cache, FIXTURE_WINDOW)
        for trades in result.trades.values():
            assert {trade.exit_reason for trade in trades} <= allowed


class TestExecution:
    """§4.3: fills at the next open, never at the signal's own close."""

    def test_entry_fills_at_the_next_session_open(
        self, fixture_membership: Membership, fixture_cache: data.PriceCache
    ) -> None:
        frames = fixture_frames()
        result = engine.run(fixture_membership, fixture_cache, FIXTURE_WINDOW)
        trades = result.trades["base"]
        assert trades

        for trade in trades:
            frame = frames[trade.chain]
            sessions = [stamp.date() for stamp in pd.DatetimeIndex(frame.index)]
            assert trade.entry_date > trade.signal_date
            position = sessions.index(trade.signal_date)
            assert sessions[position + 1] == trade.entry_date
            assert trade.entry_price == pytest.approx(frame["Open"].iloc[position + 1])
            assert trade.entry_basis == "open"

    def test_signal_exits_fill_at_the_next_open(
        self, fixture_membership: Membership, fixture_cache: data.PriceCache
    ) -> None:
        frames = fixture_frames()
        result = engine.run(fixture_membership, fixture_cache, FIXTURE_WINDOW)
        signalled = [t for t in result.trades["base"] if t.exit_signal_date is not None]
        assert signalled

        for trade in signalled:
            sessions = [stamp.date() for stamp in pd.DatetimeIndex(frames[trade.chain].index)]
            position = sessions.index(trade.exit_signal_date)
            assert sessions[position + 1] == trade.exit_date
            assert trade.exit_basis == "open"

    def test_a_long_halt_skips_the_entry(self) -> None:
        """§4.3: no bar within five NYSE sessions and the entry is dropped."""
        days = business_days(FIXTURE_START, FIXTURE_END)
        frame = price_frame(days)
        panel = engine.build_panel("AAA", frame)
        assert panel is not None

        calendar = [day for day in days if FIXTURE_WINDOW.start <= day <= FIXTURE_WINDOW.end]
        book = engine.Book(
            chain="AAA",
            member_fridays={
                friday: "AAA"
                for friday in data.week_endings(FIXTURE_WINDOW.start, FIXTURE_WINDOW.end)
            },
        )
        full = engine._replay(book, panel, FIXTURE_WINDOW, calendar, engine.BASE_ZONE_HIGH)
        assert full[0], "control: the unhalted fixture trades"

        # Excise a fortnight starting the session after the first entry fill, so
        # the signal that produced it now has no bar within five sessions.
        first = full[0][0]
        halted = frame.drop(
            index=[
                stamp
                for stamp in pd.DatetimeIndex(frame.index)
                if first.entry_date <= stamp.date() < first.entry_date + timedelta(days=14)
            ]
        )
        halted_panel = engine.build_panel("AAA", halted)
        assert halted_panel is not None
        trades, skipped = engine._replay(
            book, halted_panel, FIXTURE_WINDOW, calendar, engine.BASE_ZONE_HIGH
        )

        assert any(
            entry.signal_date == first.signal_date and entry.reason == "halt_too_long"
            for entry in skipped
        )
        assert all(trade.signal_date != first.signal_date for trade in trades)

    def test_a_signal_on_the_last_session_cannot_fill(self) -> None:
        """§4.3's window-edge rule: no bar to fill against, so no trade."""
        days = business_days(FIXTURE_START, FIXTURE_END)
        frame = price_frame(days)
        panel = engine.build_panel("AAA", frame)
        assert panel is not None

        signals = engine._entry_signals(
            engine.Book(
                chain="AAA",
                member_fridays={
                    friday: "AAA"
                    for friday in data.week_endings(FIXTURE_WINDOW.start, FIXTURE_WINDOW.end)
                },
            ),
            panel,
            FIXTURE_WINDOW,
            engine.BASE_ZONE_HIGH,
        )
        assert signals, "the fixture must produce at least one signal to truncate to"

        # Close the window exactly on a signal Friday: that signal has nowhere
        # to fill, and filling at its own close is what §4.3 forbids.
        signal_session = sorted(signals)[0]
        edge_date = panel.session_dates[signal_session]
        window = replace(FIXTURE_WINDOW, end=edge_date)
        book = engine.Book(chain="AAA", member_fridays={edge_date: "AAA"})
        calendar = [day for day in days if window.start <= day <= window.end]

        trades, skipped = engine._replay(book, panel, window, calendar, engine.BASE_ZONE_HIGH)
        assert trades == ()
        assert [(entry.signal_date, entry.reason) for entry in skipped] == [(edge_date, "no_fill")]


class TestPositionIdentity:
    """§4.3a: a rename is not an exit, and a recycled ticker is not one company."""

    def test_a_trade_runs_through_the_rename_boundary(
        self, fixture_membership: Membership, fixture_cache: data.PriceCache
    ) -> None:
        result = engine.run(fixture_membership, fixture_cache, FIXTURE_WINDOW)
        boundary = FIXTURE_RENAME
        chain = [trade for trade in result.trades["base"] if trade.chain == "NEW"]
        assert chain

        straddling = [t for t in chain if t.entry_date < boundary < t.exit_date]
        assert straddling, "the fixture must hold a position across the rename"
        for trade in straddling:
            assert trade.exit_reason != "delisted"
            assert trade.symbol == "OLD"  # the membership symbol at entry

    def test_books_group_by_chain_not_by_symbol(self, fixture_membership: Membership) -> None:
        built = engine.build_books(fixture_membership, FIXTURE_WINDOW)
        books = {book.chain: book for book in built}
        assert set(books) == {"AAA", "BBB", "NEW"}
        # `NEW`'s book carries both spans' weeks, labelled with the symbol that
        # was the member each week.
        symbols = set(books["NEW"].member_fridays.values())
        assert symbols == {"OLD", "NEW"}

    def test_recycled_tickers_resolve_to_different_chains(self, tmp_path: Path) -> None:
        """The shipped file's `IR`, the case that makes the chain the right key."""
        members = Membership.load()
        before = members.span_on("IR", date(2019, 1, 4))
        after = members.span_on("IR", date(2021, 1, 8))
        assert before is not None and after is not None
        assert members.price_source(before).symbol == "TT"
        assert members.price_source(after).symbol == "IR"

    def test_membership_removal_is_not_an_exit(
        self, fixture_membership: Membership, fixture_cache: data.PriceCache
    ) -> None:
        """§4.3a: leaving the index is not one of §4.4's rules."""
        result = engine.run(fixture_membership, fixture_cache, FIXTURE_WINDOW)
        removal = date(2019, 3, 15)
        bbb = [trade for trade in result.trades["base"] if trade.chain == "BBB"]
        # BBB's data stops before its row closes, so the last trade must be the
        # forced `delisted` mark — never an exit dated on the removal itself.
        assert all(trade.exit_date != removal for trade in bbb)
        assert all(trade.signal_date < removal for trade in bbb)


class TestZoneVariant:
    """P13: the Strict zone is reported alongside, never as the headline."""

    def test_strict_signals_are_a_subset_of_base_signals(
        self, fixture_membership: Membership, fixture_cache: data.PriceCache
    ) -> None:
        """The zone is the only difference, so every strict signal is a base one."""
        books = {b.chain: b for b in engine.build_books(fixture_membership, FIXTURE_WINDOW)}
        checked = 0
        for chain, book in books.items():
            panel = engine.build_panel(chain, fixture_cache.load(chain))
            assert panel is not None
            base = engine._entry_signals(book, panel, FIXTURE_WINDOW, engine.BASE_ZONE_HIGH)
            strict = engine._entry_signals(book, panel, FIXTURE_WINDOW, engine.STRICT_ZONE_HIGH)
            assert strict.items() <= base.items()
            checked += len(strict)
        assert checked > 0

    def test_strict_trades_all_entered_inside_the_strict_zone(
        self, fixture_membership: Membership, fixture_cache: data.PriceCache
    ) -> None:
        """Trades are *not* a subset: skipping an entry frees the chain earlier.

        A strict run that passes on a 60-slowK setup is flat when the next one
        arrives, so it can take a trade the base run was still holding through.
        Asserting a subset of trades would be asserting something P8 forbids.
        """
        result = engine.run(fixture_membership, fixture_cache, FIXTURE_WINDOW)
        strict = result.trades["strict"]
        assert strict
        for trade in strict:
            assert indicators.ZONE_LOW <= trade.entry_slow_k <= engine.STRICT_ZONE_HIGH

        base_signals = {(t.chain, t.signal_date) for t in result.trades["base"]}
        assert any((t.chain, t.signal_date) not in base_signals for t in strict)


class TestNoLookAhead:
    """§9's property test / acceptance 4."""

    @pytest.mark.parametrize("cutoff", [date(2018, 5, 18), date(2019, 7, 12)])
    def test_truncating_the_input_changes_no_earlier_decision(
        self, fixture_membership: Membership, tmp_path: Path, cutoff: date
    ) -> None:
        full_cache = data.PriceCache(tmp_path / "full")
        cut_cache = data.PriceCache(tmp_path / "cut")
        for symbol, frame in fixture_frames().items():
            full_cache.store(symbol, frame)
            cut_cache.store(symbol, frame.loc[: pd.Timestamp(cutoff)])

        full = engine.run(fixture_membership, full_cache, FIXTURE_WINDOW)
        cut = engine.run(fixture_membership, cut_cache, replace(FIXTURE_WINDOW, end=cutoff))

        entry_fields = (
            "chain",
            "symbol",
            "signal_date",
            "entry_date",
            "entry_price",
            "entry_basis",
            "entry_slow_k",
        )

        def decided(result: engine.EngineResult) -> dict[tuple, tuple]:
            """Every decision the run had actually taken by the cutoff."""
            rows: dict[tuple, tuple] = {}
            for trade in result.trades["base"]:
                if trade.signal_date > cutoff:
                    continue
                row = trade.as_dict()
                exit_ = None
                # A trade the truncated run had to mark `end_of_window` was not
                # *decided* — the data simply stopped. Its entry still has to
                # match, so only the exit half is set aside.
                if trade.exit_reason != "end_of_window" and trade.exit_date <= cutoff:
                    exit_ = (trade.exit_reason, trade.exit_date.isoformat(), trade.exit_price)
                key = (trade.chain, trade.signal_date.isoformat())
                rows[key] = (tuple(row[field] for field in entry_fields), exit_)
            return rows

        full_rows, cut_rows = decided(full), decided(cut)
        assert cut_rows, "the truncated run must still make decisions to compare"

        for key, (entry, exit_) in cut_rows.items():
            assert key in full_rows, f"{key} appeared only in the truncated run"
            reference_entry, reference_exit = full_rows[key]
            assert entry == reference_entry
            if exit_ is not None:
                assert exit_ == reference_exit

    def test_skipped_entries_also_survive_truncation(
        self, fixture_membership: Membership, tmp_path: Path
    ) -> None:
        cutoff = date(2019, 7, 12)
        full_cache = data.PriceCache(tmp_path / "full")
        cut_cache = data.PriceCache(tmp_path / "cut")
        for symbol, frame in fixture_frames().items():
            full_cache.store(symbol, frame)
            cut_cache.store(symbol, frame.loc[: pd.Timestamp(cutoff)])

        full = engine.run(fixture_membership, full_cache, FIXTURE_WINDOW)
        cut = engine.run(fixture_membership, cut_cache, replace(FIXTURE_WINDOW, end=cutoff))

        # The final signal of a truncated run has nothing to fill against, which
        # is a fact about the cutoff rather than a decision — everything before
        # it must match.
        def earlier(result: engine.EngineResult) -> set[tuple[str, str, str]]:
            return {
                (entry.chain, entry.signal_date.isoformat(), entry.reason)
                for entry in result.skipped["base"]
                if entry.signal_date < cutoff
            }

        assert earlier(cut) <= earlier(full)


class TestTrackA:
    """§6.1's per-trade statistics."""

    def test_market_delta_uses_the_trades_own_fill_basis(self) -> None:
        frame = fixture_frames()[data.BENCHMARK_SYMBOL]
        benchmark = metrics.BenchmarkPrices(frame)
        entry, exit_ = date(2018, 3, 1), date(2018, 9, 4)
        trade = engine.Trade(
            chain="AAA",
            symbol="AAA",
            signal_date=date(2018, 2, 28),
            entry_date=entry,
            entry_price=100.0,
            exit_date=exit_,
            exit_price=110.0,
            exit_reason="delisted",
            exit_signal_date=None,
            exit_basis="close",
        )
        expected = (
            frame["Close"].loc[pd.Timestamp(exit_)] / frame["Open"].loc[pd.Timestamp(entry)] - 1.0
        )
        assert benchmark.hold_return(trade) == pytest.approx(expected)

    def test_profit_factor_is_null_rather_than_infinite(self) -> None:
        assert metrics._profit_factor(np.array([0.1, 0.2])) is None
        assert metrics._profit_factor(np.array([0.2, -0.1])) == pytest.approx(2.0)

    def test_stats_cover_every_field_section_6_1_names(
        self, fixture_membership: Membership, fixture_cache: data.PriceCache
    ) -> None:
        result = engine.run(fixture_membership, fixture_cache, FIXTURE_WINDOW)
        benchmark = metrics.BenchmarkPrices(fixture_cache.load_benchmark())
        stats = metrics.track_a("base", result.trades["base"], result.skipped["base"], benchmark)

        assert stats.trades == len(result.trades["base"])
        assert stats.win_rate is not None
        assert set(stats.return_percentiles) == {"p5", "p25", "p50", "p75", "p95"}
        assert set(stats.holding_days) == {"mean", "median", "min", "max"}
        assert stats.exit_reasons
        assert stats.trades_by_year
        assert stats.market_delta_unpriced == 0
        # JSON-safe: no NaN or infinity can reach `results.json` (§8).
        assert "NaN" not in metrics.track_a_json([stats])
        assert "Infinity" not in metrics.track_a_json([stats])


class TestUnusableQuotes:
    """A quote that cannot be traded is a gap, not a fill price."""

    def _book(self) -> engine.Book:
        return engine.Book(
            chain="AAA",
            member_fridays={
                friday: "AAA"
                for friday in data.week_endings(FIXTURE_WINDOW.start, FIXTURE_WINDOW.end)
            },
        )

    def _calendar(self) -> list[date]:
        return [
            day
            for day in business_days(FIXTURE_START, FIXTURE_END)
            if FIXTURE_WINDOW.start <= day <= FIXTURE_WINDOW.end
        ]

    def test_tradeable_rejects_nan_and_non_positive(self) -> None:
        assert engine._tradeable(12.5) is True
        assert engine._tradeable(float("nan")) is False
        assert engine._tradeable(0.0) is False
        assert engine._tradeable(-3.0) is False

    def test_entry_fills_at_the_next_tradeable_open(self) -> None:
        """A blank open is skipped over, not filled against."""
        frame = fixture_frames()["AAA"]
        book, calendar = self._book(), self._calendar()
        control, _ = engine._replay(
            book,
            engine.build_panel("AAA", frame),
            FIXTURE_WINDOW,
            calendar,
            engine.BASE_ZONE_HIGH,
        )
        first = control[0]

        gapped = frame.copy()
        gapped.loc[pd.Timestamp(first.entry_date), "Open"] = float("nan")
        sessions = [stamp.date() for stamp in pd.DatetimeIndex(frame.index)]
        after = sessions[sessions.index(first.entry_date) + 1]

        trades, skipped = engine._replay(
            book,
            engine.build_panel("AAA", gapped),
            FIXTURE_WINDOW,
            calendar,
            engine.BASE_ZONE_HIGH,
        )
        moved = [t for t in trades if t.signal_date == first.signal_date]
        assert len(moved) == 1
        assert moved[0].entry_date == after
        assert moved[0].entry_price == pytest.approx(gapped["Open"].loc[pd.Timestamp(after)])
        assert not any(entry.signal_date == first.signal_date for entry in skipped)

    def test_a_signal_with_no_tradeable_bar_left_is_skipped(self) -> None:
        """Every remaining open blanked: no fill exists, so no trade is invented."""
        frame = fixture_frames()["AAA"]
        book, calendar = self._book(), self._calendar()
        control, _ = engine._replay(
            book,
            engine.build_panel("AAA", frame),
            FIXTURE_WINDOW,
            calendar,
            engine.BASE_ZONE_HIGH,
        )
        first = control[0]

        blanked = frame.copy()
        blanked.loc[pd.Timestamp(first.signal_date) :, "Open"] = float("nan")
        trades, skipped = engine._replay(
            book,
            engine.build_panel("AAA", blanked),
            FIXTURE_WINDOW,
            calendar,
            engine.BASE_ZONE_HIGH,
        )

        assert all(trade.signal_date != first.signal_date for trade in trades)
        assert any(
            entry.signal_date == first.signal_date and entry.reason == "no_fill"
            for entry in skipped
        )
        assert all(engine._tradeable(trade.entry_price) for trade in trades)

    def test_forced_mark_falls_back_to_the_last_usable_close(
        self, fixture_membership: Membership, fixture_cache: data.PriceCache
    ) -> None:
        """§4.4's mark needs a real close, not whichever row happens to be last."""
        result = engine.run(fixture_membership, fixture_cache, FIXTURE_WINDOW)
        marked = [t for t in result.trades["base"] if t.exit_reason == "end_of_window"]
        assert marked, "the fixture must end holding a position"
        held = marked[0]

        books = {b.chain: b for b in engine.build_books(fixture_membership, FIXTURE_WINDOW)}
        frame = fixture_frames()[held.chain]
        sessions = [stamp.date() for stamp in pd.DatetimeIndex(frame.index)]
        previous = sessions[sessions.index(held.exit_date) - 1]

        blanked = frame.copy()
        blanked.loc[pd.Timestamp(held.exit_date), "Close"] = float("nan")
        trades, _ = engine._replay(
            books[held.chain],
            engine.build_panel(held.chain, blanked),
            FIXTURE_WINDOW,
            self._calendar(),
            engine.BASE_ZONE_HIGH,
        )
        forced = [t for t in trades if t.exit_reason == "end_of_window"]
        assert len(forced) == 1
        assert forced[0].exit_date == previous
        assert forced[0].exit_price == pytest.approx(blanked["Close"].loc[pd.Timestamp(previous)])

    def test_no_trade_ever_carries_a_non_finite_price(
        self, fixture_membership: Membership, tmp_path: Path
    ) -> None:
        """The end-to-end guarantee: §8's JSON stays parseable."""
        cache = data.PriceCache(tmp_path / "holed")
        for symbol, frame in fixture_frames().items():
            holed = frame.copy()
            # Blank one open per fortnight. `Close` is left alone deliberately:
            # a NaN close poisons 200 sessions of SMA behind it, which suppresses
            # the signals instead of stressing the fills this test is about.
            holed.loc[pd.DatetimeIndex(holed.index)[::10], "Open"] = float("nan")
            cache.store(symbol, holed)

        result = engine.run(fixture_membership, cache, FIXTURE_WINDOW)
        benchmark = metrics.BenchmarkPrices(cache.load_benchmark())
        stats = [
            metrics.track_a(name, result.trades[name], result.skipped[name], benchmark)
            for name in result.trades
        ]

        for trades in result.trades.values():
            assert trades
            for trade in trades:
                assert engine._tradeable(trade.entry_price)
                assert engine._tradeable(trade.exit_price)
        rendered = metrics.track_a_json(stats)
        assert "NaN" not in rendered and "Infinity" not in rendered
        json.loads(rendered)  # strict parse: the §8 contract


class TestCoverageWarning:
    """Acceptance 2: the warning travels with the headline table."""

    def _coverage(self, ratio: float) -> data.Coverage:
        return data.Coverage(
            window=FIXTURE_WINDOW,
            run_date=FIXTURE_WINDOW.end,
            snapshot_date=None,
            total_member_weeks=1000,
            covered_member_weeks=int(1000 * ratio),
            members=4,
            no_data_members=0,
        )

    def _stats(self, coverage: data.Coverage | None) -> metrics.TrackAStats:
        trade = engine.Trade(
            chain="AAA",
            symbol="AAA",
            signal_date=date(2018, 3, 1),
            entry_date=date(2018, 3, 2),
            entry_price=100.0,
            exit_date=date(2018, 6, 1),
            exit_price=110.0,
            exit_reason="time_exit",
            exit_signal_date=date(2018, 5, 31),
        )
        return metrics.track_a(
            "base", [trade], [], metrics.BenchmarkPrices(None), coverage=coverage
        )

    def test_low_coverage_reaches_the_json_and_the_line(self) -> None:
        stats = self._stats(self._coverage(0.84))
        assert stats.low_coverage_warning is True
        assert stats.as_dict()["coverage_ratio"] == pytest.approx(0.84)
        assert "LOW COVERAGE 84.0%" in backtest_main._track_a_line(stats)

    def test_healthy_coverage_carries_no_warning(self) -> None:
        stats = self._stats(self._coverage(0.927))
        assert stats.low_coverage_warning is False
        assert "LOW COVERAGE" not in backtest_main._track_a_line(stats)

    def test_unmeasured_coverage_is_null_not_false(self) -> None:
        """`None` says "not measured"; `False` would claim coverage was fine."""
        stats = self._stats(None)
        assert stats.low_coverage_warning is None
        assert stats.as_dict()["coverage_ratio"] is None


class TestMissingBenchmark:
    """§3.5 lets a run continue without SPY — it must not invent the comparison."""

    def test_market_delta_reports_absence_rather_than_zero(
        self, fixture_membership: Membership, fixture_cache: data.PriceCache
    ) -> None:
        result = engine.run(fixture_membership, fixture_cache, FIXTURE_WINDOW)
        stats = metrics.track_a(
            "base", result.trades["base"], result.skipped["base"], metrics.BenchmarkPrices(None)
        )

        assert stats.market_delta["mean"] is None
        assert stats.market_delta_unpriced == stats.trades

        line = backtest_main._track_a_line(stats)
        assert "market delta n/a" in line
        assert "market delta +0.00%" not in line


class TestDeterminism:
    """Acceptance 1: two runs from one cache agree byte for byte."""

    def test_two_runs_produce_identical_json(
        self, fixture_membership: Membership, fixture_cache: data.PriceCache
    ) -> None:
        def once() -> str:
            result = engine.run(fixture_membership, fixture_cache, FIXTURE_WINDOW)
            benchmark = metrics.BenchmarkPrices(fixture_cache.load_benchmark())
            return metrics.track_a_json(
                [
                    metrics.track_a(name, result.trades[name], result.skipped[name], benchmark)
                    for name in engine.VARIANTS
                ]
            )

        assert once() == once()


def golden_payload(membership: Membership, cache: data.PriceCache) -> dict:
    """The committed integration fixture (§9): trades plus §6.1's tables.

    Both tracks, and every §5.6 configuration — the overlay's trade set diverges
    from the stock track's wherever a premium stop fires, so pinning only the
    stock track would leave that divergence unwitnessed.
    """
    overlays = synthetic.build_overlays(cache)
    result = engine.run(membership, cache, FIXTURE_WINDOW, overlays=overlays)
    benchmark = metrics.BenchmarkPrices(cache.load_benchmark())
    return {
        "window": {
            "start": FIXTURE_WINDOW.start.isoformat(),
            "end": FIXTURE_WINDOW.end.isoformat(),
            "fetch_start": FIXTURE_WINDOW.fetch_start.isoformat(),
        },
        "chains": result.chains,
        "priced_chains": result.priced_chains,
        "trades": {
            name: [trade.as_dict() for trade in rows] for name, rows in result.trades.items()
        },
        "skipped": {
            name: [entry.as_dict() for entry in rows] for name, rows in result.skipped.items()
        },
        "overlay_trades": {
            overlay.name: [trade.as_dict() for trade in overlay.trades] for overlay in overlays
        },
        "overlay_skipped": {
            overlay.name: [entry.as_dict() for entry in overlay.skipped] for overlay in overlays
        },
        "track_a": {
            name: metrics.track_a(
                name, result.trades[name], result.skipped[name], benchmark
            ).as_dict()
            for name in result.trades
        },
        "track_a_overlay": {
            overlay.name: metrics.track_a_overlay(
                overlay.config, overlay.trades, overlay.skipped, benchmark
            ).as_dict()
            for overlay in overlays
        },
    }


class TestGoldenIntegration:
    """§9: the engine over a recorded fixture universe, pinned to a golden file."""

    def test_matches_the_committed_golden(
        self, fixture_membership: Membership, fixture_cache: data.PriceCache
    ) -> None:
        golden = json.loads((GOLDEN_DIR / "backtest_trades.json").read_text(encoding="utf-8"))
        assert golden_payload(fixture_membership, fixture_cache) == golden
