"""Compile point-in-time S&P 500 membership from Wikipedia wikitext (§2.1).

Reverse-walks the change history from today's constituent list to reconstruct
(symbol, added, removed, price_symbol) intervals. Not part of the package; the
CSV it emits is. It is committed because the CSV is compiled rather than
hand-edited: a data file whose compiler is lost can be neither audited nor
rebuilt, and this one carries hand-sourced facts that exist nowhere else in the
repo (the rename table below).

The change table records index additions and removals only — its editors
explicitly keep ticker and name changes out of it — so every re-ticker is
supplied here as a sourced synthetic event instead of being inferred, and
becomes both a remove+add row pair (§2.3) and a price alias (§2.3a).

Fetch the two inputs with `?action=raw`:
    https://en.wikipedia.org/wiki/List_of_S%26P_500_companies
    https://en.wikipedia.org/wiki/Historical_components_of_the_S%26P_500

Usage: build_membership.py <constituents.wikitext> <changes.wikitext> <out.csv>
Then check the emitted anomaly count and alias tally on stderr before committing.
"""

from __future__ import annotations

import csv
import re
import sys
from datetime import date, datetime
from pathlib import Path

RETRIEVED = date(2026, 8, 16)
FLOOR = "1957-03-04"  # index inception: "member before the recorded history begins"

CONSTITUENTS_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
CHANGES_URL = "https://en.wikipedia.org/wiki/Historical_components_of_the_S%26P_500"

# Ticker changes for index members, each verified against a company press
# release, SEC filing or exchange notice. (old, new or None, effective date):
# the date is the first trading day under the new symbol. `None` means the line
# stopped trading without a successor symbol.
TICKER_CHANGES: list[tuple[str, str | None, str]] = [
    ("WLP", "ANTM", "2014-12-03"),  # WellPoint -> Anthem
    ("ACT", "AGN", "2015-06-15"),  # Actavis plc -> Allergan plc
    ("ANTM", "ELV", "2022-06-28"),  # Anthem -> Elevance Health
    ("JOYG", "JOY", "2011-12-06"),  # Joy Global, Nasdaq -> NYSE
    ("TSO", "ANDV", "2017-08-01"),  # Tesoro -> Andeavor
    ("Q", "IQV", "2017-11-15"),  # QuintilesIMS -> IQVIA
    ("DLPH", "APTV", "2017-12-05"),  # Delphi Automotive -> Aptiv
    ("PCLN", "BKNG", "2018-02-27"),  # Priceline Group -> Booking Holdings
    ("LUK", "JEF", "2018-05-24"),  # Leucadia National -> Jefferies Financial
    ("KORS", "CPRI", "2019-01-02"),  # Michael Kors -> Capri Holdings
    ("HRS", "LHX", "2019-07-01"),  # Harris -> L3Harris Technologies
    ("JEC", "J", "2019-12-10"),  # Jacobs Engineering
    ("IR", "TT", "2020-03-02"),  # Ingersoll-Rand plc -> Trane Technologies
    ("COG", "CTRA", "2021-10-04"),  # Cabot Oil & Gas -> Coterra Energy
    ("FB", "META", "2022-06-09"),  # Facebook -> Meta Platforms
    ("FBHS", "FBIN", "2022-12-15"),  # Fortune Brands Home & Security -> Innovations
    ("RE", "EG", "2023-07-10"),  # Everest Re -> Everest Group
    ("FLT", "CPAY", "2024-03-25"),  # FleetCor -> Corpay
    ("SATS", "ECHO", "2026-06-24"),  # EchoStar
]

