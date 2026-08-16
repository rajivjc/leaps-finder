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
from dataclasses import dataclass, field
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
    # §2.3a: the ticker this span's prices live under, when the company
    # re-tickered. Holds the *immediate* successor; `Membership.price_source`
    # walks any chain. None means the span prices itself.
    price_symbol: str | None = None

    def covers(self, day: date) -> bool:
        """§2.2's membership rule, and the only place it is written down."""
        return self.added <= day and (self.removed is None or day < self.removed)

    def overlaps(self, start: date, end: date) -> bool:
        """True if this span shares any day with the inclusive range."""
        if self.added > end:
            return False
        return self.removed is None or self.removed > start


@dataclass(frozen=True)
class PriceSource:
    """Which cached series prices one membership span, and how much of it (§2.3a).

    `until` is the span's own removal date, not the successor's. Reading META for
    Facebook's span must stop where Facebook's membership row stops: the sessions
    after it belong to META's own row, and counting them twice would inflate
    coverage for a week no single membership row owns.
    """

    symbol: str
    until: date | None  # exclusive; None = read the whole series


@dataclass(frozen=True)
class Membership:
    """Every membership span, indexed for point-in-time questions."""

    spans: tuple[MembershipSpan, ...]
    # Derived in `__post_init__`; excluded from equality and repr so two
    # Membership values still compare on the spans that define them.
    _by_symbol: dict[str, tuple[MembershipSpan, ...]] = field(
        init=False, repr=False, compare=False, default_factory=dict
    )

    def __post_init__(self) -> None:
        """Index the spans by symbol once, at construction.

        `span_on` is the hot call of the whole backtest: §2.4's coverage asks it
        once per member-week (262k times) and B2's daily exit loop several times
        that. Scanning all ~900 spans per call made it 95% of the coverage
        runtime, for a lookup that is a dict hit. The dataclass is frozen — this
        is the sanctioned way to derive a field on one.
        """
        grouped: dict[str, list[MembershipSpan]] = {}
        for span in self.spans:
            grouped.setdefault(span.symbol, []).append(span)
        object.__setattr__(
            self,
            "_by_symbol",
            {
                symbol: tuple(sorted(spans, key=lambda span: span.added))
                for symbol, spans in grouped.items()
            },
        )

    def __post_init__(self) -> None:
        """Group the spans by symbol once, at construction.

        Every per-symbol question below (`spans_for`, `span_on`,
        `_span_added_on`) is asked once per member-week — a quarter of a million
        times for the ten-year window, and several times that once §4's event
        loop steps daily. Answering each by walking all ~900 spans is what makes
        `compute_coverage` almost entirely a linear scan; answering it from this
        index walks the one-to-three spans the symbol actually has.

        Insertion order is preserved per symbol, so every lookup returns exactly
        what the linear scan returned — §10.1's byte-identical reruns depend on
        that. Not a field: it is derived from `spans`, and adding it to the
        dataclass would put it in `__eq__` and the repr. Hence the frozen-safe
        `object.__setattr__`.
        """
        by_symbol: dict[str, list[MembershipSpan]] = {}
        for span in self.spans:
            by_symbol.setdefault(span.symbol, []).append(span)
        index = {symbol: tuple(symbol_spans) for symbol, symbol_spans in by_symbol.items()}
        object.__setattr__(self, "_by_symbol", index)

    @classmethod
    def load(cls, path: Path | None = None) -> Membership:
        return cls(spans=tuple(load_spans(path)))

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_symbol))

    def spans_for(self, symbol: str) -> tuple[MembershipSpan, ...]:
        return self._by_symbol.get(symbol, ())

    def members_on(self, day: date) -> tuple[str, ...]:
        """Symbols in the index on `day`, sorted — determinism starts here."""
        return tuple(sorted({span.symbol for span in self.spans if span.covers(day)}))

    def members_between(self, start: date, end: date) -> tuple[str, ...]:
        """Every symbol that is a member on at least one day of the range."""
        return tuple(sorted({span.symbol for span in self.spans if span.overlaps(start, end)}))

    def span_on(self, symbol: str, day: date) -> MembershipSpan | None:
        """The span that makes `symbol` a member on `day`, if any.

        Spans for one symbol never overlap (enforced at load), so this is
        unambiguous — which is what lets a recycled ticker resolve to the right
        company rather than to whichever span happened to be found first.
        """
        for span in self._by_symbol.get(symbol, ()):
            if span.covers(day):
                return span
        return None

    def price_source(self, span: MembershipSpan) -> PriceSource:
        """Which cached series prices this span, following any rename chain (§2.3a).

        The chain hop is `(symbol, removed) -> the successor's span added that
        same day`, repeated while the span reached carries its own alias. Only
        `ANTM` chains today (`WLP -> ANTM -> ELV`), but resolving transitively is
        what lets the CSV record the immediate successor — the fact that was
        actually sourced — instead of a terminal ticker nobody announced.

        The cutoff stays the *first* span's removal date throughout: the chain
        moves which file is read, never how much of the calendar it covers.
        """
        if span.price_symbol is None:
            return PriceSource(symbol=span.symbol, until=None)

        target, seen = span.price_symbol, {span.symbol}
        hop = span
        while True:
            if target in seen:  # cycle; validation rejects these at load
                break
            seen.add(target)
            successor = self._span_added_on(target, hop.removed)
            if successor is None or successor.price_symbol is None:
                break
            hop, target = successor, successor.price_symbol

        return PriceSource(symbol=target, until=span.removed)

    def price_symbols_between(self, start: date, end: date) -> tuple[str, ...]:
        """Rename successors that must be fetched for spans overlapping the range.

        Every one of them is also a member today, so this is currently a no-op
        against `members_between`. It is not redundant: the pairing is a
        convention of the compiled file, not a guarantee, and a successor that
        stops being a member would otherwise go silently unfetched.
        """
        wanted = {
            self.price_source(span).symbol
            for span in self.spans
            if span.price_symbol is not None and span.overlaps(start, end)
        }
        return tuple(sorted(wanted))

    def _span_added_on(self, symbol: str, day: date | None) -> MembershipSpan | None:
        if day is None:
            return None
        for span in self._by_symbol.get(symbol, ()):
            if span.added == day:
                return span
        return None

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


