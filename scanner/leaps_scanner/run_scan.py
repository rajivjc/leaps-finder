"""Scanner entrypoints.

    python -m leaps_scanner.run_scan full      # SPEC.md §3, weekly (Sat 02:00 UTC)
    python -m leaps_scanner.run_scan refresh   # SPEC.md §3, daily (Tue-Sat 22:30 UTC)

Both commands exit non-zero on failure so a red Actions run is impossible to
miss (SPEC.md §3.8).

The full scan is two-pass by construction: per-symbol data first (signals,
fundamentals, option economics, IV snapshot), then the cross-sectional pieces
that need the whole universe at once — the forward-P/E percentile and the
iv30/rv20 percentile that stands in for IV rank while a symbol's snapshot
history is warming up (§6).
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd

from leaps_scanner import db, fundamentals, indicators, options, prices, scoring, universe
from leaps_scanner.config import ConfigError, load_env_file, load_settings
from leaps_scanner.fundamentals import Fundamentals
from leaps_scanner.indicators import InsufficientHistory, Signals
from leaps_scanner.options import OptionsResult

logger = logging.getLogger(__name__)

COMMANDS = ("full", "refresh")

# SPEC.md §9: losing more than a fifth of the universe means the scan does not
# get to call itself complete. Option-chain and fundamentals fetch failures
# count toward the same ceiling — a row without option economics or with a
# silently-empty financials payload is partial data too.
MAX_MISSING_FRACTION = 0.20

# Calendar window fetched for IV rank; scoring then uses the trailing
# IV_RANK_WINDOW snapshots within it. 252 snapshots at M5's daily cadence
# (5/week minus holidays) span ~367 calendar days, so the fetch window needs
# headroom beyond a year or it would silently truncate the rank window.
IV_HISTORY_DAYS = 420

M3_NOTE = "M3: five filters, option economics and scoring; exit monitor lands in M5."


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
    option_failures: int = 0
    fundamentals_failures: int = 0

    @property
    def ok(self) -> bool:
        return self.status == db.STATUS_OK


@dataclass(frozen=True)
class IvContext:
    """A symbol's IV-rank inputs after the cross-sectional pass (§6)."""

    value: float | None
    status: str


def earnings_distance(next_earnings: date | None, as_of: date) -> int | None:
    """Days from the scan date to the next earnings, or None if unknown.

    A date at or before `as_of` is a stale calendar entry, not a next earnings
    date; it is treated as unknown rather than as "earnings today".
    """
    if next_earnings is None or next_earnings <= as_of:
        return None
    return (next_earnings - as_of).days


def build_scan_row(
    scan_id: int,
    symbol: str,
    signals: Signals,
    *,
    fundamentals: Fundamentals,
    options_result: OptionsResult | None,
    iv_context: IvContext,
    fwd_pe_percentile: float | None,
    as_of: date,
) -> dict:
    """One complete `scan_results` row: signals, fundamentals snapshot,
    contract economics, subscores, composite and preset flags.

    Pure — everything cross-sectional arrives precomputed — so the golden-file
    test can pin an entire row from fixed inputs.
    """
    contract = options_result.contract if options_result is not None else None
    iv30 = options_result.iv30 if options_result is not None else None
    earnings_dte = earnings_distance(fundamentals.next_earnings, as_of)
    upside_adj = scoring.upside_adjusted(fundamentals.analyst_target, signals.spot)

    s_trend = scoring.trend_score(signals)
    s_quality, quality_all_present = scoring.quality_score(fundamentals)
    s_option = scoring.option_score(contract, iv30, iv_context.value)
    s_valuation = scoring.valuation_score(upside_adj, fwd_pe_percentile)
    s_entry = scoring.entry_score(signals.stoch_k, signals.weeks_since_cross_up)
    score = scoring.composite_score(s_trend, s_quality, s_option, s_valuation, s_entry)

    presets = scoring.preset_flags(
        trend_pass=signals.trend_pass,
        stoch_k=signals.stoch_k,
        turning_up=signals.turning_up,
        iv_rank_value=iv_context.value,
        earnings_dte=earnings_dte,
        spread_pct=contract.spread_pct if contract else None,
        oi=contract.oi if contract else None,
        s_quality=s_quality,
        quality_all_present=quality_all_present,
    )

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
        # Standalone checklist booleans — M3 addendum thresholds (scoring.py).
        "quality_pass": s_quality is not None and s_quality >= scoring.QUALITY_PASS_MIN,
        "valuation_pass": upside_adj is not None and upside_adj > 0,
        "iv_pass": iv_context.value is not None and iv_context.value <= scoring.IV_PASS_MAX,
        "passes_strict": presets.strict,
        "passes_balanced": presets.balanced,
        "passes_wide": presets.wide,
        "s_trend": s_trend,
        "s_quality": s_quality,
        "s_option": s_option,
        "s_valuation": s_valuation,
        "s_entry": s_entry,
        "score": score,
        "op_margin": fundamentals.op_margin,
        "roe": fundamentals.roe,
        "net_debt_ebitda": fundamentals.net_debt_ebitda,
        "rev_growth": fundamentals.rev_growth,
        "fcf_margin": fundamentals.fcf_margin,
        "fwd_pe": fundamentals.fwd_pe,
        "analyst_target": fundamentals.analyst_target,
        "upside_adj": upside_adj,
        "opt_expiry": contract.expiry.isoformat() if contract else None,
        "opt_strike": contract.strike if contract else None,
        "opt_dte": contract.dte if contract else None,
        "opt_delta": contract.delta if contract else None,
        "opt_mid": contract.mid if contract else None,
        "opt_bid": contract.bid if contract else None,
        "opt_ask": contract.ask if contract else None,
        "opt_spread_pct": contract.spread_pct if contract else None,
        "opt_oi": contract.oi if contract else None,
        "opt_iv": contract.iv if contract else None,
        "breakeven": contract.breakeven if contract else None,
        "breakeven_pct": contract.breakeven_pct if contract else None,
        "cost_pct_spot": contract.cost_pct_spot if contract else None,
        "iv30": iv30,
        "iv_rank": iv_context.value,
        "iv_rank_status": iv_context.status,
        "next_earnings": (
            fundamentals.next_earnings.isoformat() if earnings_dte is not None else None
        ),
        "earnings_dte": earnings_dte,
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


