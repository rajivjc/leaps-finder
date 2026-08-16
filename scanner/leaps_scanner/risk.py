"""Exit-signal evaluation for open positions (SPEC.md §7).

Pure functions over dataclasses — no network, no database — so every rule in
§7's table can be tested directly, including the crossing semantics §10 calls
out (slowK *crossing* below 20 is not the same as slowK *being* below 20; the
weekly rule delegates to `indicators.crosses_below` for exactly that reason).

Two things shape the design beyond the rules themselves.

**An alert must carry its evidence.** Every alert records the trading day it
was computed for and quotes the numbers that fired it, so its message is a
statement about a specific day's data rather than a floating assertion. A
message reading "mid 4.10, 41% of entry" is only checkable if the reader knows
which day's 4.10 that was.

**An alert must not repeat itself into uselessness.** Four of §7's six rules
describe a *state* (the trend is broken, the premium is halved, the clock is
short) rather than an event, and a state re-fires every single day it holds.
Suppression is therefore per-rule and matched to the rule's nature — see
`AlertLedger`. It is deliberately not a blanket "one per day": that would let a
genuinely new signal be swallowed by an unrelated one.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import pandas as pd

from leaps_scanner import indicators

logger = logging.getLogger(__name__)

# §7's alert kinds. `circuit_breaker` is M5's addition to §2's comment list: it
# is a fact about the sleeve, not about any one position.
TREND_BREAK = "trend_break"
STOCH_BELOW_20 = "stoch_below_20"
PREMIUM_STOP = "premium_stop"
TIME_EXIT = "time_exit"
EARNINGS_SOON = "earnings_soon"
CIRCUIT_BREAKER = "circuit_breaker"

# §7 thresholds, verbatim.
PREMIUM_STOP_FRACTION = 0.50
TIME_EXIT_DTE = 180
EARNINGS_WARN_DAYS = 21
CIRCUIT_BREAKER_LOSS_FRACTION = 0.08
CIRCUIT_BREAKER_WEEKS = 4
CIRCUIT_BREAKER_DAYS = CIRCUIT_BREAKER_WEEKS * 7

# One option contract covers 100 shares; premiums throughout are per share.
SHARES_PER_CONTRACT = 100

STATUS_OPEN = "open"
STATUS_CLOSED = "closed"

# §7's cadence column, as sets a caller can pass. The circuit breaker is daily
# too but is sleeve-level, so it is evaluated once per run rather than per
# position and does not appear here.
DAILY_RULES = frozenset({TREND_BREAK, PREMIUM_STOP, TIME_EXIT, EARNINGS_SOON})
WEEKLY_RULES = frozenset({STOCH_BELOW_20})

# Rules whose evaluation gap fails a run. These are the four that can miss an
# *exit*; §7 calls the earnings heads-up informational, and plenty of names
# simply have no earnings date in the data at all. Failing every refresh over a
# missing informational date would train the owner to ignore a red build, which
# costs more than the heads-up is worth.
BLOCKING_RULES = frozenset({TREND_BREAK, PREMIUM_STOP, TIME_EXIT, STOCH_BELOW_20})


@dataclass(frozen=True)
class Position:
    """One row of `positions`, as the scanner reads it."""

    id: int
    user_id: str
    symbol: str
    opened_on: date
    expiry: date
    strike: float
    contracts: int
    entry_premium: float
    account_equity_at_entry: float | None = None
    status: str = STATUS_OPEN
    closed_on: date | None = None
    exit_premium: float | None = None

    @property
    def cost_basis(self) -> float:
        """Premium paid, in dollars."""
        return self.entry_premium * self.contracts * SHARES_PER_CONTRACT

    def value_at(self, premium: float) -> float:
        return premium * self.contracts * SHARES_PER_CONTRACT

    def describe(self) -> str:
        """`AAPL 2027-01-15 180C` — how a holding is named in alert messages."""
        return f"{self.symbol} {self.expiry.isoformat()} {self.strike:g}C"

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Position:
        return cls(
            id=int(row["id"]),
            user_id=str(row["user_id"]),
            symbol=str(row["symbol"]),
            opened_on=_as_date(row["opened_on"]),
            expiry=_as_date(row["expiry"]),
            strike=float(row["strike"]),
            contracts=int(row["contracts"]),
            entry_premium=float(row["entry_premium"]),
            account_equity_at_entry=_optional_float(row.get("account_equity_at_entry")),
            status=str(row.get("status") or STATUS_OPEN),
            closed_on=_optional_date(row.get("closed_on")),
            exit_premium=_optional_float(row.get("exit_premium")),
        )


@dataclass(frozen=True)
class Mark:
    """One day's quote for a held contract (a `position_marks` row)."""

    position_id: int
    mark_date: date
    bid: float
    ask: float
    mid: float
    underlying_close: float | None = None


