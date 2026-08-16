"""§6.2's sleeve simulation: SPEC.md §7's discipline over the LEAP overlay.

**The sleeve is a filter, not a third replay.** §6.2 applies §7's discipline "to
Track A's LEAP-overlay signals" — so the overlay's finished trades are the
candidate set, the sleeve decides which of them to fund, and a funded position
runs to that trade's own exit. It consumes the overlay's signals; it does not
regenerate them. Two consequences worth stating rather than discovering:

* the sleeve's positions are a **strict subset** of the overlay track's trades,
  which is a property the tests assert rather than assume; and
* a declined entry does *not* free the chain for a later signal the way a real
  sleeve would. The overlay track already consumed that chain under P8, so the
  next signal on it is the one the overlay itself produced. Modelling the
  counterfactual would need sleeve state inside §4's event loop — a third pass
  per configuration, and a trade set no longer comparable row-for-row with
  §6.1's table. The filter reading is what §6.2's wording asks for; the
  divergence is noted here because it is a real simplification, not a free one.

**Daily marks without a second pricing pass.** §6.2 marks equity daily from
model mids and measures max drawdown on that curve. `LeapOverlay` does not
retain the mid path, but it does not have to: a `SyntheticTrade` carries every
§5.1 input the position was priced from, so `SyntheticPosition` is fully
reconstructible from it and `mid()` answers any session in the trade's life.
Closes come from `PriceCache.load(chain)` — the §4.3a rule, the chain's whole
series, never `data.source_frame`'s span-scoped cut.

**Sector labels do not cover the historical universe** (RC, 2026-08-16). §3.6
sources sectors from the v1 seed list, which holds the 503 *current* members;
208 of the window's 711 point-in-time members have none. RC's call: the
unlabelled names share one `Unknown` bucket, so the 2-per-sector cap binds
across them. That is deliberately the conservative direction — it can only
decline entries the discipline might have allowed, never allow ones it would
have refused — and the count travels into the report and bias-register row 7,
because a cap that binds on a bucket rather than on a sector is only honest if
the reader is told how many names are in it.

The circuit breaker's arithmetic is `risk.py`'s, called rather than mirrored:
`risk.Position` and `risk.Mark` are plain dataclasses, so the sleeve builds them
and hands them to `risk.sleeve_pnl`. §6.2 makes `risk.py` the reference for that
arithmetic; the one thing this module adds is §6.2's own hardening — live, the
breaker raises an advisory banner, and here it is an enforced entry ban, because
a simulation cannot model an override decision.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, timedelta

from leaps_scanner import risk, universe
from leaps_scanner.backtest import data, engine, metrics
from leaps_scanner.backtest.synthetic import SyntheticPosition, SyntheticTrade

logger = logging.getLogger(__name__)

# SPEC.md §7's sizing and caps. They live in TypeScript on the live side
# (`apps/web/lib/metrics.ts`), so there is no Python constant to import; they are
# written here once, against the spec, and nothing else in this module restates
# them. `SHARES_PER_CONTRACT` and both breaker constants *are* Python already and
# are imported from `risk` rather than copied.
POSITION_BUDGET_FRACTION = 0.03
MAX_OPEN_POSITIONS = 5
MAX_PER_SECTOR = 2
MAX_OPEN_PREMIUM_FRACTION = 0.15

# P9: the sleeve's starting equity, and the sensitivity that probes what integer
# contracts cost at small equity.
DEFAULT_START_EQUITY = 100_000.0
SENSITIVITY_START_EQUITY = 250_000.0

# P10's pinned preference point: the midpoint of SPEC.md §6's 25–45 Entry
# plateau. Arbitrary but fixed, and explicitly *not* a §6 derivation — inside the
# plateau §6 scores every value identically, so §6 cannot break the tie at all.
TIE_BREAK_SLOW_K = 35.0

# The bucket unlabelled names share (RC, 2026-08-16 — see the module docstring).
SECTOR_UNKNOWN = "Unknown"

# Why the sleeve declined an entry the overlay took. §6.2 names these five and
# requires them counted separately; the prefix keeps them distinct from §4.3's
# `no_fill` and §5's `overlay_*` skips, which are refusals of a different kind —
# those say the *signal* never became a trade, these say the sleeve had a trade
# available and chose not to fund it.
SKIP_BREAKER = "sleeve_breaker"
SKIP_SLOTS = "sleeve_slots"
SKIP_SECTOR = "sleeve_sector"
SKIP_GRANULARITY = "sleeve_granularity"
SKIP_EXPOSURE = "sleeve_exposure"

SKIP_REASONS = (SKIP_BREAKER, SKIP_SLOTS, SKIP_SECTOR, SKIP_GRANULARITY, SKIP_EXPOSURE)


class SectorLabels:
    """§3.6's labels, with the coverage gap counted rather than hidden.

    `unlabelled` is not diagnostics: RC's decision to bucket unlabelled names
    together is only honest if the report says how many there are, so the count
    travels with the labels and lands in `results.json`.
    """

    def __init__(self, labels: Mapping[str, str]) -> None:
        self._labels = dict(labels)
        self._unlabelled: set[str] = set()

    @classmethod
    def load(cls) -> SectorLabels:
        """The bundled seed list — the same source the live screener labels from."""
        return cls({entry.symbol: entry.sector for entry in universe.load_seed()})

    def sector_of(self, symbol: str) -> str:
        """The symbol's GICS sector, or the `Unknown` bucket.

        Keyed on the *membership* symbol, never on the rename chain: `IR` names
        two different companies in this universe (§4.3a), and the sector belongs
        to the company that was in the index, not to the series that prices it.
        """
        sector = self._labels.get(symbol)
        if sector:
            return sector
        self._unlabelled.add(symbol)
        return SECTOR_UNKNOWN

    @property
    def unlabelled(self) -> tuple[str, ...]:
        """Every symbol that fell to the bucket, in the order a report lists them."""
        return tuple(sorted(self._unlabelled))


@dataclass(frozen=True)
class SleeveConfig:
    """One P9 sleeve: a starting equity and the name the report gives it."""

    name: str
    start_equity: float = DEFAULT_START_EQUITY


CONFIGS: tuple[SleeveConfig, ...] = (
    SleeveConfig("E0=100k", DEFAULT_START_EQUITY),
    SleeveConfig("E0=250k", SENSITIVITY_START_EQUITY),
)


@dataclass(frozen=True)
class SleevePosition:
    """One funded position: an overlay trade, sized and stamped with its equity."""

    position_id: int
    trade: SyntheticTrade
    contracts: int
    sector: str
    # P9 / §6.2: the equity the position was sized against, frozen at entry. It is
    # also the circuit breaker's denominator via `risk.breaker_equity`, which is
    # why it is a snapshot rather than a lookup.
    equity_at_entry: float

    @property
    def chain(self) -> str:
        return self.trade.trade.chain

    @property
    def symbol(self) -> str:
        return self.trade.trade.symbol

    @property
    def entry_date(self) -> date:
        return self.trade.trade.entry_date

    @property
    def exit_date(self) -> date:
        return self.trade.trade.exit_date

    @property
    def cost_basis(self) -> float:
        """Premium paid in dollars: per-share cost × contracts × 100."""
        return self.trade.entry_cost * self.contracts * risk.SHARES_PER_CONTRACT

    @property
    def proceeds(self) -> float:
        """What the exit fill returned, §5.5's haircut already applied."""
        return self.trade.exit_value * self.contracts * risk.SHARES_PER_CONTRACT

    @property
    def profit(self) -> float:
        return self.proceeds - self.cost_basis

    def as_risk_position(self) -> risk.Position:
        """The same position as `risk.py` models it, so §7's arithmetic can run
        on it unmodified — §6.2 makes `risk.py` the reference, and calling it
        beats mirroring it."""
        return risk.Position(
            id=self.position_id,
            user_id="backtest",
            symbol=self.symbol,
            opened_on=self.entry_date,
            expiry=self.trade.expiry,
            strike=self.trade.strike,
            contracts=self.contracts,
            entry_premium=self.trade.entry_cost,
            account_equity_at_entry=self.equity_at_entry,
        )

    def as_closed_risk_position(self) -> risk.Position:
        return replace(
            self.as_risk_position(),
            status=risk.STATUS_CLOSED,
            closed_on=self.exit_date,
            exit_premium=self.trade.exit_value,
        )

    def as_dict(self) -> dict:
        return {
            "chain": self.chain,
            "symbol": self.symbol,
            "sector": self.sector,
            "entry_date": self.entry_date.isoformat(),
            "exit_date": self.exit_date.isoformat(),
            "exit_reason": self.trade.trade.exit_reason,
            "contracts": self.contracts,
            "entry_cost": self.trade.entry_cost,
            "exit_value": self.trade.exit_value,
            "cost_basis": self.cost_basis,
            "proceeds": self.proceeds,
            "profit": self.profit,
            "r_overlay": self.trade.r_overlay,
            "equity_at_entry": self.equity_at_entry,
        }