def membership_text() -> str:
    """The bundled CSV's contents.

    Reads through the resource itself rather than handing back a filesystem
    path: `resources.as_file` only materializes a real file for the duration of
    its context, so a path returned out of one points at a deleted temp file
    under any loader that does not serve the package straight off disk.
    """
    source = resources.files("leaps_scanner.backtest").joinpath(MEMBERSHIP_FILE)
    return source.read_text(encoding="utf-8")


def load_spans(path: Path | None = None) -> list[MembershipSpan]:
    """Read the membership CSV, validating what a hand-compiled file can get wrong.

    Comment lines carry the provenance §2.1 requires and are skipped here, but
    their line numbers are kept so an error points at the line the file actually
    has — the shipped file opens with nearly forty lines of header, and a row
    number counted after stripping them sends the reader to the wrong place.

    Rows are validated rather than trusted: a span that ends before it starts, or
    two overlapping spans for one symbol, would silently double-count member-weeks
    and inflate the coverage denominator, so both are refused outright.
    """
    name = path.name if path is not None else Path(MEMBERSHIP_FILE).name
    text = path.read_text(encoding="utf-8") if path is not None else membership_text()

    numbered = [
        (number, line)
        for number, line in enumerate(text.splitlines(), start=1)
        if not line.startswith("#")
    ]
    rows = list(csv.DictReader(line for _, line in numbered))
    # The header consumes the first surviving line; the rest map one-to-one onto
    # data rows (no quoted newlines in this file).
    line_numbers = [number for number, _ in numbered[1:]]

    spans: list[MembershipSpan] = []
    for row, number in zip(rows, line_numbers, strict=False):
        symbol = normalize_symbol(row.get("symbol") or "")
        if not symbol:
            raise ValueError(f"{name} line {number}: missing symbol")

        added = _parse_date(row.get("added"), name, number, "added")
        if added is None:
            raise ValueError(f"{name} line {number}: {symbol} has no added date")
        removed = _parse_date(row.get("removed"), name, number, "removed")

        if removed is not None and removed < added:
            raise ValueError(f"{name} line {number}: {symbol} removed {removed} < added")

        price_symbol = normalize_symbol(row.get("price_symbol") or "") or None
        if price_symbol is not None:
            if price_symbol == symbol:
                raise ValueError(f"{name} line {number}: {symbol} aliases itself")
            if removed is None:
                # A ticker still trading prices itself; an alias here would say
                # the company re-tickered and also never left, which is not a
                # state the file can describe.
                raise ValueError(f"{name} line {number}: {symbol} aliases an open span")

        spans.append(
            MembershipSpan(symbol=symbol, added=added, removed=removed, price_symbol=price_symbol)
        )

    _reject_overlaps(spans, name)
    _reject_bad_aliases(spans, name)
    spans.sort(key=lambda span: (span.symbol, span.added))
    return spans