@dataclass(frozen=True)
class Alert:
    """An alert to be written. `position_id` is None for sleeve-level rules."""

    user_id: str
    kind: str
    message: str
    as_of_date: date
    position_id: int | None = None

    def to_row(self, scan_id: int) -> dict[str, Any]:
        """An `alerts` row. `scan_id` and `as_of_date` are the provenance that
        lets a reader check the message against the run that produced it."""
        return {
            "user_id": self.user_id,
            "position_id": self.position_id,
            "kind": self.kind,
            "message": self.message,
            "as_of_date": self.as_of_date.isoformat(),
            "scan_id": scan_id,
        }


@dataclass(frozen=True)
class ExistingAlert:
    """An already-written alert, read back so a rule can decline to repeat it."""

    kind: str
    created_on: date
    position_id: int | None = None
    as_of_date: date | None = None
    acknowledged: bool = False

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> ExistingAlert:
        return cls(
            kind=str(row.get("kind") or ""),
            created_on=_as_date(row["created_at"]),
            position_id=(int(row["position_id"]) if row.get("position_id") is not None else None),
            as_of_date=_optional_date(row.get("as_of_date")),
            acknowledged=bool(row.get("acknowledged")),
        )


# ---------------------------------------------------------------------------
# The rules themselves (§7's table, one function each).
# ---------------------------------------------------------------------------


def days_to_expiry(expiry: date, as_of: date) -> int:
    return (expiry - as_of).days


def is_trend_broken(trend: indicators.DailyTrend) -> bool:
    """§4/§7: close below the 200-day, or the 50-day below the 200-day."""
    return trend.trend_broken


def is_premium_stopped(entry_premium: float, mid: float) -> bool:
    """§7: current mid at or below half the entry premium.

    At-or-below, not below: the spec writes `≤`, and a mark landing exactly on
    the stop is a stop.
    """
    return mid <= PREMIUM_STOP_FRACTION * entry_premium


def is_time_exit(expiry: date, as_of: date) -> bool:
    """§7: fewer than 180 days to expiry."""
    return days_to_expiry(expiry, as_of) < TIME_EXIT_DTE


def is_earnings_soon(next_earnings: date | None, as_of: date) -> bool:
    """§7: earnings inside 21 days, informational.

    A date at or before `as_of` is a stale calendar entry rather than an
    imminent event — the same reading `run_scan.earnings_distance` takes.
    """
    if next_earnings is None or next_earnings <= as_of:
        return False
    return (next_earnings - as_of).days <= EARNINGS_WARN_DAYS


def is_stochastic_exit(slow_k: pd.Series) -> bool:
    """§4/§7: the weekly slow %K *crosses* below 20 on the completed bar.

    Delegated to `indicators.crosses_below` so the crossing semantics §10 pins
    have exactly one implementation: a %K that has sat at 15 for six weeks is
    not crossing anything, and must not re-fire an exit every week.
    """
    return indicators.crosses_below(slow_k, indicators.EXIT_LEVEL)


# ---------------------------------------------------------------------------
# Sleeve arithmetic and the circuit breaker.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SleevePnl:
    """§7's circuit-breaker inputs, itemised so the alert can quote them."""

    realized: float
    unrealized: float
    equity: float | None
    open_count: int
    marked_count: int
    realized_since: date

    @property
    def total(self) -> float:
        return self.realized + self.unrealized

    @property
    def loss_fraction(self) -> float | None:
        """Loss as a positive fraction of entry equity; None without a base."""
        if self.equity is None or self.equity <= 0:
            return None
        return -self.total / self.equity

    @property
    def complete(self) -> bool:
        """True when every open position contributed a mark.

        An unmarked position makes the unrealized leg an understatement, and an
        understated loss is the one direction a breaker must never fail in.
        """
        return self.marked_count == self.open_count