def expected_as_of_date(today: date | None = None) -> date:
    """The Friday the scan is provisionally dated with, before signals exist.

    The `scans` row is opened *before* any fetching so that a crashed run still
    leaves a record, but `as_of_date` is not null — so the row starts with the
    most recent Friday and `finish_scan` corrects it from the actual signals.
    """
    reference = date.today() if today is None else today
    # weekday(): Mon=0 .. Fri=4. Saturday (the cron slot) resolves to yesterday.
    return reference - timedelta(days=(reference.weekday() - 4) % 7)


def assess(
    universe_count: int,
    evaluated_count: int,
    option_failures: int = 0,
    fundamentals_failures: int = 0,
    data_failures: int | None = None,
) -> tuple[str, str]:
    """Decide the scan's status from its coverage (SPEC.md §9).

    Every kind of gap is partial data: symbols with no price history, and
    evaluated symbols whose option chains or fundamentals payloads never
    arrived. (A chain that arrived but contained no valid contract is a
    market fact, recorded as nulls, and does not count here.) `data_failures`
    is the deduplicated count of evaluated symbols with either fetch failure;
    it defaults to the sum when the caller has no overlap to deduplicate.
    """
    missing = universe_count - evaluated_count
    if data_failures is None:
        data_failures = option_failures + fundamentals_failures
    incomplete = missing + data_failures
    fraction = incomplete / universe_count if universe_count else 1.0
    detail = (
        f"{missing}/{universe_count} symbols without price history, "
        f"{option_failures} evaluated without option data, "
        f"{fundamentals_failures} without fundamentals. {M3_NOTE}"
    )

    if universe_count == 0:
        return db.STATUS_FAILED, "empty universe: no symbol cleared the market-cap floor"

    if fraction > MAX_MISSING_FRACTION:
        return (
            db.STATUS_FAILED,
            f"{incomplete}/{universe_count} symbols ({fraction:.1%}) incomplete, "
            f"over the {MAX_MISSING_FRACTION:.0%} ceiling. {detail}",
        )

    return db.STATUS_OK, detail


def evaluate_all(frames: Mapping[str, pd.DataFrame]) -> dict[str, Signals]:
    """Run §4 over every fetched symbol, dropping those with thin history."""
    signals: dict[str, Signals] = {}
    for symbol, frame in frames.items():
        try:
            signals[symbol] = indicators.evaluate(frame)
        except InsufficientHistory as exc:
            logger.info("skipping %s: %s", symbol, exc)
    return signals