@dataclass(frozen=True)
class SkippedFunding:
    """An overlay trade the sleeve declined, and which rule declined it."""

    chain: str
    symbol: str
    sector: str
    entry_date: date
    reason: str

    def as_dict(self) -> dict:
        return {
            "chain": self.chain,
            "symbol": self.symbol,
            "sector": self.sector,
            "entry_date": self.entry_date.isoformat(),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class SleeveResult:
    """§6.2's reported table for one sleeve.

    Every ratio here is `None` rather than zero when it is undefined: a sleeve
    that funded nothing has no return, no win rate and no drawdown, and writing
    `0.0` for any of them would state a measured flat result that never happened.
    """

    name: str
    start_equity: float
    final_equity: float
    curve_dates: tuple[date, ...]
    curve_equity: tuple[float, ...]
    cagr: float | None
    max_drawdown: float | None
    max_drawdown_date: date | None
    calendar_year_returns: Mapping[str, float | None]
    positions: tuple[SleevePosition, ...]
    skipped: tuple[SkippedFunding, ...]
    wins: int
    win_rate: float | None
    average_open_positions: float | None
    average_premium_exposure: float | None
    breaker_dates: tuple[date, ...]
    unlabelled_symbols: tuple[str, ...] = field(default=())
    # Acceptance 2: below §2.4's 85% every headline table carries the warning, and
    # the sleeve's table is one. Carried on the result so the numbers cannot be
    # printed without it. `None` means coverage was not measured — not that it was
    # fine.
    coverage_ratio: float | None = None
    low_coverage_warning: bool | None = None

    @property
    def total_return(self) -> float | None:
        if self.start_equity <= 0:
            return None
        return self.final_equity / self.start_equity - 1.0

    @property
    def skipped_counts(self) -> dict[str, int]:
        """Every §6.2 cause, including the ones that never fired.

        Present-with-zero rather than absent: "the sector cap declined nothing"
        and "the sector cap was never evaluated" are different claims, and a
        missing key reads as the second.
        """
        counts = dict.fromkeys(SKIP_REASONS, 0)
        for entry in self.skipped:
            counts[entry.reason] = counts.get(entry.reason, 0) + 1
        return counts

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "start_equity": self.start_equity,
            "final_equity": self.final_equity,
            "total_return": self.total_return,
            "cagr": self.cagr,
            "max_drawdown": self.max_drawdown,
            "max_drawdown_date": (
                self.max_drawdown_date.isoformat() if self.max_drawdown_date else None
            ),
            "calendar_year_returns": dict(self.calendar_year_returns),
            "positions": len(self.positions),
            "chains": len({position.chain for position in self.positions}),
            "wins": self.wins,
            "win_rate": self.win_rate,
            "average_open_positions": self.average_open_positions,
            "average_premium_exposure": self.average_premium_exposure,
            "skipped_entries": self.skipped_counts,
            "breaker_activations": len(self.breaker_dates),
            "breaker_dates": [day.isoformat() for day in self.breaker_dates],
            "unlabelled_symbols": len(self.unlabelled_symbols),
            "coverage_ratio": self.coverage_ratio,
            "low_coverage_warning": self.low_coverage_warning,
        }