def breaker_equity(positions: Sequence[Position]) -> float | None:
    """The circuit breaker's denominator: the equity the sleeve was last sized
    against — `account_equity_at_entry` of the most recently opened position
    in the sleeve.

    §7 says "8% of entry equity" while every position carries its own snapshot,
    so the ambiguity has to be resolved somewhere. The newest entry is the
    right one for two reasons: it is the most current view of the account that
    was actually used to size a trade, and — being an immutable snapshot rather
    than the live figure in `user_settings` — it cannot be edited to defuse a
    breaker that has already tripped.

    "The sleeve" deliberately includes positions closed inside the breaker's
    window, not just open ones. A sleeve that was stopped out and fully closed
    has no open position to take a denominator from, and that is exactly the
    moment §7's four-week entry ban exists for — reading the equity only from
    open positions would leave the breaker silent in its most important case.

    Positions opened without an equity snapshot are skipped rather than
    treated as zero; None means no position in the sleeve carries one at all.
    """
    with_equity = [
        position
        for position in positions
        if position.account_equity_at_entry is not None and position.account_equity_at_entry > 0
    ]
    if not with_equity:
        return None

    newest = max(with_equity, key=lambda position: (position.opened_on, position.id))
    return newest.account_equity_at_entry


def sleeve_pnl(
    open_positions: Sequence[Position],
    closed_positions: Sequence[Position],
    marks: dict[int, Mark],
    as_of: date,
) -> SleevePnl:
    """Realized + unrealized sleeve P&L for §7's circuit breaker.

    Realized is taken over the trailing four weeks — the same window as the ban
    the breaker imposes. Counting realized losses over all time instead would
    mean a bad quarter two years ago could hold the sleeve shut forever, which
    is not a circuit breaker but a permanent one.

    Unrealized covers every open position that has a mark. Positions without
    one are counted in `open_count` but not `marked_count`, so the caller can
    see that the figure is incomplete instead of reading a partial sum as
    a full one.
    """
    since = as_of - timedelta(days=CIRCUIT_BREAKER_DAYS)

    recent_closed = [
        position
        for position in closed_positions
        if position.exit_premium is not None
        and position.closed_on is not None
        and position.closed_on >= since
    ]
    realized = sum(
        position.value_at(position.exit_premium) - position.cost_basis
        for position in recent_closed
        if position.exit_premium is not None
    )

    unrealized = 0.0
    marked = 0
    for position in open_positions:
        mark = marks.get(position.id)
        if mark is None:
            continue
        marked += 1
        unrealized += position.value_at(mark.mid) - position.cost_basis

    return SleevePnl(
        realized=float(realized),
        unrealized=unrealized,
        # Both halves of the sleeve, so a fully-closed one still has a base.
        equity=breaker_equity([*open_positions, *recent_closed]),
        open_count=len(open_positions),
        marked_count=marked,
        realized_since=since,
    )


def circuit_breaker_alert(pnl: SleevePnl, user_id: str, as_of: date) -> Alert | None:
    """§7: sleeve loss at or beyond 8% of entry equity trips the breaker."""
    fraction = pnl.loss_fraction
    if fraction is None or fraction < CIRCUIT_BREAKER_LOSS_FRACTION:
        return None

    lifts = as_of + timedelta(days=CIRCUIT_BREAKER_DAYS)
    coverage = (
        ""
        if pnl.complete
        else f" (unrealised covers {pnl.marked_count} of {pnl.open_count} open positions)"
    )
    message = (
        f"Sleeve down {_usd(-pnl.total)} as of {as_of.isoformat()}: "
        f"{_signed_usd(pnl.realized)} realised on positions closed since "
        f"{pnl.realized_since.isoformat()}, {_signed_usd(pnl.unrealized)} unrealised across "
        f"{pnl.open_count} open{coverage}. That is {fraction:.1%} of the "
        f"{_usd(pnl.equity or 0.0)} equity the sleeve was last sized against, at or past the "
        f"{CIRCUIT_BREAKER_LOSS_FRACTION:.0%} breaker. No new entries until {lifts.isoformat()}."
    )
    return Alert(user_id=user_id, kind=CIRCUIT_BREAKER, message=message, as_of_date=as_of)


