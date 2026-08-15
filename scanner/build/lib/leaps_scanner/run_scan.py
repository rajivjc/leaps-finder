"""Scanner entrypoints.

    python -m leaps_scanner.run_scan full      # SPEC.md §3, weekly (Sat 02:00 UTC)
    python -m leaps_scanner.run_scan refresh   # SPEC.md §3, daily (Tue-Sat 22:30 UTC)

Both commands exit non-zero on failure so a red Actions run is impossible to
miss (SPEC.md §3.8).
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

import pandas as pd

from leaps_scanner import db, indicators, prices, universe
from leaps_scanner.config import ConfigError, load_env_file, load_settings
from leaps_scanner.indicators import InsufficientHistory, Signals

logger = logging.getLogger(__name__)

COMMANDS = ("full", "refresh")

# SPEC.md §9: losing more than a fifth of the universe means the scan does not
# get to call itself complete.
MAX_MISSING_FRACTION = 0.20

M2_NOTE = "M2: trend and weekly stochastic only; quality, valuation and IV filters land in M3."


@dataclass(frozen=True)
class ScanReport:
    """What a full scan did, for the `scans` row and the exit code."""

    as_of_date: date
    universe_count: int
    evaluated_count: int
    missing_count: int
    matches_count: int
    status: str
    notes: str

    @property
    def missing_fraction(self) -> float:
        return self.missing_count / self.universe_count if self.universe_count else 0.0

    @property
    def ok(self) -> bool:
        return self.status == db.STATUS_OK


def is_match(signals: Signals) -> bool:
    """The filters implemented so far: trend, plus the weekly stochastic entry.

    Quality, valuation and IV join this in M3; until then `matches_count` counts
    what has actually been evaluated rather than implying a full five-filter pass.
    """
    return signals.trend_pass and signals.in_zone and signals.turning_up


def build_scan_row(scan_id: int, symbol: str, signals: Signals) -> dict:
    """One `scan_results` row. Columns owned by later milestones stay unset."""
    return {
        "scan_id": scan_id,
        "symbol": symbol,
        "spot": signals.spot,
        "pct_off_52w_high": signals.pct_off_52w_high,
        "sma50": signals.sma50,
        "sma200": signals.sma200,
        "stoch_k": signals.stoch_k,
        "stoch_d": signals.stoch_d,
        "stoch_k_prev": signals.stoch_k_prev,
        "turning_up": signals.turning_up,
        "in_zone": signals.in_zone,
        "trend_pass": signals.trend_pass,
    }


def resolve_as_of_date(signals_by_symbol: Mapping[str, Signals]) -> date:
    """The week the scan speaks for: the one most symbols completed.

    A symbol halted into Friday can trail the rest by a week; the majority date
    is what the scan is labelled with.
    """
    if not signals_by_symbol:
        raise ValueError("no signals to date the scan with")

    counts = Counter(signal.as_of_date for signal in signals_by_symbol.values())
    most_common = max(counts.items(), key=lambda item: (item[1], item[0]))
    return most_common[0]


def assess(universe_count: int, evaluated_count: int) -> tuple[str, str]:
    """Decide the scan's status from its coverage (SPEC.md §9)."""
    missing = universe_count - evaluated_count
    fraction = missing / universe_count if universe_count else 1.0

    if universe_count == 0:
        return db.STATUS_FAILED, "empty universe: no symbol cleared the market-cap floor"

    if fraction > MAX_MISSING_FRACTION:
        return (
            db.STATUS_FAILED,
            f"{missing}/{universe_count} symbols ({fraction:.1%}) missing, "
            f"over the {MAX_MISSING_FRACTION:.0%} ceiling. {M2_NOTE}",
        )

    return db.STATUS_OK, f"{missing}/{universe_count} symbols missing. {M2_NOTE}"


def evaluate_all(frames: Mapping[str, pd.DataFrame]) -> dict[str, Signals]:
    """Run §4 over every fetched symbol, dropping those with thin history."""
    signals: dict[str, Signals] = {}
    for symbol, frame in frames.items():
        try:
            signals[symbol] = indicators.evaluate(frame)
        except InsufficientHistory as exc:
            logger.info("skipping %s: %s", symbol, exc)
    return signals


