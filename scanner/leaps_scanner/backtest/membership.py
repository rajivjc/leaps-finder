"""Point-in-time S&P 500 membership (SPEC-BACKTEST.md §2).

The whole point of this module is that the universe is read *as of a date*, not
as of today: backtesting the current index members over ten years would quietly
drop every company that failed out of it, which is the survivorship bias this
spec spends §2 avoiding.

The membership file itself is checked in beside this module and documents its own
source and retrieval date in a header comment.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import date
from importlib import resources
from pathlib import Path

# The CSV lives in a plain `data/` directory rather than a subpackage on
# purpose: a `data/__init__.py` here would turn it into a regular package, and a
# regular package shadows the sibling `data.py` module that SPEC-BACKTEST.md §8
# also pins. Hence the path-relative lookup below rather than
# `resources.files("leaps_scanner.backtest.data")`.
MEMBERSHIP_FILE = "data/sp500_membership.csv"


def normalize_symbol(symbol: str) -> str:
    """Index-vendor symbol form to yfinance form (SPEC-BACKTEST.md §2.3).

    Class shares are written with a dot by S&P and Wikipedia (`BRK.B`) and with
    a dash by Yahoo (`BRK-B`). Everything downstream — cache filenames, fetches,
    coverage rows — speaks the yfinance form, so the translation happens once,
    here, at the edge.
    """
    return symbol.strip().upper().replace(".", "-")


@dataclass(frozen=True)
class MembershipSpan:
    """One continuous stretch of index membership for one symbol.

    Half-open by §2.2: inclusive of `added`, exclusive of `removed`. A symbol
    added and removed on the same date is therefore a member on no date at all,
    which is the correct reading of a same-day replacement.
    """

    symbol: str
    added: date
    removed: date | None  # None = still a member at the retrieval date

    def covers(self, day: date) -> bool:
        """§2.2's membership rule, and the only place it is written down."""
        return self.added <= day and (self.removed is None or day < self.removed)

    def overlaps(self, start: date, end: date) -> bool:
        """True if this span shares any day with the inclusive range."""
        if self.added > end:
            return False
        return self.removed is None or self.removed > start


@dataclass(frozen=True)
class Membership:
    """Every membership span, indexed for point-in-time questions."""

    spans: tuple[MembershipSpan, ...]

    @classmethod
    def load(cls, path: Path | None = None) -> Membership:
        return cls(spans=tuple(load_spans(path)))

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(sorted({span.symbol for span in self.spans}))

    def spans_for(self, symbol: str) -> tuple[MembershipSpan, ...]:
        return tuple(span for span in self.spans if span.symbol == symbol)

    def members_on(self, day: date) -> tuple[str, ...]:
        """Symbols in the index on `day`, sorted — determinism starts here."""
        return tuple(sorted({span.symbol for span in self.spans if span.covers(day)}))

    def members_between(self, start: date, end: date) -> tuple[str, ...]:
        """Every symbol that is a member on at least one day of the range."""
        return tuple(sorted({span.symbol for span in self.spans if span.overlaps(start, end)}))

    def member_weeks(self, week_endings: Sequence[date]) -> dict[str, tuple[date, ...]]:
        """The weeks each symbol was a member, keyed by week-ending Friday.

        A week counts for a symbol when the symbol is a member on the week's
        Friday — the same instant §4.2 evaluates signals at, so a member-week is
        exactly a week the engine could have acted on. (A symbol added
        mid-week is therefore first counted at that week's close, not from the
        Monday.) This is the denominator of §2.4's coverage ratio.
        """
        weeks: dict[str, list[date]] = {}
        for friday in week_endings:
            for symbol in self.members_on(friday):
                weeks.setdefault(symbol, []).append(friday)
        return {symbol: tuple(fridays) for symbol, fridays in sorted(weeks.items())}


def membership_path() -> Path:
    source = resources.files("leaps_scanner.backtest").joinpath(MEMBERSHIP_FILE)
    with resources.as_file(source) as path:
        return Path(path)


def load_spans(path: Path | None = None) -> list[MembershipSpan]:
    """Read the membership CSV, validating what a hand-compiled file can get wrong.

    Comment lines carry the provenance §2.1 requires and are skipped here.
    Rows are validated rather than trusted: a span that ends before it starts, or
    two overlapping spans for one symbol, would silently double-count member-weeks
    and inflate the coverage denominator, so both are refused outright.
    """
    target = path if path is not None else membership_path()
    with target.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(line for line in handle if not line.startswith("#")))

    spans: list[MembershipSpan] = []
    for number, row in enumerate(rows, start=2):
        symbol = normalize_symbol(row.get("symbol") or "")
        if not symbol:
            raise ValueError(f"{target.name} row {number}: missing symbol")

        added = _parse_date(row.get("added"), target.name, number, "added")
        if added is None:
            raise ValueError(f"{target.name} row {number}: {symbol} has no added date")
        removed = _parse_date(row.get("removed"), target.name, number, "removed")

        if removed is not None and removed < added:
            raise ValueError(f"{target.name} row {number}: {symbol} removed {removed} < added")
        spans.append(MembershipSpan(symbol=symbol, added=added, removed=removed))

    _reject_overlaps(spans, target.name)
    spans.sort(key=lambda span: (span.symbol, span.added))
    return spans


def _parse_date(value: str | None, filename: str, number: int, column: str) -> date | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{filename} row {number}: bad {column} date {text!r}") from exc


def _reject_overlaps(spans: Iterable[MembershipSpan], filename: str) -> None:
    by_symbol: dict[str, list[MembershipSpan]] = {}
    for span in spans:
        by_symbol.setdefault(span.symbol, []).append(span)

    for symbol, symbol_spans in by_symbol.items():
        ordered = sorted(symbol_spans, key=lambda span: span.added)
        for earlier, later in _pairs(ordered):
            if earlier.removed is None or earlier.removed > later.added:
                raise ValueError(f"{filename}: overlapping spans for {symbol}")


def _pairs(items: Sequence[MembershipSpan]) -> Iterator[tuple[MembershipSpan, MembershipSpan]]:
    return zip(items, items[1:], strict=False)