# ---------------------------------------------------------------------------
# Suppression.
# ---------------------------------------------------------------------------


class AlertLedger:
    """Decides whether an alert would merely repeat one already on record.

    Per-rule, because the rules differ in kind:

    * **`circuit_breaker`** latches. §7 imposes a four-week ban, so once the
      breaker trips it stays tripped for four weeks and does not re-fire — the
      banner's lifetime *is* the suppression window.
    * **`earnings_soon`** is one alert per earnings event. Suppressed once this
      position has been flagged for the same upcoming report, so a 21-day
      run-up produces one heads-up rather than fifteen.
    * **`stoch_below_20`** is an event, and `crosses_below` already fires it at
      most once per crossing. Only same-week reruns are suppressed, so the
      weekly scan stays idempotent.
    * **`trend_break` / `premium_stop` / `time_exit`** are states that hold for
      as long as they hold. Suppressed while the owner has an unacknowledged
      alert of that kind on that position — and on same-day reruns regardless.
      Acknowledging one while still holding the position re-arms it: the
      condition is still true tomorrow, and a risk monitor that goes quiet
      because it was dismissed once is not doing its job.
    """

    def __init__(self, existing: Iterable[ExistingAlert]) -> None:
        self._existing = list(existing)

    def suppresses(self, alert: Alert, *, next_earnings: date | None = None) -> bool:
        same_target = [
            record
            for record in self._existing
            if record.kind == alert.kind and record.position_id == alert.position_id
        ]

        if alert.kind == CIRCUIT_BREAKER:
            floor = alert.as_of_date - timedelta(days=CIRCUIT_BREAKER_DAYS)
            return any(record.created_on > floor for record in same_target)

        # A rerun of the same trading day must not double-write.
        if any(record.as_of_date == alert.as_of_date for record in same_target):
            return True

        if alert.kind == EARNINGS_SOON:
            if next_earnings is None:
                return False
            window_opens = next_earnings - timedelta(days=EARNINGS_WARN_DAYS)
            return any(
                record.as_of_date is not None and record.as_of_date >= window_opens
                for record in same_target
            )

        if alert.kind == STOCH_BELOW_20:
            return False

        return any(not record.acknowledged for record in same_target)


# ---------------------------------------------------------------------------
# Per-position evaluation.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PositionInputs:
    """Everything the rules need for one position on one day.

    Any field may be None — the market data behind it may not have arrived.
    `evaluate_position` reports which rules that left unevaluated rather than
    quietly returning fewer alerts, because a rule that was never run and a
    rule that ran and found nothing are very different facts (CLAUDE.md: a
    partially-failed run must not look complete).
    """

    position: Position
    trend: indicators.DailyTrend | None = None
    mark: Mark | None = None
    next_earnings: date | None = None
    slow_k: pd.Series | None = None


@dataclass(frozen=True)
class PositionOutcome:
    alerts: tuple[Alert, ...]
    unevaluated: tuple[str, ...]


