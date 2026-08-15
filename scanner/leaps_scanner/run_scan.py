"""Scanner entrypoints.

    python -m leaps_scanner.run_scan full      # SPEC.md §3, weekly (Sat 02:00 UTC)
    python -m leaps_scanner.run_scan refresh   # SPEC.md §3, daily (Tue-Sat 22:30 UTC)

Both commands exit non-zero on failure so a red Actions run is impossible to miss
(SPEC.md §3.8). The pipeline itself lands in M2 (prices/indicators) and M3
(options/scoring); this module currently just wires the CLI and config.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from leaps_scanner.config import ConfigError, load_settings

COMMANDS = ("full", "refresh")


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
    """Weekly scan: universe, signals, fundamentals, options, scoring, exits."""
    raise NotImplementedError("full_scan lands in M2 (universe/prices) and M3 (options/scoring)")


def daily_refresh() -> int:
    """Daily refresh: quotes, IV snapshot, daily exit rules. No rescoring."""
    raise NotImplementedError("daily_refresh lands in M5 (risk engine + daily job)")


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        load_settings()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    return full_scan() if args.command == "full" else daily_refresh()


if __name__ == "__main__":  # pragma: no cover - exercised via the CLI
    raise SystemExit(main())