def _tie_break_key(trade: SyntheticTrade) -> tuple[float, str]:
    """P10: ascending `abs(slowK − 35)`, then alphabetical.

    Alphabetical on the *chain*, because the chain is the position's identity
    (§4.3a) and two different companies share the symbol `IR` — sorting on the
    symbol would not be a total order over this universe. A signal whose slowK
    was not recorded sorts last rather than unpredictably: NaN compares False
    against everything, which would make the order depend on the input sequence
    and quietly break §10.1's determinism.
    """
    slow_k = float(trade.trade.entry_slow_k)
    distance = abs(slow_k - TIE_BREAK_SLOW_K) if math.isfinite(slow_k) else math.inf
    return (distance, trade.trade.chain)


def _calendar_year_returns(
    dates: Sequence[date], equity: Sequence[float], start_equity: float
) -> dict[str, float | None]:
    """Year-over-year returns off the daily curve.

    The first and last years are partial — the window starts and ends mid-year —
    and are reported as they are rather than annualized, because annualizing a
    two-month stub would state a yearly figure no year produced.
    """
    if not dates:
        return {}
    year_end: dict[int, float] = {}
    for day, value in zip(dates, equity, strict=True):
        year_end[day.year] = value

    returns: dict[str, float | None] = {}
    previous = start_equity
    for year in sorted(year_end):
        closing = year_end[year]
        returns[str(year)] = (closing / previous - 1.0) if previous > 0 else None
        previous = closing
    return returns