def resolve_iv_contexts(
    symbols: Sequence[str],
    *,
    history: Mapping[str, list[tuple[date, float]]],
    iv30_by_symbol: Mapping[str, float | None],
    rv20_by_symbol: Mapping[str, float | None],
    as_of: date,
) -> dict[str, IvContext]:
    """§6 IV rank, cross-sectional pass.

    A symbol with a current iv30 and ≥ 120 snapshots in the trailing window
    gets the real rank; everyone else is warming up and gets the
    cross-sectional percentile of iv30/rv20 substituted in its place (same
    scale, same preset thresholds). A symbol with *no* current iv30 never
    gets a rank at all — §6 defines the rank over the current value, and
    stamping last week's rank `ok` in a row whose iv30 is null would assert
    an IV fact the scan does not have. The current snapshot is appended in
    memory when the history read predates this scan's write.
    """
    ratios: dict[str, float] = {}
    for symbol in symbols:
        iv30 = iv30_by_symbol.get(symbol)
        rv20 = rv20_by_symbol.get(symbol)
        if iv30 is not None and rv20 is not None and rv20 > 0:
            ratios[symbol] = iv30 / rv20
    population = list(ratios.values())

    contexts: dict[str, IvContext] = {}
    for symbol in symbols:
        iv30 = iv30_by_symbol.get(symbol)
        rank = None
        if iv30 is not None:
            series = list(history.get(symbol, []))
            if not series or series[-1][0] != as_of:
                series.append((as_of, iv30))
            values = [value for _, value in series]
            if len(values[-scoring.IV_RANK_WINDOW :]) >= scoring.IV_RANK_MIN_SNAPSHOTS:
                rank = scoring.iv_rank(values)

        if rank is not None:
            contexts[symbol] = IvContext(value=rank, status=scoring.IV_RANK_STATUS_OK)
        elif symbol in ratios:
            contexts[symbol] = IvContext(
                value=scoring.percentile_of(ratios[symbol], population),
                status=scoring.IV_RANK_STATUS_WARMING,
            )
        else:
            contexts[symbol] = IvContext(value=None, status=scoring.IV_RANK_STATUS_WARMING)
    return contexts


def run_full_scan(
    client,
    *,
    seed: Sequence[universe.SeedEntry] | None = None,
    market_cap_fetcher=universe.yahoo_market_cap,
    downloader=prices.yahoo_downloader,
    fundamentals_fetcher=fundamentals.yahoo_fundamentals,
    expiries_fetcher=options.yahoo_expiries,
    chain_fetcher=options.yahoo_chain,
    risk_free_fetcher=options.yahoo_risk_free_rate,
    min_market_cap: float = universe.MIN_MARKET_CAP,
    sleeper=time.sleep,
) -> ScanReport:
    """The weekly pipeline (SPEC.md §3, steps 1-6; the exit monitor is M5).

    `sleeper` backs every throttle and backoff in the run; tests pass a no-op
    so the pipeline suite stays instant.

    The `scans` row is opened before any work begins and closed in an `except`,
    so a run that dies mid-fetch leaves a `failed` row rather than no trace at
    all. Its `as_of_date` starts as the most recent Friday and is corrected once
    the signals say which week they actually describe.
    """
    scan_id = db.start_scan(client, "full", expected_as_of_date())
    try:
        return _run_full_scan(
            client,
            scan_id,
            seed=seed,
            market_cap_fetcher=market_cap_fetcher,
            downloader=downloader,
            fundamentals_fetcher=fundamentals_fetcher,
            expiries_fetcher=expiries_fetcher,
            chain_fetcher=chain_fetcher,
            risk_free_fetcher=risk_free_fetcher,
            min_market_cap=min_market_cap,
            sleeper=sleeper,
        )
    except Exception as exc:
        db.finish_scan(
            client,
            scan_id,
            status=db.STATUS_FAILED,
            universe_count=0,
            matches_count=0,
            notes=f"scan aborted: {type(exc).__name__}: {exc}",
        )
        raise


