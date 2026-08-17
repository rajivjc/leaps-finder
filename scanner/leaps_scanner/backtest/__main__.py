"""`python -m leaps_scanner.backtest` — manual, local, never a scheduled job.

Resolve the window, build the point-in-time universe, fill the price cache,
report §2.4 coverage, replay §4 over the universe, print §6.1's Track A tables
for the stock track and every §5.6 configuration of the LEAP overlay, then run
§6.2's sleeve and §6.3's benchmarks. §8 puts the committed report under
`docs/backtest/<run-date>/`; nothing is written unless `--report-dir` asks for
it, so an exploratory run never touches the repo.

§2.4's floor gates the engine, not just the report: below 80% coverage the run
is `failed` and computes no statistics, because a headline number over a
universe that thin would be exactly the "partial data looking complete" the
spec forbids.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from leaps_scanner.backtest import data, engine, metrics, report, sleeve, subscore, synthetic
from leaps_scanner.backtest.membership import Membership

logger = logging.getLogger("leaps_scanner.backtest")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m leaps_scanner.backtest")
    parser.add_argument(
        "--run-date",
        type=date.fromisoformat,
        default=None,
        help="evaluate as of this date (default: today); pinning it makes a run reproducible",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=data.DEFAULT_CACHE_DIR,
        help=f"Parquet cache directory (default: {data.DEFAULT_CACHE_DIR})",
    )
    parser.add_argument(
        "--no-fetch",
        action="store_true",
        help="compute from the existing cache only, making no network calls",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="re-fetch every symbol even if the cache already covers it",
    )
    parser.add_argument(
        "--coverage-json",
        type=Path,
        default=None,
        help="also write the coverage report as JSON to this path",
    )
    parser.add_argument(
        "--coverage-only",
        action="store_true",
        help="stop after §2.4 coverage; do not replay the §4 signal engine",
    )
    parser.add_argument(
        "--track-a-json",
        type=Path,
        default=None,
        help="write §6.1's Track A statistics as JSON to this path",
    )
    parser.add_argument(
        "--trades-json",
        type=Path,
        default=None,
        help="write every simulated trade as JSON to this path",
    )
    parser.add_argument(
        "--no-overlay",
        action="store_true",
        help="stock track only; skip §5's synthetic LEAP overlay and its sensitivity grid",
    )
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=None,
        help=(
            "write §8's report.md, results.json and equity SVGs under "
            "<dir>/<run-date>/ (this is the committed deliverable)"
        ),
    )
    parser.add_argument(
        "--no-sleeve",
        action="store_true",
        help="skip §6.2's sleeve simulation and §6.3's benchmarks",
    )
    parser.add_argument(
        "--subscore-dir",
        type=Path,
        default=None,
        help=(
            "score every base-variant trade with SPEC.md §6's Trend and Entry subscores "
            "as of its signal date and write the analysis under <dir>/<run-date>-subscore/ "
            "(analysis, not part of §8's published run)"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    run_date = args.run_date or date.today()
    window = data.evaluation_window(run_date)
    membership = Membership.load()
    cache = data.PriceCache(args.cache_dir)

    universe = membership.members_between(window.start, window.end)
    # §2.3a successors are members themselves today, so this adds nothing — but
    # the pairing is a convention of the compiled file, not a guarantee, and a
    # successor that stopped being a member would otherwise go unfetched and take
    # its predecessor's whole span down with it.
    successors = membership.price_symbols_between(window.start, window.end)
    logger.info(
        "window %s..%s (fetch from %s); %d point-in-time members, %d rename successors",
        window.start,
        window.end,
        window.fetch_start,
        len(universe),
        len(successors),
    )

    if args.no_fetch:
        logger.info("--no-fetch: computing from the cache as it stands")
    else:
        summary = data.fill_cache(
            [*universe, *successors], window, cache, run_date=run_date, refresh=args.refresh
        )
        logger.info(
            "fetched %d, topped up %d, reused %d, no data for %d",
            len(summary.fetched),
            len(summary.topped_up),
            len(summary.reused),
            len(summary.failed),
        )

    coverage = data.compute_coverage(membership, cache, window, run_date=run_date)
    coverage_report = coverage.to_json()
    if args.coverage_json:
        args.coverage_json.write_text(coverage_report, encoding="utf-8")
    print(coverage_report, end="")

    print(_summary_line(coverage), file=sys.stderr)

    # §2.4: below the floor the run is `failed` and commits no report, so it
    # must not compute one either — a statistic over a universe this thin is
    # exactly the partial data that must never look complete.
    if coverage.status == "failed":
        logger.error(
            "coverage %.1f%% is below the %.0f%% floor: run status failed, no report",
            100 * coverage.ratio,
            100 * data.COVERAGE_FAIL_BELOW,
        )
        return 1
    if coverage.low_coverage_warning:
        logger.warning(
            "coverage %.1f%% is below %.0f%%: every headline table must carry the warning",
            100 * coverage.ratio,
            100 * data.COVERAGE_WARN_BELOW,
        )

    if args.coverage_only:
        return 0

    return _run_engine(membership, cache, window, coverage, args)


def _run_engine(
    membership: Membership,
    cache: data.PriceCache,
    window: data.Window,
    coverage: data.Coverage,
    args: argparse.Namespace,
) -> int:
    """§4's replay and §6.1's Track A tables, for both tracks.

    Returns the process exit status, so a run that was asked for a report and
    could not produce one fails loudly. A caller gating on `$?` would otherwise
    read "nothing written" as success and go on to deploy whatever stale report
    was already committed.

    `coverage` is passed into the statistics rather than merely logged above:
    acceptance 2 wants the low-coverage warning on every headline table, and
    these are headline tables.

    The overlays are replayed inside the same pass over the universe: §5.6 makes
    sharing the signals and the stock track normative, and the engine already
    holds each chain's panel while it walks it.
    """
    overlays = () if args.no_overlay else synthetic.build_overlays(cache)
    result = engine.run(membership, cache, window, overlays=overlays)
    logger.info(
        "engine: %d rename chains, %d priced; %s",
        result.chains,
        result.priced_chains,
        ", ".join(f"{name} {len(rows)} trades" for name, rows in result.trades.items()),
    )

    benchmark = metrics.BenchmarkPrices(cache.load_benchmark())
    stats = [
        metrics.track_a(
            name, result.trades[name], result.skipped[name], benchmark, coverage=coverage
        )
        for name in result.trades
    ]
    stats.extend(
        metrics.track_a_overlay(
            overlay.config, overlay.trades, overlay.skipped, benchmark, coverage=coverage
        )
        for overlay in overlays
    )

    if args.track_a_json:
        args.track_a_json.write_text(metrics.track_a_json(stats), encoding="utf-8")
    if args.trades_json:
        args.trades_json.write_text(_trades_json(result, overlays), encoding="utf-8")

    for item in stats:
        print(_track_a_line(item), file=sys.stderr)

    if args.subscore_dir:
        _run_subscore(result, overlays, cache, coverage, args.subscore_dir)

    if args.no_sleeve or not overlays:
        if args.report_dir:
            logger.error(
                "--report-dir needs the sleeve and the overlay, but %s was passed: nothing written",
                "--no-sleeve" if args.no_sleeve else "--no-overlay",
            )
            return 1
        return 0

    sleeves, benchmarks, calendar, unlabelled = _run_sleeve(
        result, overlays, cache, window, coverage
    )
    for item in sleeves:
        print(_sleeve_line(item), file=sys.stderr)
    for item in benchmarks:
        print(_benchmark_line(item), file=sys.stderr)

    if args.report_dir:
        directory = args.report_dir / _run_date_of(coverage)
        written = report.write_report(
            report.RunInputs(
                run_date=coverage.run_date,
                window=window,
                coverage=coverage,
                stats=stats,
                sleeves=sleeves,
                benchmarks=benchmarks,
                calendar=calendar,
                unlabelled_traded=unlabelled,
            ),
            directory,
        )
        print(f"wrote {len(written)} files to {directory}", file=sys.stderr)
    return 0


def _run_date_of(coverage: data.Coverage) -> str:
    return coverage.run_date.isoformat()


def _run_subscore(
    result: engine.EngineResult,
    overlays: Sequence[synthetic.LeapOverlay],
    cache: data.PriceCache,
    coverage: data.Coverage,
    directory: Path,
) -> None:
    """Score the base variant's trades with §6's Trend and Entry (analysis only).

    The base zone variant alone: P13 reports Strict alongside as a sensitivity,
    and running the analysis over both would invite picking the flattering one.
    The overlay return is attached on `(chain, entry_date)` — the §2.3a-safe key,
    since a chain outlives the ticker it traded under.

    Writes under its own dated directory rather than into §8's published run,
    which this must not disturb.
    """
    trades = result.trades[subscore.VARIANT]
    base = next((item for item in overlays if item.name == "base"), None)
    overlay_returns = (
        {}
        if base is None
        else {(item.trade.chain, item.trade.entry_date): item.r_overlay for item in base.trades}
    )

    scored = subscore.score_trades(
        trades,
        cache,
        metrics.BenchmarkPrices(cache.load_benchmark()),
        overlay_returns=overlay_returns,
    )
    payload = subscore.build_results(scored, run_date=coverage.run_date, variant=subscore.VARIANT)
    written = subscore.write_report(payload, directory / f"{_run_date_of(coverage)}-subscore")
    print(_subscore_line(scored), file=sys.stderr)
    print(f"wrote {len(written)} subscore files", file=sys.stderr)


def _subscore_line(scored: subscore.ScoreResult) -> str:
    return (
        f"subscore: scored {len(scored.scored)} of {scored.trades_in} base trades "
        f"(insufficient history {scored.insufficient_history}, "
        f"unpriced chains {scored.unpriced_chains}, "
        f"overlay matched {scored.overlay_matched}); alignment gate passed"
    )


def _run_sleeve(
    result: engine.EngineResult,
    overlays: Sequence[synthetic.LeapOverlay],
    cache: data.PriceCache,
    window: data.Window,
    coverage: data.Coverage,
) -> tuple[
    tuple[sleeve.SleeveResult, ...],
    tuple[metrics.BenchmarkResult, ...],
    tuple[date, ...],
    int,
]:
    """§6.2's sleeves and §6.3's benchmarks, off the base overlay configuration.

    The sleeve runs on `base` alone. §6.2 says "Track A's LEAP-overlay signals"
    and P9 scopes the E₀ sensitivity to "base (m, h) only", so running it across
    the §5.6 grid would produce twelve portfolios the spec never asked for and
    invite a reader to pick the flattering one.
    """
    base = next((item for item in overlays if item.name == "base"), overlays[0])
    trades = base.trades

    # One set of close arrays for both sleeves *and* §6.3.2's benchmark: they all
    # read the same entered chains, and loading ~570 Parquet frames twice is the
    # most expensive thing this function could do by accident.
    closes = data.ChainCloses(cache)
    sleeves = sleeve.simulate_all(trades, cache, window, closes=closes, coverage=coverage)
    unlabelled = _traded_without_sector(trades)

    calendar = engine.session_calendar(
        cache, window, sorted({trade.trade.chain for trade in trades})
    )
    entered = sorted({trade.chain for trade in result.trades["base"]})
    benchmarks = tuple(
        item
        for item in (
            metrics.spy_buy_and_hold(cache, window, coverage=coverage),
            metrics.equal_weight_entered(
                entered, cache, window, calendar, closes=closes, coverage=coverage
            ),
        )
        if item is not None
    )
    return sleeves, benchmarks, tuple(calendar), unlabelled


def _traded_without_sector(trades: Sequence[synthetic.SyntheticTrade]) -> int:
    """How many *traded* names fall into the `Unknown` bucket (bias register 7).

    Counted over the names that actually produced a trade, not over the whole
    membership: the cap can only bind on a name the sleeve was offered, so the
    membership-wide gap would overstate what the reader needs to weigh.
    """
    labels = sleeve.SectorLabels.load()
    symbols = {trade.trade.symbol for trade in trades}
    return sum(1 for symbol in sorted(symbols) if labels.sector_of(symbol) == sleeve.SECTOR_UNKNOWN)


def _sleeve_line(item: sleeve.SleeveResult) -> str:
    warning = (
        f" [LOW COVERAGE {100 * (item.coverage_ratio or 0):.1f}%]"
        if item.low_coverage_warning
        else ""
    )
    cagr = "n/a" if item.cagr is None else f"{100 * item.cagr:+.2f}%"
    drawdown = "n/a" if item.max_drawdown is None else f"{100 * item.max_drawdown:.2f}%"
    return (
        f"sleeve {item.name}: ${item.final_equity:,.0f} final, CAGR {cagr}, "
        f"max DD {drawdown}, {len(item.positions)} positions, "
        f"{len(item.breaker_dates)} breaker trips{warning}"
    )


def _benchmark_line(item: metrics.BenchmarkResult) -> str:
    cagr = "n/a" if item.cagr is None else f"{100 * item.cagr:+.2f}%"
    drawdown = "n/a" if item.max_drawdown is None else f"{100 * item.max_drawdown:.2f}%"
    return f"benchmark {item.name}: CAGR {cagr}, max DD {drawdown}"


def _trades_json(
    result: engine.EngineResult, overlays: Sequence[synthetic.LeapOverlay] = ()
) -> str:
    payload = {
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
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _track_a_line(stats: metrics.TrackAStats) -> str:
    """One line per table. P13: Strict is reported alongside, not as headline."""
    label = "" if stats.variant == "base" else f" [{stats.variant}, not headline]"
    if stats.parameters is not None:
        label = (
            f" [m={stats.parameters['sigma_multiplier']}, h={stats.parameters['friction']}, "
            f"{stats.parameters['zone']} zone"
            f"{'' if stats.variant == 'base' else ', not headline'}]"
        )
    if stats.low_coverage_warning:
        # Acceptance 2: below 85% the warning rides on the table itself, ahead of
        # the numbers, where it cannot be read separately from them.
        label += f" [LOW COVERAGE {100 * (stats.coverage_ratio or 0):.1f}%]"
    if not stats.trades:
        return f"track A {stats.track} {stats.variant}: no trades{label}"

    # A missing benchmark is reported as missing. Printing `+0.00%` for "not
    # computed" would state a measured match with the market that never happened
    # — the same null-vs-zero mistake `indicators._finite` exists to avoid.
    market_delta = stats.market_delta["mean"]
    delta_text = (
        f"{100 * market_delta:+.2f}%"
        if market_delta is not None
        else f"n/a ({stats.market_delta_unpriced} trades unpriced)"
    )
    # §6.1 reports vehicle alpha on the overlay only; on the stock track it is
    # not a zero to print, it is a quantity that does not exist.
    alpha = None if stats.vehicle_alpha is None else stats.vehicle_alpha["mean"]
    alpha_text = "" if alpha is None else f", vehicle alpha {100 * alpha:+.2f}%"
    return (
        f"track A {stats.track} {stats.variant}: "
        f"{stats.trades} trades over {stats.chains} names, "
        f"win rate {100 * stats.win_rate:.1f}%, "
        f"mean {100 * stats.mean_return:+.2f}%, "
        f"median {100 * stats.median_return:+.2f}%, "
        f"market delta {delta_text}{alpha_text}{label}"
    )


def _summary_line(coverage: data.Coverage) -> str:
    return (
        f"coverage {100 * coverage.ratio:.2f}% "
        f"({coverage.covered_member_weeks}/{coverage.total_member_weeks} member-weeks), "
        f"{coverage.members} members, {coverage.no_data_members} with no data, "
        f"status {coverage.status}"
    )


if __name__ == "__main__":
    raise SystemExit(main())
