"""Fundamentals (SPEC.md §3.3): the Quality and Valuation inputs.

Every metric is optional. Yahoo's `info` payload is best-effort, and §6's rule
is that a missing metric is excluded from the Quality mean and flagged — never
substituted with an invented zero. The derivations that need judgment are
pinned here:

* net debt / EBITDA needs total debt and a positive EBITDA; missing cash is
  treated as zero (overstating net debt — the conservative direction).
* forward P/E ≤ 0 means negative forward earnings, which is not "cheap"; it is
  treated as missing rather than winning the cheapness percentile.
* the dividend yield is Yahoo's *trailing* annual yield (a fraction), which is
  exactly the q that §5.2's delta calls for; absent means 0.
"""

from __future__ import annotations

import logging
import math
import random
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime

from leaps_scanner.prices import Throttle, retry_fetch, shared_session

logger = logging.getLogger(__name__)

FundamentalsFetcher = Callable[[str], "Fundamentals"]

METRIC_FIELDS = (
    "op_margin",
    "roe",
    "net_debt_ebitda",
    "rev_growth",
    "fcf_margin",
    "fwd_pe",
    "analyst_target",
)


@dataclass(frozen=True)
class Fundamentals:
    """One symbol's §3.3 snapshot. None means Yahoo did not provide it."""

    op_margin: float | None = None
    roe: float | None = None
    net_debt_ebitda: float | None = None
    rev_growth: float | None = None
    fcf_margin: float | None = None
    fwd_pe: float | None = None
    analyst_target: float | None = None
    dividend_yield: float = 0.0
    next_earnings: date | None = None

    @property
    def is_empty(self) -> bool:
        return all(getattr(self, field) is None for field in METRIC_FIELDS)


EMPTY = Fundamentals()


@dataclass(frozen=True)
class FundamentalsOutcome:
    """Per-symbol snapshots plus the symbols whose fetch failed outright.

    A failed symbol still maps to an empty snapshot in `results` so the
    pipeline can index it, but it is named in `failed` — the scan counts
    those toward its incomplete-data ceiling instead of letting a wholesale
    Yahoo outage masquerade as a universe of companies with no financials.
    """

    results: dict[str, Fundamentals]
    failed: tuple[str, ...]


def _number(info: Mapping, key: str) -> float | None:
    value = info.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def from_info(info: Mapping, next_earnings: date | None = None) -> Fundamentals:
    """Derive the §3.3 metrics from a yfinance `info` mapping. Pure."""
    total_debt = _number(info, "totalDebt")
    total_cash = _number(info, "totalCash")
    ebitda = _number(info, "ebitda")
    net_debt_ebitda = None
    if total_debt is not None and ebitda is not None and ebitda > 0:
        net_debt_ebitda = (total_debt - (total_cash or 0.0)) / ebitda

    free_cash_flow = _number(info, "freeCashflow")
    revenue = _number(info, "totalRevenue")
    fcf_margin = None
    if free_cash_flow is not None and revenue is not None and revenue > 0:
        fcf_margin = free_cash_flow / revenue

    fwd_pe = _number(info, "forwardPE")
    if fwd_pe is not None and fwd_pe <= 0:
        fwd_pe = None

    analyst_target = _number(info, "targetMeanPrice")
    if analyst_target is not None and analyst_target <= 0:
        analyst_target = None

    return Fundamentals(
        op_margin=_number(info, "operatingMargins"),
        roe=_number(info, "returnOnEquity"),
        net_debt_ebitda=net_debt_ebitda,
        rev_growth=_number(info, "revenueGrowth"),
        fcf_margin=fcf_margin,
        fwd_pe=fwd_pe,
        analyst_target=analyst_target,
        dividend_yield=_number(info, "trailingAnnualDividendYield") or 0.0,
        next_earnings=next_earnings,
    )


def _next_earnings(ticker) -> date | None:
    """Earliest scheduled earnings date from yfinance's calendar, if any."""
    try:
        calendar = ticker.calendar or {}
        dates = calendar.get("Earnings Date") or []
    except Exception:  # noqa: BLE001 - the calendar endpoint is best-effort
        return None

    parsed = []
    for value in dates:
        if isinstance(value, datetime):
            parsed.append(value.date())
        elif isinstance(value, date):
            parsed.append(value)
    return min(parsed) if parsed else None


def yahoo_fundamentals(symbol: str) -> Fundamentals:
    """Default fetcher: one `info` payload plus the earnings calendar."""
    import yfinance as yf

    ticker = yf.Ticker(symbol, session=shared_session())
    info = ticker.info or {}
    return from_info(info, _next_earnings(ticker))


def fetch_fundamentals(
    symbols: Iterable[str],
    *,
    fetcher: FundamentalsFetcher = yahoo_fundamentals,
    throttle: Throttle | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> FundamentalsOutcome:
    """Fetch fundamentals per symbol, throttled, retrying empty payloads.

    A symbol that still has nothing after the retries keeps an empty snapshot
    (its metrics are simply missing, which §6 knows how to handle) *and* is
    reported in `failed`: an all-None payload after three retries is a fetch
    failure, not a company without financials, and the scan must count it as
    incomplete data rather than quietly scoring on nulls.
    """
    limiter = throttle if throttle is not None else Throttle(sleeper=sleeper)

    results: dict[str, Fundamentals] = {}
    failed: list[str] = []
    for symbol in symbols:
        fetched = retry_fetch(
            lambda symbol=symbol: fetcher(symbol),
            describe=f"fundamentals {symbol}",
            throttle=limiter,
            sleeper=sleeper,
            rng=rng,
            is_empty=lambda snapshot: snapshot.is_empty,
        )
        if fetched is None:
            failed.append(symbol)
        results[symbol] = fetched if fetched is not None else EMPTY

    return FundamentalsOutcome(results=results, failed=tuple(failed))
