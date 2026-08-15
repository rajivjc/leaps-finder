"""Regenerate the golden scan_results row (tests/golden/scan_row.json).

Run deliberately, from scanner/:

    .venv/bin/python tests/regenerate_golden.py

then hand-verify the diff before committing — the golden file is the arbiter,
so a regenerated value is only correct if it can be defended by hand.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from test_run_scan import GOLDEN_DIR, TestGoldenRow  # noqa: E402


def main() -> None:
    row = TestGoldenRow().scan_row()
    GOLDEN_DIR.mkdir(exist_ok=True)
    target = GOLDEN_DIR / "scan_row.json"
    target.write_text(json.dumps(row, indent=2, sort_keys=True) + "\n")
    print(f"wrote {target}")


if __name__ == "__main__":
    main()