def _run_full_scan(
    client,
    scan_id: int,
    *,
    seed: Sequence[universe.SeedEntry] | None,
    market_cap_fetcher,
    downloader,
    fundamentals_fetcher,
    expiries_fetcher,
    chain_fetcher,
    risk_free_fetcher,
    min_market_cap: float,
    sleeper,
) -> ScanReport:
    entries = list(seed) if seed is not None else universe.load_seed()
    logger.info("seed list: %d symbols", len(entries))

    caps = universe.fetch_market_caps(
        [entry.symbol for entry in entries], fetcher=market_cap_fetcher, sleeper=sleeper
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

    outcome = prices.fetch_daily_ohlcv(
        [entry.symbol for entry in selected], downloader=downloader, sleeper=sleeper
    )
    if outcome.failed:
        logger.warning("price fetch failed for %d symbols", len(outcome.failed))

    signals_by_symbol = evaluate_all(outcome.frames)
    logger.info("evaluated %d symbols", len(signals_by_symbol))

    as_of = resolve_as_of_date(signals_by_symbol) if signals_by_symbol else expected_as_of_date()
    symbols = sorted(signals_by_symbol)

    funds_outcome = fundamentals.fetch_fundamentals(
        symbols, fetcher=fundamentals_fetcher, sleeper=sleeper
    )
    funds = funds_outcome.results
    if funds_outcome.failed:
        logger.warning("fundamentals fetch failed for %d symbols", len(funds_outcome.failed))

    options_outcome = options.OptionsOutcome(results={}, failed=())
    if symbols:
        # §5.2's r, fetched once. Without it no delta is computable for
        # anyone, so a scan that cannot get it aborts (and is recorded as
        # failed) rather than writing a universe of half-rows. Skipped
        # entirely when nothing survived to be scored, so an empty-universe
        # run reports the real root cause instead of a rate error.
        rate = prices.retry_fetch(
            risk_free_fetcher,
            describe="risk-free rate (^IRX)",
            throttle=prices.Throttle(sleeper=sleeper),
            sleeper=sleeper,
        )
        if rate is None:
            raise RuntimeError(
                "risk-free rate (^IRX) unavailable; option deltas cannot be computed"
            )

        options_outcome = options.fetch_options(
            [
                options.OptionQuery(
                    symbol=symbol,
                    spot=signals_by_symbol[symbol].spot,
                    dividend_yield=funds[symbol].dividend_yield,
                )
                for symbol in symbols
            ],
            today=as_of,
            r=rate,
            expiries_fetcher=expiries_fetcher,
            chain_fetcher=chain_fetcher,
            sleeper=sleeper,
        )
        if options_outcome.failed:
            logger.warning("option chain fetch failed for %d symbols", len(options_outcome.failed))

    iv30_by_symbol = {symbol: result.iv30 for symbol, result in options_outcome.results.items()}
    rv20_by_symbol = {
        symbol: options.realized_vol_20d(
            outcome.frames[symbol].loc[: pd.Timestamp(signals_by_symbol[symbol].as_of_date)][
                "Close"
            ]
        )
        for symbol in symbols
    }

    # §3.5: snapshot first, then read the window back — so the current value
    # is part of its own 252-snapshot range and a rerun stays idempotent.
    db.upsert_iv_snapshots(
        client,
        [
            {
                "symbol": symbol,
                "snap_date": as_of.isoformat(),
                "iv30": iv30_by_symbol.get(symbol),
                "rv20": rv20_by_symbol.get(symbol),
            }
            for symbol in symbols
            if iv30_by_symbol.get(symbol) is not None or rv20_by_symbol.get(symbol) is not None
        ],
    )
    history = db.fetch_iv_history(
        client, since=as_of - timedelta(days=IV_HISTORY_DAYS), until=as_of
    )
    iv_contexts = resolve_iv_contexts(
        symbols,
        history=history,
        iv30_by_symbol=iv30_by_symbol,
        rv20_by_symbol=rv20_by_symbol,
        as_of=as_of,
    )

    # Cross-sectional forward-P/E percentile (§6 Valuation).
    pe_population = [funds[s].fwd_pe for s in symbols if funds[s].fwd_pe is not None]
    fwd_pe_percentiles = {
        symbol: (
            scoring.percentile_of(funds[symbol].fwd_pe, pe_population)
            if funds[symbol].fwd_pe is not None
            else None
        )
        for symbol in symbols
    }

    rows = [
        build_scan_row(
            scan_id,
            symbol,
            signals_by_symbol[symbol],
            fundamentals=funds[symbol],
            options_result=options_outcome.results.get(symbol),
            iv_context=iv_contexts[symbol],
            fwd_pe_percentile=fwd_pe_percentiles[symbol],
            as_of=as_of,
        )
        for symbol in symbols
    ]
    # matches_count = the Wide preset, the loosest §6 tier — "anything worth a
    # look". (Rows from scans 1-3 predate M3 and counted trend+zone+turning
    # instead; the column is not comparable across that boundary.)
    matches = sum(1 for row in rows if row["passes_wide"])
    # A symbol can fail both fetches; the ceiling counts it once.
    data_failures = len(set(options_outcome.failed) | set(funds_outcome.failed))
    status, notes = assess(
        len(selected),
        len(signals_by_symbol),
        option_failures=len(options_outcome.failed),
        fundamentals_failures=len(funds_outcome.failed),
        data_failures=data_failures,
    )

    db.write_scan_results(client, rows)
    # Average volume comes from the history just fetched rather than a second
    # lookup, so it is always consistent with the bars the signals used.
    db.upsert_tickers(
        client,
        [
            {"symbol": symbol, "avg_volume_30d": signals_by_symbol[symbol].avg_volume_30d}
            for symbol in symbols
        ],
    )
    db.finish_scan(
        client,
        scan_id,
        status=status,
        universe_count=len(selected),
        matches_count=matches,
        notes=notes,
        as_of_date=as_of,
    )

    return ScanReport(
        as_of_date=as_of,
        universe_count=len(selected),
        evaluated_count=len(signals_by_symbol),
        missing_count=len(selected) - len(signals_by_symbol),
        matches_count=matches,
        status=status,
        notes=notes,
        option_failures=len(options_outcome.failed),
        fundamentals_failures=len(funds_outcome.failed),
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