# Symbols whose reconstructed spans are replaced wholesale, because the source
# rows are too tangled for the walk to read correctly. Each is stated per
# *symbol* — which is what the price fetch keys on — rather than per share class.
MANUAL_SPANS: dict[str, list[tuple[str, str]]] = {
    # Under Armour joined as Class A under UA (2014-05-01). The 2016 Class C
    # distribution added a second line, and on 2016-12-07 the symbols swapped:
    # Class A UA -> UAA, Class C UA.C -> UA. Both lines left on 2022-06-21. The
    # source table labels the Class C addition "UA" too, which the walk cannot
    # untangle. At symbol level: UA was a member throughout (Class A, then Class
    # C), and UAA from the swap onward.
    "UA": [("2014-05-01", "2022-06-21")],
    "UAA": [("2016-12-07", "2022-06-21")],
    # Four companies the change table records an addition for but no removal.
    # Each ends on the date it stopped being a member, taken from an SEC Form 25
    # delisting notice or the acquirer's completion filing; where only the
    # corporate action is sourced and not S&P's own effective date, the two
    # differ by at most a day or two. All four spans close more than six years
    # before the evaluation window opens, so none of this can reach a result.
    # Their successor symbols (CFC for Countrywide, USB for Firstar) are likewise
    # pre-window and are not reconstructed here.
    "CCR": [("1997-06-17", "2002-11-13")],  # first traded as CFC on 2002-11-13
    "FSR": [("1998-12-11", "2001-02-27")],  # merged into U.S. Bancorp (USB)
    "BUD": [("1976-07-01", "2008-11-18")],  # last traded 11-17; InBev closed 11-18
    "NCC": [("1994-09-30", "2009-01-02")],  # last traded 12-31; out at the 01-02 open
}


