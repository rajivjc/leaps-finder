"""§4 signal reconstruction and the stock-track event loop (SPEC-BACKTEST.md §4).

`leaps_scanner.indicators` stays the reference implementation (§4.1): every
formula here comes from calling `sma`, `weekly_bars` and `slow_stochastic`, never
from restating SPEC.md §4 in a second place. What this module adds is *time* —
those functions read the latest bar, and a ten-year replay needs every bar — so
the latest-bar decisions (`completed_weekly_bars`' trim, `crosses_below`'
transition, `evaluate`'s history minimums) are re-derived as whole-series arrays.
§10.7 makes that a gate rather than a hope: `tests/test_backtest_engine.py`
proves the panel equals `indicators.evaluate` at every as-of date of a fixture.

Two structural choices are worth stating up front, because neither is obvious
from the formulas:

* **A position belongs to a rename chain, not to a ticker** (§4.3a). The engine
  reads the chain's terminal series in full and never truncates it at a span
  boundary, so a trade open across `FB -> META` simply keeps running. The
  span-scoped truncation `data.source_frame` applies is a *coverage* concept: it
  decides which membership row owns a week. It cannot decide what an open
  position does, and applying it here would manufacture a `delisted` exit out of
  a press release.
* **Membership gates entries only** (§4.3a). Leaving the index is not one of
  §4.4's exits, so a position runs on price data until a §4.4 rule fires.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from leaps_scanner import indicators
from leaps_scanner.backtest import data
from leaps_scanner.backtest.membership import Membership

logger = logging.getLogger(__name__)

# §4.4 / P3: DTE < 180 on a 365-day tenor first bites at 186 calendar days held.
# The stock track has no expiry, but it holds the same discipline so the two
# tracks trade the same signal rather than two subtly different ones.
TIME_EXIT_DAYS = 186

# §4.3: an entry whose next bar is more than five NYSE sessions away is skipped —
# a halt that long is information, not a fill.
MAX_FILL_SESSIONS = 5

# §4.2 headline zone and P13's reported-alongside Strict variant.
BASE_ZONE_HIGH = indicators.ZONE_HIGH
STRICT_ZONE_HIGH = 55.0
VARIANTS: Mapping[str, float] = {"base": BASE_ZONE_HIGH, "strict": STRICT_ZONE_HIGH}

# §4.4's priority, most severe first. `premium_stop` belongs to the B3 overlay
# and sits between `stoch_below_20` and `time_exit` when it arrives.
EXIT_PRIORITY = ("trend_break", "stoch_below_20", "time_exit")


@dataclass(frozen=True)
class Trade:
    """One completed round trip on the stock track (§4.5)."""

    chain: str  # terminal pricing ticker — the position's identity (§4.3a)
    symbol: str  # the membership symbol in force on the entry signal date
    signal_date: date
    entry_date: date
    entry_price: float
    exit_date: date
    exit_price: float
    exit_reason: str
    # None for a forced mark (`delisted`, `end_of_window`): nothing signalled,
    # the data ran out. Distinguishing them keeps §6.1's breakdown honest.
    exit_signal_date: date | None
    # The fill basis actually used, so a reader can see which trades were marked
    # at a close rather than filled at an open (§4.3).
    entry_basis: str = "open"
    exit_basis: str = "open"
    # slowK at the entry signal — B4's P10 tie-break sorts on |slowK − 35|.
    entry_slow_k: float = float("nan")

    @property
    def r_trade(self) -> float:
        """§4.5: exit_fill / entry_fill − 1, dividends excluded."""
        return self.exit_price / self.entry_price - 1.0

    @property
    def holding_days(self) -> int:
        return (self.exit_date - self.entry_date).days

    def as_dict(self) -> dict:
        return {
            "chain": self.chain,
            "symbol": self.symbol,
            "signal_date": self.signal_date.isoformat(),
            "entry_date": self.entry_date.isoformat(),
            "entry_price": self.entry_price,
            "entry_basis": self.entry_basis,
            "entry_slow_k": self.entry_slow_k,
            "exit_signal_date": (
                self.exit_signal_date.isoformat() if self.exit_signal_date else None
            ),
            "exit_date": self.exit_date.isoformat(),
            "exit_price": self.exit_price,
            "exit_basis": self.exit_basis,
            "exit_reason": self.exit_reason,
            "holding_days": self.holding_days,
            "r_trade": self.r_trade,
        }


@dataclass(frozen=True)
class SkippedEntry:
    """A signal that never became a trade — logged and counted, never dropped."""

    chain: str
    symbol: str
    signal_date: date
    reason: str  # "no_fill" | "halt_too_long"

    def as_dict(self) -> dict:
        return {
            "chain": self.chain,
            "symbol": self.symbol,
            "signal_date": self.signal_date.isoformat(),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class Book:
    """One rename chain: the series that prices it and the weeks it was a member.

    `member_fridays` maps each member week-ending to the membership symbol in
    force that week, which is how a trade opened under `FB` records `FB` while
    pricing off `META` (§4.3a).
    """

    chain: str
    member_fridays: Mapping[date, str]


def build_books(membership: Membership, window: data.Window) -> tuple[Book, ...]:
    """Group the point-in-time universe into rename chains (§4.3a).

    Keyed on the chain rather than the symbol so that the two companies that
    shared the ticker `IR` land in *different* books — one priced off `TT`, one
    off the recycled `IR` series — while `FB` and `META` land in the same one.
    """
    fridays = data.week_endings(window.start, window.end)
    chains: dict[str, dict[date, str]] = {}

    for friday in fridays:
        for symbol in membership.members_on(friday):
            span = membership.span_on(symbol, friday)
            if span is None:  # unreachable: members_on came from these spans
                continue
            chain = membership.price_source(span).symbol
            weeks = chains.setdefault(chain, {})
            if friday in weeks:
                # Two membership rows claiming one chain in one week would mean
                # the rename encoding (remove + add on one date) had overlapped.
                # Load-time validation forbids it; if it ever happens, say so and
                # resolve deterministically rather than depending on dict order.
                logger.warning(
                    "chain %s claimed by both %s and %s on %s; keeping the first alphabetically",
                    chain,
                    weeks[friday],
                    symbol,
                    friday,
                )
                weeks[friday] = min(weeks[friday], symbol)
            else:
                weeks[friday] = symbol

    return tuple(
        Book(chain=chain, member_fridays=dict(sorted(weeks.items())))
        for chain, weeks in sorted(chains.items())
    )


@dataclass(frozen=True)
class Panel:
    """Every §4 quantity for one chain, over its whole history.

    Daily arrays are indexed by session; weekly arrays by completed-week label.
    `latest_label` is the bridge: for each session it holds the index of the
    newest weekly bar that was *complete* at that session's close, which is the
    whole of `completed_weekly_bars`' trim rule expressed as a lookup.
    """

    chain: str
    session_dates: tuple[date, ...]
    open_: np.ndarray
    close: np.ndarray
    trend_pass: np.ndarray
    trend_broken: np.ndarray
    label_dates: tuple[date, ...]
    slow_k: np.ndarray
    stoch_d: np.ndarray
    label_session: np.ndarray  # per label: index of the last session at or before it
    latest_label: np.ndarray  # per session: index of the newest completed label, or −1
    cross_below_exit: np.ndarray  # per label: slowK crossed below 20 on this bar
    entry_ready: np.ndarray  # per label: enough history, all inputs finite
    in_zone_low: np.ndarray  # per label: slowK ≥ 20 (the zone's upper edge varies)
    turning_up: np.ndarray  # per label: slowK > D and slowK > slowK[−1]
    first_eligible: date | None


def build_panel(chain: str, frame: pd.DataFrame) -> Panel | None:
    """Vectorize §4's signals over a chain's whole daily history.

    Every rolling quantity is computed once over the full series. That is safe
    against look-ahead precisely because each one is backward-looking: an SMA at
    session *j*, a stochastic at bar *i*, and a cross at bar *i* all read only
    sessions and bars at or before themselves, so slicing the input at any date
    leaves the earlier values untouched. §9's property test asserts it rather
    than trusting the argument.
    """
    if frame is None or frame.empty:
        return None

    frame = frame.sort_index()
    sessions = pd.DatetimeIndex(frame.index)
    close_series = frame["Close"]
    close = close_series.to_numpy(dtype="float64")
    open_ = frame["Open"].to_numpy(dtype="float64")

    sma50 = indicators.sma(close_series, indicators.SMA_FAST).to_numpy(dtype="float64")
    sma200 = indicators.sma(close_series, indicators.SMA_SLOW).to_numpy(dtype="float64")

    # NaN comparisons are False in numpy, which is the behaviour wanted: before
    # the SMA200 is defined a symbol neither passes the trend nor breaks it.
    trend_pass = (close > sma50) & (close > sma200) & (sma50 > sma200)
    trend_broken = (close < sma200) | (sma50 < sma200)

    weekly = indicators.weekly_bars(frame)
    if weekly.empty:
        return None
    stoch = indicators.slow_stochastic(weekly)
    labels = pd.DatetimeIndex(weekly.index)
    slow_k = stoch["slow_k"].to_numpy(dtype="float64")
    stoch_d = stoch["d"].to_numpy(dtype="float64")

    # Which session each weekly label reads its daily indicators at: the last
    # session at or before the label. `evaluate` does the same with
    # `daily.loc[:as_of]`, because a Friday label may be a holiday.
    label_session = np.searchsorted(sessions.to_numpy(), labels.to_numpy(), side="right") - 1

    # `completed_weekly_bars(daily[:j], today=j+1)` keeps every bar when session
    # j is a Friday and drops the newest one otherwise — one condition, so one
    # array. `own_label` is the index of the bar covering session j's own week.
    weekday = sessions.weekday.to_numpy()
    week_label = sessions.to_numpy() + (((4 - weekday) % 7) * np.timedelta64(1, "D"))
    own_label = np.searchsorted(labels.to_numpy(), week_label, side="right") - 1
    latest_label = np.where(weekday == 4, own_label, own_label - 1)

    return Panel(
        chain=chain,
        session_dates=tuple(stamp.date() for stamp in sessions),
        open_=open_,
        close=close,
        trend_pass=trend_pass,
        trend_broken=trend_broken,
        label_dates=tuple(stamp.date() for stamp in labels),
        slow_k=slow_k,
        stoch_d=stoch_d,
        label_session=label_session,
        latest_label=latest_label,
        cross_below_exit=_crosses_below(slow_k, indicators.EXIT_LEVEL),
        entry_ready=_entry_ready(slow_k, stoch_d, sma50, sma200, close, label_session),
        in_zone_low=slow_k >= indicators.ZONE_LOW,
        turning_up=_turning_up(slow_k, stoch_d),
        first_eligible=data.first_eligible_date(frame),
    )


def _crosses_below(values: np.ndarray, level: float) -> np.ndarray:
    """`indicators.crosses_below` at every bar at once.

    The reference drops NaN *before* reading the last two values, so "previous"
    means the previous defined bar, not the previous row. A stochastic goes
    undefined whenever a 10-week window is perfectly flat, so the distinction is
    real, and reproducing it is the difference between parity and nearly-parity.
    """
    defined = np.isfinite(values)
    compact = values[defined]
    crossed = np.zeros(values.shape, dtype=bool)
    if compact.size < 2:
        return crossed

    # Position within the compacted series of the last defined bar at or before
    # each bar; −1 before the first defined one.
    position = np.cumsum(defined) - 1
    transition = np.zeros(compact.shape, dtype=bool)
    transition[1:] = (compact[:-1] >= level) & (compact[1:] < level)
    usable = position >= 1
    crossed[usable] = transition[position[usable]]
    return crossed


def _turning_up(slow_k: np.ndarray, stoch_d: np.ndarray) -> np.ndarray:
    """§4.2: slowK above D and above its own previous bar."""
    previous = np.empty_like(slow_k)
    previous[0] = np.nan
    previous[1:] = slow_k[:-1]
    return (slow_k > stoch_d) & (slow_k > previous)


def _entry_ready(
    slow_k: np.ndarray,
    stoch_d: np.ndarray,
    sma50: np.ndarray,
    sma200: np.ndarray,
    close: np.ndarray,
    label_session: np.ndarray,
) -> np.ndarray:
    """`evaluate`'s history minimums and NaN guard, per weekly bar.

    `evaluate` raises `InsufficientHistory` when fewer than `MIN_WEEKLY_BARS`
    completed weeks exist, when the daily history through that week is shorter
    than `MIN_DAILY_BARS`, or when any input is NaN. A replay cannot raise —
    those bars are simply not evaluable — so the same three conditions become a
    mask, and a bar that fails it produces no signal of any kind.
    """
    count = slow_k.shape[0]
    index = np.arange(count)
    ready = index >= indicators.MIN_WEEKLY_BARS - 1
    ready &= label_session + 1 >= indicators.MIN_DAILY_BARS

    previous = np.empty_like(slow_k)
    previous[0] = np.nan
    previous[1:] = slow_k[:-1]
    ready &= np.isfinite(slow_k) & np.isfinite(previous) & np.isfinite(stoch_d)

    # Guarded so the daily lookup cannot index with the −1 that a label older
    # than the first session would produce.
    session = np.where(label_session >= 0, label_session, 0)
    daily_finite = np.isfinite(close[session]) & np.isfinite(sma50[session])
    daily_finite &= np.isfinite(sma200[session])
    return ready & (label_session >= 0) & daily_finite


def session_calendar(
    cache: data.PriceCache, window: data.Window, chains: Sequence[str] = ()
) -> tuple[date, ...]:
    """The NYSE session calendar, for §4.3's fill window and §4.4's `delisted`.

    Taken from the benchmark series, which trades every NYSE session and is
    already cached for §6. When it is missing — §3.5 lets a run continue without
    it — the calendar falls back to the union of the members' own sessions,
    which is the same set for any window with a liquid name trading in it.
    """
    benchmark = cache.load_benchmark()
    if benchmark is not None and not benchmark.empty:
        sessions = pd.DatetimeIndex(benchmark.index)
    else:
        logger.warning(
            "%s is not cached; deriving the session calendar from member history",
            data.BENCHMARK_SYMBOL,
        )
        union: set[pd.Timestamp] = set()
        for chain in chains:
            frame = cache.load(chain)
            if frame is not None and not frame.empty:
                union.update(pd.DatetimeIndex(frame.index))
        sessions = pd.DatetimeIndex(sorted(union))

    return tuple(stamp.date() for stamp in sessions if window.start <= stamp.date() <= window.end)


@dataclass(frozen=True)
class ChainResult:
    """What one book produced, per §4.2 zone variant."""

    trades: Mapping[str, tuple[Trade, ...]]
    skipped: Mapping[str, tuple[SkippedEntry, ...]]


def run_book(
    book: Book,
    panel: Panel,
    window: data.Window,
    calendar: Sequence[date],
    *,
    variants: Mapping[str, float] = VARIANTS,
) -> ChainResult:
    """Replay §4 over one chain, once per zone variant.

    The variants share the panel deliberately: only `in_zone`'s upper edge
    differs between them (P13), so recomputing the indicators per variant would
    double the run for a comparison of one threshold.
    """
    trades: dict[str, tuple[Trade, ...]] = {}
    skipped: dict[str, tuple[SkippedEntry, ...]] = {}
    for name, zone_high in variants.items():
        book_trades, book_skipped = _replay(book, panel, window, calendar, zone_high)
        trades[name] = book_trades
        skipped[name] = book_skipped
    return ChainResult(trades=trades, skipped=skipped)


def _replay(
    book: Book,
    panel: Panel,
    window: data.Window,
    calendar: Sequence[date],
    zone_high: float,
) -> tuple[tuple[Trade, ...], tuple[SkippedEntry, ...]]:
    """One chronological walk: fills at the open, decisions at the close."""
    sessions = panel.session_dates
    last = _last_session_in_window(sessions, window)
    if last is None:
        return (), ()

    # §4.4's `delisted` vs `end_of_window`: did the market keep trading after
    # this series stopped? Measured on the calendar, never on a tolerance.
    final_reason = (
        "delisted"
        if _calendar_has_session_after(calendar, sessions[last], window.end)
        else "end_of_window"
    )
    signal_label = _entry_signals(book, panel, window, zone_high)

    trades: list[Trade] = []
    skipped: list[SkippedEntry] = []
    open_trade: dict | None = None
    pending_exit: tuple[date, str] | None = None
    pending_entry: tuple[int, date] | None = None  # (label index, signal date)

    for index in range(last + 1):
        today = sessions[index]

        # -- fills, at the open, from decisions taken at a previous close ------
        if pending_exit is not None and open_trade is not None:
            signalled, reason = pending_exit
            trades.append(
                _close_trade(
                    open_trade,
                    exit_date=today,
                    exit_price=float(panel.open_[index]),
                    exit_basis="open",
                    exit_reason=reason,
                    exit_signal_date=signalled,
                )
            )
            open_trade, pending_exit = None, None

        if pending_entry is not None and open_trade is None:
            label, signalled = pending_entry
            open_trade = {
                "chain": panel.chain,
                "symbol": book.member_fridays[panel.label_dates[label]],
                "signal_date": signalled,
                "entry_date": today,
                "entry_price": float(panel.open_[index]),
                "entry_slow_k": float(panel.slow_k[label]),
            }
            pending_entry = None

        # -- decisions, at the close ------------------------------------------
        if open_trade is not None:
            if index == last:
                trades.append(
                    _close_trade(
                        open_trade,
                        exit_date=today,
                        exit_price=float(panel.close[index]),
                        exit_basis="close",
                        exit_reason=final_reason,
                        exit_signal_date=None,
                    )
                )
                open_trade = None
                break
            reason = _exit_reason(panel, index, open_trade["entry_date"])
            if reason is not None:
                pending_exit = (today, reason)
            continue

        label = signal_label.get(index)
        if label is None:
            continue
        if index == last:
            # §4.3: the window ends before any bar could fill this signal, and a
            # same-close fill is forbidden. Skipped, logged, counted.
            skipped.append(
                SkippedEntry(
                    chain=panel.chain,
                    symbol=book.member_fridays[panel.label_dates[label]],
                    signal_date=today,
                    reason="no_fill",
                )
            )
            continue
        if _sessions_between(calendar, today, sessions[index + 1]) > MAX_FILL_SESSIONS:
            skipped.append(
                SkippedEntry(
                    chain=panel.chain,
                    symbol=book.member_fridays[panel.label_dates[label]],
                    signal_date=today,
                    reason="halt_too_long",
                )
            )
            continue
        pending_entry = (label, today)

    return tuple(trades), tuple(skipped)


def _entry_signals(
    book: Book, panel: Panel, window: data.Window, zone_high: float
) -> dict[int, int]:
    """Session index → weekly label, for every §4.2 entry signal in the window.

    A label is evaluated only when it was *itself* the newest completed bar at
    its own Friday close. A Friday-holiday week is therefore never an evaluation
    date — the reference scanner running that weekend would still be reporting
    the previous bar, and would have moved on to the next one by the following
    weekend. Being one week stale is v1's stated, safe behaviour, and the
    backtest inherits it rather than quietly improving on it.
    """
    signals: dict[int, int] = {}
    for label, label_date in enumerate(panel.label_dates):
        symbol = book.member_fridays.get(label_date)
        if symbol is None:
            continue  # not a member that week: no entry (§4.3a gates entries only)
        if panel.first_eligible is None or label_date < panel.first_eligible:
            continue  # §3.1's warm-up: not every input is defined yet
        if not panel.entry_ready[label]:
            continue

        session = int(panel.label_session[label])
        if panel.latest_label[session] != label:
            continue  # the week did not complete at its own Friday close
        if not panel.trend_pass[session]:
            continue
        if not (panel.in_zone_low[label] and panel.slow_k[label] <= zone_high):
            continue
        if not panel.turning_up[label]:
            continue
        signals[session] = label

    return signals


def _exit_reason(panel: Panel, index: int, entry_date: date) -> str | None:
    """§4.4's rules at one session's close, resolved by the pinned priority."""
    label = int(panel.latest_label[index])
    fired = {
        "trend_break": bool(panel.trend_broken[index]),
        "stoch_below_20": bool(label >= 0 and panel.cross_below_exit[label]),
        "time_exit": (panel.session_dates[index] - entry_date).days >= TIME_EXIT_DAYS,
    }
    for reason in EXIT_PRIORITY:
        if fired[reason]:
            return reason
    return None


def _close_trade(
    open_trade: dict,
    *,
    exit_date: date,
    exit_price: float,
    exit_basis: str,
    exit_reason: str,
    exit_signal_date: date | None,
) -> Trade:
    return Trade(
        chain=open_trade["chain"],
        symbol=open_trade["symbol"],
        signal_date=open_trade["signal_date"],
        entry_date=open_trade["entry_date"],
        entry_price=open_trade["entry_price"],
        entry_slow_k=open_trade["entry_slow_k"],
        exit_date=exit_date,
        exit_price=exit_price,
        exit_basis=exit_basis,
        exit_reason=exit_reason,
        exit_signal_date=exit_signal_date,
    )


def _last_session_in_window(sessions: Sequence[date], window: data.Window) -> int | None:
    for index in range(len(sessions) - 1, -1, -1):
        if sessions[index] <= window.end:
            return index
    return None


def _calendar_has_session_after(calendar: Sequence[date], after: date, through: date) -> bool:
    position = _bisect_right(calendar, after)
    return position < len(calendar) and calendar[position] <= through


def _sessions_between(calendar: Sequence[date], start: date, end: date) -> int:
    """NYSE sessions strictly after `start` and at or before `end` (§4.3)."""
    return _bisect_right(calendar, end) - _bisect_right(calendar, start)


def _bisect_right(calendar: Sequence[date], day: date) -> int:
    low, high = 0, len(calendar)
    while low < high:
        middle = (low + high) // 2
        if calendar[middle] <= day:
            low = middle + 1
        else:
            high = middle
    return low


@dataclass(frozen=True)
class EngineResult:
    """Every §4.2 variant's trades, in a deterministic order (§10.1)."""

    window: data.Window
    trades: Mapping[str, tuple[Trade, ...]]
    skipped: Mapping[str, tuple[SkippedEntry, ...]]
    chains: int
    priced_chains: int


