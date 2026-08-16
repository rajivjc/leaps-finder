"""`python -m leaps_scanner.backtest` — manual, local, never a scheduled job.

Through B2: resolve the window, build the point-in-time universe, fill the price
cache, report §2.4 coverage, then replay §4 over the universe and print §6.1's
Track A tables for the stock track. The option overlay and the sleeve arrive in
B3-B4, and so does the committed report — §8 puts results under
`docs/backtest/<run-date>/`, which is B4's deliverable, so this command writes
nothing unless asked to.

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
from datetime import date
from pathlib import Path

from leaps_scanner.backtest import data, engine, metrics
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
    report = coverage.to_json()
    if args.coverage_json:
        args.coverage_json.write_text(report, encoding="utf-8")
    print(report, end="")

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

    _run_engine(membership, cache, window, coverage, args)
    return 0


def _run_engine(
    membership: Membership,
    cache: data.PriceCache,
    window: data.Window,
    coverage: data.Coverage,
    args: argparse.Namespace,
) -> None:
    """§4's replay and §6.1's Track A tables, for the stock track.

    `coverage` is passed into the statistics rather than merely logged above:
    acceptance 2 wants the low-coverage warning on every headline table, and
    these are headline tables.
    """
    result = engine.run(membership, cache, window)
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

    if args.track_a_json:
        args.track_a_json.write_text(metrics.track_a_json(stats), encoding="utf-8")
    if args.trades_json:
        args.trades_json.write_text(_trades_json(result), encoding="utf-8")

    for item in stats:
        print(_track_a_line(item), file=sys.stderr)


def _trades_json(result: engine.EngineResult) -> str:
    payload = {
        "trades": {
            name: [trade.as_dict() for trade in rows] for name, rows in result.trades.items()
        },
        "skipped": {
            name: [entry.as_dict() for entry in rows] for name, rows in result.skipped.items()
        },
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _track_a_line(stats: metrics.TrackAStats) -> str:
    """One line per variant. P13: Strict is reported alongside, not as headline."""
    label = "" if stats.variant == "base" else f" [{stats.variant} zone variant, not headline]"
    if stats.low_coverage_warning:
        # Acceptance 2: below 85% the warning rides on the table itself, ahead of
        # the numbers, where it cannot be read separately from them.
        label += f" [LOW COVERAGE {100 * (stats.coverage_ratio or 0):.1f}%]"
    if not stats.trades:
        return f"track A {stats.variant}: no trades{label}"

    # A missing benchmark is reported as missing. Printing `+0.00%` for "not
    # computed" would state a measured match with the market that never happened
    # — the same null-vs-zero mistake `indicators._finite` exists to avoid.
    market_delta = stats.market_delta["mean"]
    delta_text = (
        f"{100 * market_delta:+.2f}%"
        if market_delta is not None
        else f"n/a ({stats.market_delta_unpriced} trades unpriced)"
    )
    return (
        f"track A {stats.variant}: {stats.trades} trades over {stats.chains} names, "
        f"win rate {100 * stats.win_rate:.1f}%, "
        f"mean {100 * stats.mean_return:+.2f}%, "
        f"median {100 * stats.median_return:+.2f}%, "
        f"market delta {delta_text}{label}"
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