def simulate(
    trades: Sequence[SyntheticTrade],
    cache: data.PriceCache,
    window: data.Window,
    *,
    config: SleeveConfig = CONFIGS[0],
    sectors: SectorLabels | None = None,
    closes: data.ChainCloses | None = None,
    calendar: Sequence[date] | None = None,
    coverage: data.Coverage | None = None,
) -> SleeveResult:
    """Run §6.2's sleeve over one overlay configuration's trades.

    One chronological walk of the session calendar. Each session, in order:
    exits fill at the open, then entries fill at the open, then equity is marked
    at the close, then the breaker is evaluated on that mark. The ordering is
    what keeps the simulation free of look-ahead — sizing at the open can only
    see the *previous* close's marks, which is exactly what a live sleeve would
    have had — and it is why the breaker bans the sessions *after* the one that
    tripped it rather than the one it was measured on.
    """
    sectors = sectors if sectors is not None else SectorLabels.load()
    closes = closes if closes is not None else data.ChainCloses(cache)
    if calendar is None:
        calendar = engine.session_calendar(
            cache, window, sorted({trade.trade.chain for trade in trades})
        )

    entries: dict[date, list[SyntheticTrade]] = {}
    for trade in trades:
        entries.setdefault(trade.trade.entry_date, []).append(trade)
    for same_day in entries.values():
        same_day.sort(key=_tie_break_key)

    cash = config.start_equity
    open_positions: list[SleevePosition] = []
    closed_positions: list[SleevePosition] = []
    skipped: list[SkippedFunding] = []
    breaker_dates: list[date] = []
    # The last mark taken for each open position, so sizing at the open reads the
    # previous close and never today's.
    last_mid: dict[int, float] = {}
    curve_dates: list[date] = []
    curve_equity: list[float] = []
    open_counts: list[int] = []
    exposures: list[float] = []
    banned_through: date | None = None
    next_id = 1

    # Entry dates in order, consumed by the walk rather than looked up by exact
    # date — see the entry block below for why the lookup was not enough.
    entry_dates = sorted(entries)
    next_entry = 0

    for today in calendar:
        # -- exits, at the open ------------------------------------------------
        # Scanned rather than looked up by date: a chain can in principle exit on
        # a session the benchmark calendar does not carry, and a position whose
        # exit day never came round would otherwise be held to the end of the run
        # and mark the sleeve against a trade that had already closed.
        due = sorted(
            (position for position in open_positions if position.exit_date <= today),
            key=lambda item: (item.exit_date, item.chain),
        )
        for position in due:
            open_positions.remove(position)
            closed_positions.append(position)
            last_mid.pop(position.position_id, None)
            cash += position.proceeds

        # -- entries, at the open ----------------------------------------------
        # Consumed by date order rather than matched exactly, for the same reason
        # the exits above are scanned: an entry filling on a session the calendar
        # does not carry would otherwise be dropped silently — neither funded nor
        # counted as declined — and the report's "declined N of M signals" would
        # quietly stop adding up. Dates are consumed in ascending order, and each
        # date's list is already in P10 order, so the tie-break still holds when
        # two dates land on one session.
        due_entries: list[SyntheticTrade] = []
        while next_entry < len(entry_dates) and entry_dates[next_entry] <= today:
            due_entries.extend(entries[entry_dates[next_entry]])
            next_entry += 1

        for trade in due_entries:
            symbol = trade.trade.symbol
            sector = sectors.sector_of(symbol)
            equity = cash + sum(
                position.contracts
                * risk.SHARES_PER_CONTRACT
                * last_mid.get(position.position_id, position.trade.entry_cost)
                for position in open_positions
            )
            open_cost = sum(position.cost_basis for position in open_positions)

            reason, contracts = decide_entry(
                trade=trade,
                sector=sector,
                today=today,
                equity=equity,
                cash=cash,
                open_cost=open_cost,
                open_positions=open_positions,
                banned_through=banned_through,
            )
            if reason is not None:
                skipped.append(
                    SkippedFunding(
                        chain=trade.trade.chain,
                        symbol=symbol,
                        sector=sector,
                        entry_date=today,
                        reason=reason,
                    )
                )
                continue

            position = SleevePosition(
                position_id=next_id,
                trade=trade,
                contracts=contracts,
                sector=sector,
                equity_at_entry=equity,
            )
            next_id += 1
            open_positions.append(position)
            cash -= position.cost_basis

        # -- the daily mark, at the close --------------------------------------
        marked = 0.0
        marks: dict[int, risk.Mark] = {}
        for position in open_positions:
            close = closes.close_on(position.chain, today)
            if close is None:
                # No usable close at or before today for a position the sleeve
                # holds. It contributes its cost rather than nothing — marking it
                # at zero would print a loss the data does not show — and it is
                # left out of `marks`, so `SleevePnl.complete` reports the gap and
                # the breaker knows its unrealized leg is an understatement.
                marked += position.cost_basis
                continue
            mid = _position_of(position).mid(close, today)
            last_mid[position.position_id] = mid
            marked += mid * position.contracts * risk.SHARES_PER_CONTRACT
            marks[position.position_id] = risk.Mark(
                position_id=position.position_id, mark_date=today, bid=mid, ask=mid, mid=mid
            )

        equity = cash + marked
        curve_dates.append(today)
        curve_equity.append(equity)
        open_counts.append(len(open_positions))
        exposures.append(
            (sum(position.cost_basis for position in open_positions) / equity)
            if equity > 0
            else 0.0
        )

        # -- the breaker, on that mark -----------------------------------------
        pnl = risk.sleeve_pnl(
            [position.as_risk_position() for position in open_positions],
            [position.as_closed_risk_position() for position in closed_positions],
            marks,
            today,
        )
        fraction = pnl.loss_fraction
        tripped = fraction is not None and fraction >= risk.CIRCUIT_BREAKER_LOSS_FRACTION
        latched = banned_through is not None and today <= banned_through
        if tripped and not latched:
            # **Latched, because the breaker's condition is a state, not an event.**
            # The trailing 28-day realized window keeps the same losses in view for
            # 28 days, so an unlatched check re-fires on every session in that span:
            # each one would push `banned_through` forward by another 28 days, and a
            # single loss event would ban entries for roughly 56 days rather than
            # §6.2's 28 — while `breaker_dates` counted sessions in the loss state
            # instead of breaker events. `risk.py`'s module docstring names this
            # hazard exactly ("a state re-fires every single day it holds") and adds
            # suppression for it; this is the same idea in the simulation.
            #
            # §6.2's "the window restarting on any later trip" is therefore read as
            # a *later* trip: one that happens once the ban has lapsed. While the
            # ban is in force there are no new entries to block, so re-arming it
            # against the same unchanged loss buys nothing and overstates both the
            # ban and the activation count.
            banned_through = today + timedelta(days=risk.CIRCUIT_BREAKER_DAYS)
            breaker_dates.append(today)

    if next_entry < len(entry_dates):
        # Candidates dated past the last session: the calendar ran out before the
        # signals did. Counted out loud rather than dropped in silence, because a
        # missing candidate is invisible in every ratio the report prints.
        stranded = sum(len(entries[day]) for day in entry_dates[next_entry:])
        logger.warning(
            "%s: %d candidate(s) after the last session %s were never evaluated",
            config.name,
            stranded,
            curve_dates[-1] if curve_dates else window.end,
        )

    drawdown, drawdown_date = metrics.max_drawdown(curve_dates, curve_equity)
    wins = sum(1 for position in closed_positions if position.profit > 0)
    settled = [*closed_positions, *open_positions]
    if open_positions:
        # §4.4 forces every overlay trade to an exit inside the window, so this is
        # unreachable on well-formed input. Said out loud rather than swallowed:
        # a position still open at the last session marks the final equity at a
        # model mid instead of a fill, and a silent one would make the sleeve's
        # closing figure quietly non-comparable with its own win rate.
        logger.warning(
            "%s: %d position(s) still open at %s; final equity is marked, not settled",
            config.name,
            len(open_positions),
            curve_dates[-1] if curve_dates else window.end,
        )

    return SleeveResult(
        name=config.name,
        start_equity=config.start_equity,
        final_equity=curve_equity[-1] if curve_equity else config.start_equity,
        curve_dates=tuple(curve_dates),
        curve_equity=tuple(curve_equity),
        cagr=metrics.cagr(
            config.start_equity,
            curve_equity[-1] if curve_equity else config.start_equity,
            window,
        ),
        max_drawdown=drawdown,
        max_drawdown_date=drawdown_date,
        calendar_year_returns=_calendar_year_returns(
            curve_dates, curve_equity, config.start_equity
        ),
        positions=tuple(sorted(settled, key=lambda item: (item.entry_date, item.chain))),
        skipped=tuple(sorted(skipped, key=lambda item: (item.entry_date, item.chain))),
        wins=wins,
        win_rate=(wins / len(closed_positions)) if closed_positions else None,
        average_open_positions=(sum(open_counts) / len(open_counts) if open_counts else None),
        average_premium_exposure=(sum(exposures) / len(exposures) if exposures else None),
        breaker_dates=tuple(breaker_dates),
        unlabelled_symbols=sectors.unlabelled,
        coverage_ratio=None if coverage is None else round(coverage.ratio, 6),
        low_coverage_warning=None if coverage is None else coverage.low_coverage_warning,
    )


