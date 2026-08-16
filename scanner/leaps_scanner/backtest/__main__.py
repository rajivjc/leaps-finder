"""`python -m leaps_scanner.backtest` — manual, local, never a scheduled job.

B1 scope: resolve the window, build the point-in-time universe, fill the price
cache, and report §2.4 coverage. The signal engine, the option overlay and the
sleeve arrive in B2-B4; until then this command's job is to prove the data under
them is honest.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

from leaps_scanner.backtest import data
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

    # §2.4: below the floor the run is `failed` and commits no report. B1 has no
    # report to withhold yet, so it withholds the exit code instead — B2 onwards
    # must not build on a universe this thin.
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
    return 0


def _summary_line(coverage: data.Coverage) -> str:
    return (
        f"coverage {100 * coverage.ratio:.2f}% "
        f"({coverage.covered_member_weeks}/{coverage.total_member_weeks} member-weeks), "
        f"{coverage.members} members, {coverage.no_data_members} with no data, "
        f"status {coverage.status}"
    )


if __name__ == "__main__":
    raise SystemExit(main())