def clean_cell(text: str) -> str:
    text = re.sub(r"<ref[^>]*/>", "", text)
    text = re.sub(r"<ref.*?</ref>", "", text, flags=re.DOTALL)
    # Symbol templates: {{NyseSymbol|MMM}}, {{NasdaqSymbol|AAPL}}, {{BZX link|CBOE}}.
    text = re.sub(r"\{\{[^}|]+\|([^}|]+)\}\}", r"\1", text)
    text = re.sub(r"\[\[[^\]|]*\|([^\]]*)\]\]", r"\1", text)
    text = re.sub(r"\[\[([^\]]*)\]\]", r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


def normalize(symbol: str) -> str:
    """Wikipedia/S&P form to yfinance form: BRK.B -> BRK-B."""
    return symbol.strip().strip("|").strip().upper().replace(".", "-")


def parse_date(text: str) -> date | None:
    text = clean_cell(text)
    for fmt in ("%B %d, %Y", "%Y-%m-%d", "%b %d, %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def table_rows(wikitext: str, table_id: str) -> list[list[str]]:
    """Split one wikitable into rows of raw (uncleaned) cells."""
    start = wikitext.index(f'id="{table_id}"')
    end = wikitext.index("\n|}", start)
    body = wikitext[start:end]
    rows = []
    for chunk in body.split("\n|-")[1:]:
        cells: list[str] = []
        header_row = False
        for raw in chunk.split("\n"):
            line = raw.strip()
            if line.startswith("!"):
                header_row = True
                break
            if not line.startswith("|"):
                if cells:  # continuation of the previous cell (refs wrap lines)
                    cells[-1] += " " + line
                continue
            line = line[2:] if line.startswith("||") else line[1:]
            cells.extend(line.split("||"))
        if cells and not header_row:
            rows.append(cells)
    return rows


def main() -> None:
    constituents_text = Path(sys.argv[1]).read_text(encoding="utf-8")
    changes_text = Path(sys.argv[2]).read_text(encoding="utf-8")

    # ticker -> date added per the constituent table. Keyed by ticker, not by
    # linked article: dual-class listings (GOOGL/GOOG, FOX/FOXA, NWS/NWSA) share
    # one article, and keying on it silently collapses them.
    current: dict[str, date | None] = {}
    for row in table_rows(constituents_text, "constituents"):
        symbol = normalize(clean_cell(row[0])) if row else ""
        if len(row) < 6 or not symbol:
            continue
        current[symbol] = parse_date(row[5])
    print(f"constituents: {len(current)}", file=sys.stderr)

    # (date, added ticker or None, removed ticker or None)
    events: list[tuple[date, str | None, str | None]] = []
    skipped_future = 0
    for row in table_rows(changes_text, "changes"):
        if len(row) < 5:
            continue
        when = parse_date(row[0])
        if when is None:
            print(f"unparsed change date: {clean_cell(row[0])!r}", file=sys.stderr)
            continue
        if when > RETRIEVED:
            skipped_future += 1  # announced but not yet effective
            continue
        added = normalize(clean_cell(row[1])) or None
        removed = normalize(clean_cell(row[3])) or None
        if added or removed:
            events.append((when, added, removed))
    print(f"events: {len(events)} (dropped {skipped_future} not-yet-effective)", file=sys.stderr)

    renames = {(old, new) for old, new, _ in TICKER_CHANGES}
    synthetic = {(date.fromisoformat(when), new, old) for old, new, when in TICKER_CHANGES}
    # A few of these renames were also (incorrectly) recorded as index changes
    # in the source table. Adding a duplicate synthetic event would emit the
    # successor's span twice, once of them zero-length.
    synthetic -= set(events)
    events.extend(synthetic)

    # Real events sort *after* synthetic ones on the same date, so the reverse
    # walk sees them first. That matters where a symbol is recycled: the index
    # added the new Ingersoll Rand Inc under IR on the same day Trane vacated
    # the symbol, and the real addition has to claim IR before the rename hands
    # the older span back.
    events.sort(key=lambda event: (event[0], event not in synthetic))

    # Reverse walk. `state` maps a ticker believed to be a member in the era just
    # *before* the event being processed to the date it leaves (None = still a
    # member at the retrieval date).
    state: dict[str, date | None] = dict.fromkeys(current)
    intervals: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    anomalies: list[str] = []

    for when, added, removed in reversed(events):
        is_rename = added is not None and removed is not None and (removed, added) in renames

        if added is not None:
            if added in state:
                intervals.append((added, when.isoformat(), _iso(state.pop(added))))
                seen.add(added)
            elif is_rename and removed in state:
                # The change table lags renames: it can record a later removal
                # under the pre-rename symbol. The successor still held the
                # index line from the rename date until that removal.
                intervals.append((added, when.isoformat(), _iso(state[removed])))
                seen.add(added)
                state[removed] = when
                continue
            else:
                anomalies.append(f"{when}: added {added} is a member of nothing after")
                intervals.append((added, when.isoformat(), ""))
                seen.add(added)

        if removed is not None:
            if removed in state:
                anomalies.append(f"{when}: removed {removed} was already out")
            else:
                state[removed] = when

    for ticker, leaves in state.items():
        # Only a ticker untouched by any add event can take its start date from
        # the constituent table; for one that was re-added later, that column
        # describes the later span, not this earlier one.
        added_on = current.get(ticker) if ticker not in seen else None
        intervals.append((ticker, added_on.isoformat() if added_on else FLOOR, _iso(leaves)))

    intervals = [row for row in intervals if row[0] not in MANUAL_SPANS]
    intervals.extend(
        (symbol, added, removed)
        for symbol, spans in MANUAL_SPANS.items()
        for added, removed in spans
    )
    intervals.sort(key=lambda row: (row[0], row[1]))

    # §2.3a: attach the price alias to the *row* that closes on a re-ticker.
    # Span-scoped, never symbol-scoped — IR names two different companies either
    # side of 2020-03-02, so aliasing the symbol would price the second off the
    # first's successor. The value is the immediate successor; the loader walks
    # chains (WLP -> ANTM -> ELV).
    successor = {(old, when): new for old, new, when in TICKER_CHANGES}
    intervals = [
        (symbol, added, removed, successor.get((symbol, removed), ""))
        for symbol, added, removed in intervals
    ]
    aliased = sum(1 for row in intervals if row[3])
    print(f"price aliases attached: {aliased}/{len(TICKER_CHANGES)}", file=sys.stderr)
    missing = {(o, w) for o, _, w in TICKER_CHANGES} - {(r[0], r[2]) for r in intervals if r[3]}
    for old, when in sorted(missing):
        print(f"  ! rename {old} @ {when} matched no interval", file=sys.stderr)

    phantom = sorted({row[0] for row in intervals if not row[2]} - set(current))
    print(f"intervals: {len(intervals)}", file=sys.stderr)
    print(f"open-ended but not a current constituent: {len(phantom)}", file=sys.stderr)
    for ticker in phantom:
        print(f"  ? {ticker}", file=sys.stderr)
    anomalies = [note for note in anomalies if not any(f" {s} " in note for s in MANUAL_SPANS)]
    print(f"anomalies: {len(anomalies)}", file=sys.stderr)
    for note in anomalies:
        print(f"  ! {note}", file=sys.stderr)

    with open(sys.argv[3], "w", newline="", encoding="utf-8") as handle:
        handle.write(HEADER)
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["symbol", "added", "removed", "price_symbol"])
        writer.writerows(intervals)


def _iso(value: date | None) -> str:
    return value.isoformat() if value is not None else ""


HEADER = f"""\
# Point-in-time S&P 500 membership (SPEC-BACKTEST.md §2.1).
#
# Sources (both Wikipedia; the change history §2.1 cites lives on the companion
# "Historical components" article, linked from the list article):
#   {CONSTITUENTS_URL}
#   {CHANGES_URL}
# Retrieved: {RETRIEVED.isoformat()}
#
# Compiled by reverse-walking the change history from the constituent list as of
# the retrieval date: each change event closes or opens one membership interval.
# A symbol is a member on date d iff added <= d and (removed empty or d < removed)
# — inclusive of `added`, exclusive of `removed` (§2.2).
#
# Conventions:
#   * Symbols are in yfinance form (BRK.B -> BRK-B).
#   * `removed` empty means still a member at the retrieval date.
#   * added = {FLOOR} is a floor sentinel — S&P 500 inception — meaning "already a
#     member before this dataset's recorded history begins", not a verified date.
#   * Ticker changes are absent from the source change table by editorial policy
#     ("company name changes and ticker changes are not changes to the index"),
#     so each one is supplied from a company press release, SEC filing or
#     exchange notice and split into a removal of the old symbol and an addition
#     of the new one on the effective date, per §2.3.
#   * `price_symbol` (§2.3a) names the ticker whose cached series prices that
#     span, set only on rows that close on a re-ticker. Yahoo serves a renamed
#     company's whole history under its current ticker only — FB returns
#     nothing, META returns 2012 onward — so without it the pre-rename years are
#     unpriceable. It holds the *immediate* successor and the loader walks any
#     chain (WLP -> ANTM -> ELV). It is scoped to the row, never the symbol, and
#     the successor's series is read only up to this row's `removed` date.
#     Each alias is checked at load against §2.3's remove+add pairing.
#   * Changes announced but not yet effective at the retrieval date are excluded.
#
# Known imperfections (bias register row 8):
#   * The change table is community-maintained; dates and completeness may be
#     wrong in places, and pre-1990s coverage is sparse.
#   * Four pre-window spans (BUD, CCR, FSR, NCC) end at their acquisition's
#     completion date rather than a sourced index-removal date, which is accurate
#     to within days. All four close more than six years before the evaluation
#     window opens.
#   * Some symbols were reassigned to a different security rather than retired —
#     DLPH (to Delphi Technologies, 2017-12-05), IR (to Ingersoll Rand Inc,
#     2020-03-02), UA (to Under Armour Class C, 2016-12-07) and GOOG (to Alphabet
#     Class C, 2014-04-03). Where the earlier company simply re-tickered,
#     `price_symbol` now repairs the splice: DLPH is priced from APTV and IR from
#     TT for their pre-reassignment spans. UA and GOOG are share-class changes
#     rather than renames, so they are not repaired and stay in the bias register.
#   * The rename table below is the 19 pairs sourced so far. A handful of further
#     re-tickers (WLTW->WTW, ARNC->HWM, DWDP->DD, BHI->BKR) carry the same
#     signature but are not yet individually sourced, so they are absent: the
#     list is lossy, never wrong, and those spans stay uncovered.
"""


if __name__ == "__main__":
    main()