def _position_of(position: SleevePosition) -> SyntheticPosition:
    """Rebuild §5's pricing inputs from the finished trade, for a daily mark.

    Every field is retained on the `SyntheticTrade` except the entry spot, which
    is the stock leg's entry fill — the same number the overlay priced from,
    since both legs fill at the same open (§4.3).
    """
    trade = position.trade
    return SyntheticPosition(
        entry_date=trade.trade.entry_date,
        expiry=trade.expiry,
        spot=trade.trade.entry_price,
        strike=trade.strike,
        sigma=trade.sigma,
        rate=trade.rate,
        dividend_yield=trade.dividend_yield,
        entry_cost=trade.entry_cost,
    )


def _contracts(equity: float, entry_cost: float) -> int:
    """P9: `floor(0.03 · equity / (entry_cost · 100))`, SPEC.md §7's sizing."""
    if equity <= 0 or entry_cost <= 0:
        return 0
    budget = POSITION_BUDGET_FRACTION * equity
    return int(budget // (entry_cost * risk.SHARES_PER_CONTRACT))


def decide_entry(
    *,
    trade: SyntheticTrade,
    sector: str,
    today: date,
    equity: float,
    cash: float,
    open_cost: float,
    open_positions: Sequence[SleevePosition],
    banned_through: date | None,
) -> tuple[str | None, int]:
    """§6.2's entry rules, in order, with the contract count they sized.

    Public because it *is* the spec's rule table: every reason the sleeve can
    decline a signal is decided here and nowhere else, which is what lets §9's
    cap tests pin each rule directly instead of contriving a ten-year price path
    that happens to trip it.

    Returns `(None, contracts)` when the entry is funded — the count comes back
    with the verdict so the caller cannot size the position a second time and
    have the two answers drift.

    The order is fixed so a declined entry has exactly one recorded cause and the
    counts add up: the breaker first (it bans entries outright, whatever the caps
    would have said), then the two caps that need no arithmetic, then sizing, then
    the exposure cap — which is last because it needs the contract count sizing
    produces.
    """
    if banned_through is not None and today <= banned_through:
        return SKIP_BREAKER, 0
    if len(open_positions) >= MAX_OPEN_POSITIONS:
        return SKIP_SLOTS, 0
    if sum(1 for position in open_positions if position.sector == sector) >= MAX_PER_SECTOR:
        return SKIP_SECTOR, 0

    contracts = _contracts(equity, trade.entry_cost)
    if contracts <= 0:
        return SKIP_GRANULARITY, 0

    cost = trade.entry_cost * contracts * risk.SHARES_PER_CONTRACT
    if open_cost + cost > MAX_OPEN_PREMIUM_FRACTION * equity:
        return SKIP_EXPOSURE, 0
    if cost > cash:
        # The same constraint in its binding form. §6.2's cap is written against
        # premium at cost, but a sleeve cannot spend cash it does not hold, and
        # the two only diverge when open positions have appreciated far past
        # their cost. Counted as an exposure refusal rather than a new cause,
        # and it can only decline entries — never allow one the cap would refuse.
        return SKIP_EXPOSURE, 0
    return None, contracts


def simulate_all(
    trades: Sequence[SyntheticTrade],
    cache: data.PriceCache,
    window: data.Window,
    *,
    configs: Iterable[SleeveConfig] = CONFIGS,
    closes: data.ChainCloses | None = None,
    coverage: data.Coverage | None = None,
) -> tuple[SleeveResult, ...]:
    """Both P9 sleeves over the same trades, sharing labels, closes and calendar.

    The shared state is the point: the calendar and the close arrays are the
    expensive parts, and neither depends on the starting equity. `closes` is
    accepted rather than only created so the caller can share one instance with
    §6.3.2's benchmark, which reads the same chains.
    """
    sectors = SectorLabels.load()
    closes = closes if closes is not None else data.ChainCloses(cache)
    calendar = engine.session_calendar(
        cache, window, sorted({trade.trade.chain for trade in trades})
    )
    return tuple(
        simulate(
            trades,
            cache,
            window,
            config=config,
            sectors=sectors,
            closes=closes,
            calendar=calendar,
            coverage=coverage,
        )
        for config in configs
    )