def evaluate_position(
    inputs: PositionInputs,
    as_of: date,
    *,
    rules: frozenset[str] = DAILY_RULES,
    ledger: AlertLedger | None = None,
) -> PositionOutcome:
    """Run the requested subset of §7's rules for one open position.

    `rules` is §7's cadence column made explicit. It matters for `unevaluated`
    as much as for the alerts: a rule that was not scheduled to run is not a
    gap in coverage, whereas a scheduled rule whose data never arrived is, and
    conflating the two would make every weekly scan look half-broken.
    """
    position = inputs.position
    alerts: list[Alert] = []
    unevaluated: list[str] = []

    def emit(kind: str, message: str) -> None:
        alerts.append(
            Alert(
                user_id=position.user_id,
                kind=kind,
                message=message,
                as_of_date=as_of,
                position_id=position.id,
            )
        )

    # Trend break (daily).
    if TREND_BREAK not in rules:
        pass
    elif inputs.trend is None:
        unevaluated.append(TREND_BREAK)
    elif is_trend_broken(inputs.trend):
        trend = inputs.trend
        legs = []
        if trend.close < trend.sma200:
            legs.append(f"close {trend.close:.2f} below the 200-day {trend.sma200:.2f}")
        if trend.sma50 < trend.sma200:
            legs.append(f"50-day {trend.sma50:.2f} below the 200-day {trend.sma200:.2f}")
        emit(
            TREND_BREAK,
            f"{position.symbol} trend break on {trend.as_of_date.isoformat()}: "
            f"{' and '.join(legs)}.",
        )

    # Premium stop (daily).
    if PREMIUM_STOP not in rules:
        pass
    elif inputs.mark is None:
        unevaluated.append(PREMIUM_STOP)
    elif is_premium_stopped(position.entry_premium, inputs.mark.mid):
        mark = inputs.mark
        share = mark.mid / position.entry_premium if position.entry_premium else 0.0
        emit(
            PREMIUM_STOP,
            f"{position.describe()} marked {mark.mid:.2f} mid "
            f"(bid {mark.bid:.2f} / ask {mark.ask:.2f}) on {mark.mark_date.isoformat()}, "
            f"{share:.0%} of the {position.entry_premium:.2f} entry premium — at or below the "
            f"{PREMIUM_STOP_FRACTION:.0%} stop. Unrealised "
            f"{_signed_usd(position.value_at(mark.mid) - position.cost_basis)}.",
        )

    # Time exit (daily). Needs no market data — the calendar is always known.
    if TIME_EXIT in rules and is_time_exit(position.expiry, as_of):
        emit(
            TIME_EXIT,
            f"{position.describe()} has {days_to_expiry(position.expiry, as_of)} days to expiry "
            f"as of {as_of.isoformat()}, under the {TIME_EXIT_DTE}-day floor.",
        )

    # Earnings heads-up (daily, informational).
    if EARNINGS_SOON not in rules:
        pass
    elif inputs.next_earnings is None:
        unevaluated.append(EARNINGS_SOON)
    elif is_earnings_soon(inputs.next_earnings, as_of):
        days = (inputs.next_earnings - as_of).days
        emit(
            EARNINGS_SOON,
            f"{position.symbol} reports on {inputs.next_earnings.isoformat()}, {days} days after "
            f"{as_of.isoformat()}. Informational — §7 does not make earnings an exit.",
        )

    # Stochastic (weekly).
    if STOCH_BELOW_20 in rules:
        if inputs.slow_k is None:
            unevaluated.append(STOCH_BELOW_20)
        elif is_stochastic_exit(inputs.slow_k):
            clean = inputs.slow_k.dropna()
            emit(
                STOCH_BELOW_20,
                f"{position.symbol} weekly slow %K crossed below {indicators.EXIT_LEVEL:.0f} on "
                f"the week ending {as_of.isoformat()}: {float(clean.iloc[-1]):.1f}, "
                f"from {float(clean.iloc[-2]):.1f}.",
            )

    if ledger is not None:
        alerts = [
            alert
            for alert in alerts
            if not ledger.suppresses(alert, next_earnings=inputs.next_earnings)
        ]

    return PositionOutcome(alerts=tuple(alerts), unevaluated=tuple(unevaluated))


# ---------------------------------------------------------------------------
# Formatting helpers used inside alert messages.
# ---------------------------------------------------------------------------


def _as_date(value: Any) -> date:
    """Parse a PostgREST date or timestamptz into a date.

    Slicing to the first ten characters covers both `2026-08-14` and
    `2026-08-14T22:31:05.123456+00:00`; every timestamp the scanner writes is
    UTC (`db.now_iso`), so the leading date is the UTC calendar day, which is what
    every suppression window here is measured in.
    """
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _optional_date(value: Any) -> date | None:
    return None if value is None else _as_date(value)


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _usd(amount: float) -> str:
    return f"${amount:,.0f}"


def _signed_usd(amount: float) -> str:
    return f"{'-' if amount < 0 else '+'}${abs(amount):,.0f}"