def _parse_date(value: str | None, filename: str, number: int, column: str) -> date | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{filename} line {number}: bad {column} date {text!r}") from exc


def _reject_overlaps(spans: Iterable[MembershipSpan], filename: str) -> None:
    by_symbol: dict[str, list[MembershipSpan]] = {}
    for span in spans:
        by_symbol.setdefault(span.symbol, []).append(span)

    for symbol, symbol_spans in by_symbol.items():
        ordered = sorted(symbol_spans, key=lambda span: span.added)
        for earlier, later in _pairs(ordered):
            if earlier.removed is None or earlier.removed > later.added:
                raise ValueError(f"{filename}: overlapping spans for {symbol}")


def _reject_bad_aliases(spans: Sequence[MembershipSpan], filename: str) -> None:
    """Check every §2.3a alias against the encoding §2.3 already pins.

    A rename is written as a removal of the old symbol and an addition of the new
    one *on the same date*, so the successor must have a span opening exactly
    where the aliased span closes. That makes the alias machine-checkable rather
    than a hopeful convention, and it catches the mistake that matters: an alias
    pointed at a symbol that was never the same company.

    What is deliberately *not* checked is whether the successor has cached price
    data. `COG -> CTRA` resolves correctly and still yields nothing, because
    Coterra was itself acquired and purged. That is a fact about the cache, and
    §2.4 reports it honestly as an uncovered span rather than a load failure.
    """
    added_on = {(span.symbol, span.added) for span in spans}

    for span in spans:
        if span.price_symbol is None:
            continue
        if (span.price_symbol, span.removed) not in added_on:
            raise ValueError(
                f"{filename}: {span.symbol} aliases {span.price_symbol}, which has no span "
                f"beginning {span.removed} (§2.3 writes a rename as remove+add on one date)"
            )

    for span in spans:
        if span.price_symbol is not None:
            _walk_chain(span, spans, filename)


def _walk_chain(start: MembershipSpan, spans: Sequence[MembershipSpan], filename: str) -> None:
    """Follow a rename chain to its end, refusing to loop forever."""
    seen = {start.symbol}
    symbol, day = start.price_symbol, start.removed

    while symbol is not None:
        if symbol in seen:
            raise ValueError(f"{filename}: rename chain from {start.symbol} cycles at {symbol}")
        seen.add(symbol)
        successor = next(
            (s for s in spans if s.symbol == symbol and s.added == day),
            None,
        )
        if successor is None:
            return
        symbol, day = successor.price_symbol, successor.removed


def _pairs(items: Sequence[MembershipSpan]) -> Iterator[tuple[MembershipSpan, MembershipSpan]]:
    return zip(items, items[1:], strict=False)