def run(
    membership: Membership,
    cache: data.PriceCache,
    window: data.Window,
    *,
    variants: Mapping[str, float] = VARIANTS,
) -> EngineResult:
    """Replay §4 over the whole point-in-time universe.

    Chains are processed one at a time and their panels discarded, because
    holding ~700 decades of daily history plus their derived arrays in memory at
    once buys nothing: no chain's signals depend on another's.
    """
    books = build_books(membership, window)
    calendar = session_calendar(cache, window, [book.chain for book in books])

    trades: dict[str, list[Trade]] = {name: [] for name in variants}
    skipped: dict[str, list[SkippedEntry]] = {name: [] for name in variants}
    priced = 0

    for book in books:
        # The chain's series in full, never `data.source_frame`'s span-scoped
        # cut: §4.3a runs a position through a rename, and the history before a
        # span opens is what warms up the SMA200 the first signal needs.
        panel = build_panel(book.chain, cache.load(book.chain))
        if panel is None:
            continue
        priced += 1
        result = run_book(book, panel, window, calendar, variants=variants)
        for name in variants:
            trades[name].extend(result.trades[name])
            skipped[name].extend(result.skipped[name])

    return EngineResult(
        window=window,
        trades={
            name: tuple(sorted(rows, key=lambda t: (t.entry_date, t.chain)))
            for name, rows in trades.items()
        },
        skipped={
            name: tuple(sorted(rows, key=lambda s: (s.signal_date, s.chain)))
            for name, rows in skipped.items()
        },
        chains=len(books),
        priced_chains=priced,
    )
