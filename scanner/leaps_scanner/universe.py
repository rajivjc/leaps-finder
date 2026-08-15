"""Universe construction (SPEC.md §3.1).

Starts from the S&P 500 constituent list bundled with the package — checked in
so a scan never depends on a third-party list being reachable — then keeps the
names with a market cap of at least $50B.
"""

from __future__ import annotations

import csv
import logging
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from importlib import resources

from leaps_scanner.prices import Throttle

logger = logging.getLogger(__name__)

MIN_MARKET_CAP = 50e9
SEED_FILE = "sp500_seed.csv"

MarketCapFetcher = Callable[[str], float | None]


@dataclass(frozen=True)
class SeedEntry:
    """One row of the bundled seed list."""

    symbol: str
    name: str
    sector: str
    industry: str


@dataclass(frozen=True)
class UniverseEntry:
    """A seed name that cleared the market-cap floor."""

    symbol: str
    name: str
    sector: str
    industry: str
    market_cap: float


def load_seed() -> list[SeedEntry]:
    """Read the bundled constituent list.

    Symbols are stored in Yahoo's convention (BRK-B, not BRK.B).
    """
    source = resources.files("leaps_scanner.data").joinpath(SEED_FILE)
    with resources.as_file(source) as path, path.open(newline="", encoding="utf-8") as handle:
        return [
            SeedEntry(
                symbol=row["symbol"].strip(),
                name=row["name"].strip(),
                sector=row["sector"].strip(),
                industry=row["industry"].strip(),
            )
            for row in csv.DictReader(handle)
            if row.get("symbol", "").strip()
        ]


def yahoo_market_cap(symbol: str) -> float | None:
    """Default per-symbol market-cap lookup via yfinance's lightweight quote."""
    import curl_cffi
    import yfinance as yf

    session = curl_cffi.requests.Session(impersonate="chrome")
    value = yf.Ticker(symbol, session=session).fast_info["marketCap"]
    return float(value) if value else None


def fetch_market_caps(
    symbols: Iterable[str],
    *,
    fetcher: MarketCapFetcher = yahoo_market_cap,
    throttle: Throttle | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, float]:
    """Look up market cap per symbol, throttled.

    There is no batched endpoint for this, so it is one request each. A symbol
    that errors or returns nothing is left out; it then fails the cap filter,
    which is the conservative outcome — an unpriceable name is not screened.
    """
    limiter = throttle if throttle is not None else Throttle(sleeper=sleeper)

    caps: dict[str, float] = {}
    for symbol in symbols:
        limiter.wait()
        try:
            value = fetcher(symbol)
        except Exception as exc:  # noqa: BLE001 - one bad symbol must not stop the scan
            logger.warning("market cap lookup failed for %s: %s", symbol, exc)
            continue

        if value is not None and value > 0:
            caps[symbol] = float(value)

    return caps


def select_universe(
    seed: Sequence[SeedEntry],
    market_caps: dict[str, float],
    min_market_cap: float = MIN_MARKET_CAP,
) -> list[UniverseEntry]:
    """Keep seed names at or above the cap floor, largest first."""
    selected = [
        UniverseEntry(
            symbol=entry.symbol,
            name=entry.name,
            sector=entry.sector,
            industry=entry.industry,
            market_cap=market_caps[entry.symbol],
        )
        for entry in seed
        if market_caps.get(entry.symbol, 0.0) >= min_market_cap
    ]
    selected.sort(key=lambda entry: entry.market_cap, reverse=True)
    return selected
