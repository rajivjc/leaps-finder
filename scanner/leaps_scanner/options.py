"""LEAP contract selection and economics (SPEC.md §5), plus the IV inputs
for §6: interpolated 30-day ATM IV and 20-day realized volatility (§3.5).

Pure math first, I/O last, as everywhere else in the package: Black-Scholes
delta, expiry choice, contract selection and interpolation are plain functions
over frames and dataclasses; the Yahoo fetchers at the bottom are injected by
the pipeline and never touched by tests.

Option chains are far heavier than price history — one request per expiry per
symbol, with no batch endpoint — so every fetch goes through the shared
throttle and the shared curl_cffi session.
"""

from __future__ import annotations

import logging
import math
import random
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from leaps_scanner.prices import Throttle, retry_fetch, shared_session

logger = logging.getLogger(__name__)

TARGET_DELTA = 0.70
MIN_LEAP_DTE = 350

IV30_TARGET_DTE = 30
RV_WINDOW = 20
TRADING_DAYS_PER_YEAR = 252
DAYS_PER_YEAR = 365.0

RISK_FREE_SYMBOL = "^IRX"

ExpiriesFetcher = Callable[[str], Sequence[date]]
ChainFetcher = Callable[[str, date], "OptionChain"]


@dataclass(frozen=True)
class OptionChain:
    """One expiry's calls and puts, in yfinance's column layout."""

    calls: pd.DataFrame
    puts: pd.DataFrame

    @property
    def is_empty(self) -> bool:
        return self.calls.empty and self.puts.empty


@dataclass(frozen=True)
class ExpiryChoice:
    """The expiry §5.1 settles on, and whether it met the 350-day floor."""

    expiry: date
    dte: int
    meets_min_dte: bool


@dataclass(frozen=True)
class ContractEconomics:
    """§5.4 for the selected contract. All ratios are fractions, not percent."""

    expiry: date
    dte: int
    strike: float
    delta: float
    bid: float
    ask: float
    mid: float
    spread_pct: float
    oi: int
    iv: float
    breakeven: float
    breakeven_pct: float
    cost_pct_spot: float


@dataclass(frozen=True)
class OptionsResult:
    """Everything the options step produced for one symbol.

    `contract` is None when the chain was fetched but no call passed the
    bid/ask filter — a market fact, recorded as nulls. A symbol whose chain
    could not be fetched at all never gets a result (it counts against the
    scan's coverage instead).
    """

    contract: ContractEconomics | None
    iv30: float | None


@dataclass(frozen=True)
class OptionQuery:
    """Per-symbol inputs the options step needs from earlier steps."""

    symbol: str
    spot: float
    dividend_yield: float


@dataclass(frozen=True)
class OptionsOutcome:
    """Per-symbol results plus the symbols whose chains never arrived."""

    results: dict[str, OptionsResult]
    failed: tuple[str, ...]


