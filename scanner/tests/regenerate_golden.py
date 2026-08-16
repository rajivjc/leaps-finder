"""Regenerate the golden files under tests/golden/.

Run deliberately, from scanner/:

    .venv/bin/python tests/regenerate_golden.py

then hand-verify the diff before committing — the golden file is the arbiter,
so a regenerated value is only correct if it can be defended by hand.
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from test_backtest_engine import build_fixture, golden_payload  # noqa: E402
from test_run_scan import GOLDEN_DIR, TestGoldenRow  # noqa: E402


def write(name: str, payload: object) -> None:
    GOLDEN_DIR.mkdir(exist_ok=True)
    target = GOLDEN_DIR / name
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(f"wrote {target}")


def main() -> None:
    write("scan_row.json", TestGoldenRow().scan_row())

    # SPEC-BACKTEST.md §9's integration golden: the engine over the recorded
    # fixture universe. Built in a scratch directory because the fixture's price
    # cache is derived, not committed (§3.4 — no raw price data in the repo).
    with tempfile.TemporaryDirectory() as scratch:
        membership, cache = build_fixture(Path(scratch))
        write("backtest_trades.json", golden_payload(membership, cache))


if __name__ == "__main__":
    main()