def run_full_scan(
    client,
    *,
    seed: Sequence[universe.SeedEntry] | None = None,
    market_cap_fetcher=universe.yahoo_market_cap,
    downloader=prices.yahoo_downloader,
    min_market_cap: float = universe.MIN_MARKET_CAP,
) -> ScanReport:
    """The weekly pipeline (SPEC.md §3, steps 1-2 and 6 as far as M2 goes)."""
    entries = list(seed) if seed is not None else universe.load_seed()
    logger.info("seed list: %d symbols", len(entries))

    caps = universe.fetch_market_caps(
        [entry.symbol for entry in entries], fetcher=market_cap_fetcher
    )
    selected = universe.select_universe(entries, caps, min_market_cap=min_market_cap)
    logger.info("universe: %d symbols at or above %.0fB", len(selected), min_market_cap / 1e9)

    db.upsert_tickers(
        client,
        [
            {
                "symbol": entry.symbol,
                "name": entry.name,
                "sector": entry.sector,
                "industry": entry.industry,
                "market_cap": entry.market_cap,
            }
            for entry in selected
        ],
    )

    outcome = prices.fetch_daily_ohlcv([entry.symbol for entry in selected], downloader=downloader)
    if outcome.failed:
        logger.warning("price fetch failed for %d symbols", len(outcome.failed))

    signals_by_symbol = evaluate_all(outcome.frames)
    logger.info("evaluated %d symbols", len(signals_by_symbol))

    status, notes = assess(len(selected), len(signals_by_symbol))
    as_of = resolve_as_of_date(signals_by_symbol) if signals_by_symbol else date.today()
    matches = sum(1 for signal in signals_by_symbol.values() if is_match(signal))

    scan_id = db.start_scan(client, "full", as_of)
    db.write_scan_results(
        client,
        [
            build_scan_row(scan_id, symbol, signal)
            for symbol, signal in sorted(signals_by_symbol.items())
        ],
    )
    # Average volume comes from the history just fetched rather than a second
    # lookup, so it is always consistent with the bars the signals used.
    db.upsert_tickers(
        client,
        [
            {"symbol": symbol, "avg_volume_30d": signal.avg_volume_30d}
            for symbol, signal in sorted(signals_by_symbol.items())
        ],
    )
    db.finish_scan(
        client,
        scan_id,
        status=status,
        universe_count=len(selected),
        matches_count=matches,
        notes=notes,
    )

    return ScanReport(
        as_of_date=as_of,
        universe_count=len(selected),
        evaluated_count=len(signals_by_symbol),
        missing_count=len(selected) - len(signals_by_symbol),
        matches_count=matches,
        status=status,
        notes=notes,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_scan",
        description="LEAPS Finder scanner (SPEC.md §3).",
    )
    parser.add_argument(
        "command",
        choices=COMMANDS,
        help="full: weekly universe scan and rescore. refresh: daily quotes, IV snapshot, exits.",
    )
    return parser


def full_scan() -> int:
    """Weekly scan. Returns the process exit code."""
    client = db.service_client()
    report = run_full_scan(client)

    logger.info(
        "scan %s: %d/%d evaluated, %d matches, as of %s",
        report.status,
        report.evaluated_count,
        report.universe_count,
        report.matches_count,
        report.as_of_date,
    )
    if not report.ok:
        print(f"error: scan recorded as {report.status} — {report.notes}", file=sys.stderr)
        return 1
    return 0


def daily_refresh() -> int:
    """Daily refresh: quotes, IV snapshot, daily exit rules. No rescoring."""
    raise NotImplementedError("daily_refresh lands in M5 (risk engine + daily job)")


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    load_env_file()

    try:
        load_settings()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    return full_scan() if args.command == "full" else daily_refresh()


if __name__ == "__main__":  # pragma: no cover - exercised via the CLI
    raise SystemExit(main())