def norm_cdf(x: float) -> float:
    """Standard normal CDF via erf — no scipy needed at runtime."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_delta(spot: float, strike: float, t_years: float, r: float, q: float, sigma: float) -> float:
    """Black-Scholes call delta, exactly as pinned in SPEC.md §5.2.

    d1 = (ln(S/K) + (r − q + σ²/2)T) / (σ√T),  delta = e^(−qT) · N(d1)
    """
    if spot <= 0 or strike <= 0 or t_years <= 0 or sigma <= 0:
        raise ValueError("spot, strike, T and sigma must all be positive")
    d1 = (math.log(spot / strike) + (r - q + sigma**2 / 2.0) * t_years) / (
        sigma * math.sqrt(t_years)
    )
    return math.exp(-q * t_years) * norm_cdf(d1)


def select_leap_expiry(expiries: Sequence[date], today: date) -> ExpiryChoice | None:
    """§5.1: nearest expiry with DTE ≥ 350; else the longest available, flagged.

    The flag is simply `meets_min_dte` — downstream it shows up as
    `opt_dte < 350`, which is how the spec says to surface it.
    """
    future = sorted(expiry for expiry in expiries if expiry > today)
    if not future:
        return None

    long_enough = [expiry for expiry in future if (expiry - today).days >= MIN_LEAP_DTE]
    expiry = long_enough[0] if long_enough else future[-1]
    dte = (expiry - today).days
    return ExpiryChoice(expiry=expiry, dte=dte, meets_min_dte=dte >= MIN_LEAP_DTE)


def _finite(value) -> float | None:
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def select_contract(
    calls: pd.DataFrame,
    *,
    spot: float,
    expiry: date,
    today: date,
    r: float,
    q: float,
) -> ContractEconomics | None:
    """§5.2-4: among calls with bid > 0 and ask > 0, pick delta nearest 0.70.

    T is calendar days to expiry over 365. A contract without a positive,
    finite impliedVolatility has no computable delta and is skipped rather
    than guessed at. Ties go to the lower strike (first in strike order).
    """
    dte = (expiry - today).days
    t_years = dte / DAYS_PER_YEAR
    if t_years <= 0 or calls.empty:
        return None

    best: ContractEconomics | None = None
    best_distance = math.inf

    for row in calls.sort_values("strike").itertuples():
        strike = _finite(row.strike)
        bid = _finite(row.bid)
        ask = _finite(row.ask)
        sigma = _finite(row.impliedVolatility)
        if strike is None or strike <= 0:
            continue
        if bid is None or ask is None or bid <= 0 or ask <= 0:
            continue
        if sigma is None or sigma <= 0:
            continue

        delta = bs_delta(spot, strike, t_years, r, q, sigma)
        distance = abs(delta - TARGET_DELTA)
        if distance >= best_distance:
            continue

        mid = (bid + ask) / 2.0
        oi = _finite(getattr(row, "openInterest", None))
        best_distance = distance
        best = ContractEconomics(
            expiry=expiry,
            dte=dte,
            strike=strike,
            delta=delta,
            bid=bid,
            ask=ask,
            mid=mid,
            spread_pct=(ask - bid) / mid,
            oi=int(oi) if oi is not None else 0,
            iv=sigma,
            breakeven=strike + mid,
            breakeven_pct=(strike + mid) / spot - 1.0,
            cost_pct_spot=mid / spot,
        )

    return best


def atm_iv(chain: OptionChain, spot: float) -> float | None:
    """ATM IV for one expiry: mean of call and put IV at the strike nearest
    spot (§3.5). One usable side is accepted; none means no reading."""
    readings = []
    for side in (chain.calls, chain.puts):
        if side.empty or "strike" not in side or "impliedVolatility" not in side:
            continue
        usable = side[
            np.isfinite(side["strike"])
            & np.isfinite(side["impliedVolatility"])
            & (side["impliedVolatility"] > 0)
        ]
        if usable.empty:
            continue
        nearest = usable.loc[(usable["strike"] - spot).abs().idxmin()]
        readings.append(float(nearest["impliedVolatility"]))

    return sum(readings) / len(readings) if readings else None


def iv30_expiries(expiries: Sequence[date], today: date) -> list[date]:
    """The one or two expiries bracketing 30 days out, for interpolation."""
    future = sorted(expiry for expiry in expiries if expiry > today)
    below = [e for e in future if (e - today).days <= IV30_TARGET_DTE]
    above = [e for e in future if (e - today).days >= IV30_TARGET_DTE]

    picks = []
    if below:
        picks.append(below[-1])
    if above and above[0] not in picks:
        picks.append(above[0])
    return picks


def interpolate_iv30(terms: Sequence[tuple[int, float]]) -> float | None:
    """Linear interpolation of (dte, atm_iv) readings to 30 days (§3.5).

    With readings straddling 30d the value is interpolated in DTE; with
    readings on one side only, the nearest reading is used as-is.
    """
    clean = sorted((dte, iv) for dte, iv in terms if iv is not None and dte > 0)
    if not clean:
        return None

    below = [t for t in clean if t[0] <= IV30_TARGET_DTE]
    above = [t for t in clean if t[0] >= IV30_TARGET_DTE]
    if below and above:
        d0, v0 = below[-1]
        d1, v1 = above[0]
        if d0 == d1:
            return v0
        return v0 + (IV30_TARGET_DTE - d0) / (d1 - d0) * (v1 - v0)

    nearest = min(clean, key=lambda term: abs(term[0] - IV30_TARGET_DTE))
    return nearest[1]


def realized_vol_20d(close: pd.Series) -> float | None:
    """20-day realized volatility, annualized (§3.5): sample std of the last
    20 daily log returns × √252."""
    returns = np.log(close / close.shift(1)).dropna().tail(RV_WINDOW)
    if len(returns) < RV_WINDOW:
        return None
    return float(returns.std(ddof=1) * math.sqrt(TRADING_DAYS_PER_YEAR))


def evaluate_symbol_options(
    query: OptionQuery,
    *,
    today: date,
    r: float,
    expiries_fetcher: ExpiriesFetcher,
    chain_fetcher: ChainFetcher,
    throttle: Throttle,
    sleeper: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> OptionsResult | None:
    """§3.4-5 for one symbol. None means the fetch itself failed.

    The LEAP chain is load-bearing: if it cannot be fetched, the symbol has no
    options result at all and counts against scan coverage. The 30-day chains
    only feed IV30, so their failure degrades to iv30=None instead.
    """

    def fetch_chain(expiry: date) -> OptionChain | None:
        return retry_fetch(
            lambda: chain_fetcher(query.symbol, expiry),
            describe=f"option chain {query.symbol} {expiry}",
            throttle=throttle,
            sleeper=sleeper,
            rng=rng,
            is_empty=lambda chain: chain.is_empty,
        )

    expiries = retry_fetch(
        lambda: list(expiries_fetcher(query.symbol)),
        describe=f"option expiries {query.symbol}",
        throttle=throttle,
        sleeper=sleeper,
        rng=rng,
        is_empty=lambda listed: len(listed) == 0,
    )
    if expiries is None:
        return None

    contract = None
    choice = select_leap_expiry(expiries, today)
    if choice is not None:
        chain = fetch_chain(choice.expiry)
        if chain is None:
            return None
        contract = select_contract(
            chain.calls,
            spot=query.spot,
            expiry=choice.expiry,
            today=today,
            r=r,
            q=query.dividend_yield,
        )

    terms = []
    for expiry in iv30_expiries(expiries, today):
        chain = fetch_chain(expiry)
        if chain is None:
            continue
        reading = atm_iv(chain, query.spot)
        if reading is not None:
            terms.append(((expiry - today).days, reading))

    return OptionsResult(contract=contract, iv30=interpolate_iv30(terms))


def fetch_options(
    queries: Sequence[OptionQuery],
    *,
    today: date,
    r: float,
    expiries_fetcher: ExpiriesFetcher,
    chain_fetcher: ChainFetcher,
    throttle: Throttle | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> OptionsOutcome:
    """Run the options step for every symbol, one shared throttle throughout."""
    limiter = throttle if throttle is not None else Throttle(sleeper=sleeper)

    results: dict[str, OptionsResult] = {}
    failed: list[str] = []
    for query in queries:
        result = evaluate_symbol_options(
            query,
            today=today,
            r=r,
            expiries_fetcher=expiries_fetcher,
            chain_fetcher=chain_fetcher,
            throttle=limiter,
            sleeper=sleeper,
            rng=rng,
        )
        if result is None:
            failed.append(query.symbol)
        else:
            results[query.symbol] = result

    return OptionsOutcome(results=results, failed=tuple(failed))


def yahoo_expiries(symbol: str) -> list[date]:
    """Default expiries fetcher: yfinance's listed option expiration dates."""
    import yfinance as yf

    ticker = yf.Ticker(symbol, session=shared_session())
    return [date.fromisoformat(text) for text in ticker.options]


def yahoo_chain(symbol: str, expiry: date) -> OptionChain:
    """Default chain fetcher: one expiry's calls and puts."""
    import yfinance as yf

    ticker = yf.Ticker(symbol, session=shared_session())
    chain = ticker.option_chain(expiry.isoformat())
    return OptionChain(calls=chain.calls, puts=chain.puts)


def yahoo_risk_free_rate() -> float | None:
    """§5.2's r: the 13-week T-bill yield. ^IRX quotes the rate ×100."""
    import yfinance as yf

    ticker = yf.Ticker(RISK_FREE_SYMBOL, session=shared_session())
    value = ticker.fast_info["lastPrice"]
    return float(value) / 100.0 if value else None
