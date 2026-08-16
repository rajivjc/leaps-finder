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
from functools import cache

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

# Data-sanity bound for the ATM lookup: a strike this far from spot is not
# "at the money" no matter what the rest of the chain looks like, so a side
# whose nearest usable strike is outside the band contributes no reading.
ATM_MAX_DISTANCE = 0.20

# Strikes are exact quantities (150, 152.5) that survive a round trip through
# Postgres `numeric` and yfinance's float64 columns; the tolerance absorbs
# representation error only, never a neighbouring strike.
STRIKE_TOLERANCE = 1e-6

RISK_FREE_SYMBOL = "^IRX"

ExpiriesFetcher = Callable[[str], Sequence[date]]
ChainFetcher = Callable[[str, date], "OptionChain"]


def _frame_or_empty(frame) -> pd.DataFrame:
    """yfinance returns None (not an empty frame) for a chain side with no
    payload at all; normalize so downstream code never sees None."""
    return frame if frame is not None else pd.DataFrame()


@dataclass(frozen=True)
class OptionChain:
    """One expiry's calls and puts, in yfinance's column layout."""

    calls: pd.DataFrame
    puts: pd.DataFrame

    @property
    def is_empty(self) -> bool:
        # None-safe on both sides: this property is the emptiness probe inside
        # the retry loop, where an AttributeError would abort the whole scan.
        calls, puts = _frame_or_empty(self.calls), _frame_or_empty(self.puts)
        return calls.empty and puts.empty


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
    quote filters — a market fact, recorded as nulls. `chain_failed` marks a
    LEAP chain that could not be fetched at all: the symbol counts against
    scan coverage, but whatever IV30 the near-dated chains yielded is kept so
    the §6 snapshot history keeps accruing. A symbol whose expiries list never
    arrived gets no result at all.
    """

    contract: ContractEconomics | None
    iv30: float | None
    chain_failed: bool = False


@dataclass(frozen=True)
class ContractQuote:
    """A two-sided quote for one already-held contract (SPEC.md §7 premium stop).

    Deliberately thinner than `ContractEconomics`: marking a holding needs a
    price, not a delta or a breakeven, and recomputing those daily would invite
    them to disagree with the entry row that actually justified the trade.
    """

    strike: float
    bid: float
    ask: float
    mid: float


@dataclass(frozen=True)
class QuoteRequest:
    """One holding to price: `key` is the caller's handle (a position id)."""

    key: int
    symbol: str
    expiry: date
    strike: float


@dataclass(frozen=True)
class QuoteOutcome:
    """Marks, and the two distinct ways one can be absent.

    `failed` is a chain that never arrived — a fetch problem, retryable.
    `missing` is a chain that arrived without a usable quote at that strike — a
    market fact. Both leave the premium stop unevaluated, so both are reported
    rather than skipped, but only the first says anything about our plumbing.
    """

    quotes: dict[int, ContractQuote]
    missing: tuple[int, ...]
    failed: tuple[int, ...]


@dataclass(frozen=True)
class OptionQuery:
    """Per-symbol inputs the options step needs from earlier steps."""

    symbol: str
    spot: float
    # Only §5.2's delta needs the carry. `fetch_iv30` reads ATM implied vol
    # straight off the chain and never computes a delta, so the refresh has no
    # dividend yield to supply — hence the default rather than a made-up zero at
    # every call site.
    dividend_yield: float = 0.0


@dataclass(frozen=True)
class OptionsOutcome:
    """Per-symbol results plus the symbols whose chains never arrived."""

    results: dict[str, OptionsResult]
    failed: tuple[str, ...]


@dataclass(frozen=True)
class Iv30Outcome:
    """The refresh's narrower result: IV30 per symbol, plus outright failures.

    A symbol present in `iv30` with a None value had chains that arrived and
    carried no usable ATM reading — a market fact. A symbol in `failed` never
    got a chain list at all, and counts against the run's coverage.
    """

    iv30: dict[str, float | None]
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
    """Coerce an untrusted Yahoo cell to a finite float, or None.

    Junk (strings, None, NaN, infinities) degrades to None rather than
    raising — one bad cell must cost one contract, never the scan.
    """
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
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
    calls = _frame_or_empty(calls)
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
        # A crossed market (ask below bid) is a stale or broken quote; its
        # negative spread would trivially clear every preset gate.
        if ask < bid:
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


def quote_for_strike(calls: pd.DataFrame, strike: float) -> ContractQuote | None:
    """The quote for one exact strike in an expiry's calls (SPEC.md §7).

    §5's selection scans a chain for the contract nearest 0.70 delta; this is
    the opposite lookup — the contract is already chosen, and only its current
    price is in question. It therefore matches the strike exactly (within a
    float tolerance) and never falls back to a neighbour: a mark taken from a
    strike the owner does not hold is not a mark, it is a different trade.

    Unlike `select_contract` this accepts `bid == 0`. There, a zero bid means
    the contract is not enterable and is rightly skipped; here, a bid that has
    collapsed to nothing is precisely the condition the premium stop exists to
    catch, and dropping it would make the stop blindest exactly when it matters.
    The raw bid and ask are carried through to `position_marks` so an alert can
    be read against the quote it came from. A crossed or absent ask is still
    rejected as a broken quote.
    """
    calls = _frame_or_empty(calls)
    if calls.empty or "strike" not in calls:
        return None

    for row in calls.itertuples():
        row_strike = _finite(row.strike)
        if row_strike is None or abs(row_strike - strike) > STRIKE_TOLERANCE:
            continue

        bid = _finite(row.bid)
        ask = _finite(row.ask)
        if bid is None or ask is None or ask <= 0 or bid < 0 or ask < bid:
            continue

        return ContractQuote(strike=row_strike, bid=bid, ask=ask, mid=(bid + ask) / 2.0)

    return None


def fetch_contract_quotes(
    requests: Sequence[QuoteRequest],
    *,
    chain_fetcher: ChainFetcher,
    throttle: Throttle | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> QuoteOutcome:
    """Price every held contract, one chain fetch per distinct (symbol, expiry).

    Two positions in the same contract — a scale-in — cost one request, not two.
    """
    limiter = throttle if throttle is not None else Throttle(sleeper=sleeper)
    chains: dict[tuple[str, date], OptionChain | None] = {}

    quotes: dict[int, ContractQuote] = {}
    missing: list[int] = []
    failed: list[int] = []

    for request in requests:
        cache_key = (request.symbol, request.expiry)
        if cache_key not in chains:
            chains[cache_key] = retry_fetch(
                lambda request=request: chain_fetcher(request.symbol, request.expiry),
                describe=f"held chain {request.symbol} {request.expiry}",
                throttle=limiter,
                sleeper=sleeper,
                rng=rng,
                is_empty=lambda chain: chain.is_empty,
            )

        chain = chains[cache_key]
        if chain is None:
            failed.append(request.key)
            continue

        quote = quote_for_strike(chain.calls, request.strike)
        if quote is None:
            missing.append(request.key)
        else:
            quotes[request.key] = quote

    return QuoteOutcome(quotes=quotes, missing=tuple(missing), failed=tuple(failed))


def atm_iv(chain: OptionChain, spot: float) -> float | None:
    """ATM IV for one expiry: mean of call and put IV at the strike nearest
    spot (§3.5). One usable side is accepted; none means no reading.

    A side whose nearest usable strike sits outside ATM_MAX_DISTANCE of spot
    is skipped — blending a deep-OTM wing quote into an "ATM" reading would
    import skew, not vol level.
    """
    readings = []
    for side in (chain.calls, chain.puts):
        side = _frame_or_empty(side)
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
        if abs(float(nearest["strike"]) / spot - 1.0) > ATM_MAX_DISTANCE:
            continue
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


class _ChainCache:
    """One symbol's chains, fetched at most once per expiry.

    The LEAP expiry can double as the nearest IV30 term on a sparse chain, and
    two positions can share a contract, so the memo is not an optimisation
    detail — it is what keeps the request count honest against the ≤5 req/s
    budget.
    """

    def __init__(
        self,
        symbol: str,
        *,
        chain_fetcher: ChainFetcher,
        throttle: Throttle,
        sleeper: Callable[[float], None],
        rng: random.Random | None,
    ) -> None:
        self._symbol = symbol
        self._chain_fetcher = chain_fetcher
        self._throttle = throttle
        self._sleeper = sleeper
        self._rng = rng
        self._chains: dict[date, OptionChain | None] = {}

    def get(self, expiry: date) -> OptionChain | None:
        if expiry not in self._chains:
            self._chains[expiry] = retry_fetch(
                lambda: self._chain_fetcher(self._symbol, expiry),
                describe=f"option chain {self._symbol} {expiry}",
                throttle=self._throttle,
                sleeper=self._sleeper,
                rng=self._rng,
                is_empty=lambda chain: chain.is_empty,
            )
        return self._chains[expiry]


def _fetch_expiries(
    symbol: str,
    *,
    expiries_fetcher: ExpiriesFetcher,
    throttle: Throttle,
    sleeper: Callable[[float], None],
    rng: random.Random | None,
) -> list[date] | None:
    return retry_fetch(
        lambda: list(expiries_fetcher(symbol)),
        describe=f"option expiries {symbol}",
        throttle=throttle,
        sleeper=sleeper,
        rng=rng,
        is_empty=lambda listed: len(listed) == 0,
    )


def _iv30_from_chains(
    expiries: Sequence[date], *, spot: float, today: date, chains: _ChainCache
) -> float | None:
    """§3.5's IV30 for one symbol: ATM readings on the expiries bracketing 30
    days, interpolated."""
    terms = []
    for expiry in iv30_expiries(expiries, today):
        chain = chains.get(expiry)
        if chain is None:
            continue
        reading = atm_iv(chain, spot)
        if reading is not None:
            terms.append(((expiry - today).days, reading))
    return interpolate_iv30(terms)


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
    """§3.4-5 for one symbol. None means the expiries list never arrived.

    The 30-day chains are fetched first and their IV30 survives a failed LEAP
    fetch (`chain_failed=True`): losing the contract must not also stall the
    symbol's §6 snapshot accrual.
    """
    chains = _ChainCache(
        query.symbol, chain_fetcher=chain_fetcher, throttle=throttle, sleeper=sleeper, rng=rng
    )
    expiries = _fetch_expiries(
        query.symbol, expiries_fetcher=expiries_fetcher, throttle=throttle, sleeper=sleeper, rng=rng
    )
    if expiries is None:
        return None

    iv30 = _iv30_from_chains(expiries, spot=query.spot, today=today, chains=chains)

    contract = None
    choice = select_leap_expiry(expiries, today)
    if choice is not None:
        chain = chains.get(choice.expiry)
        if chain is None:
            return OptionsResult(contract=None, iv30=iv30, chain_failed=True)
        contract = select_contract(
            chain.calls,
            spot=query.spot,
            expiry=choice.expiry,
            today=today,
            r=r,
            q=query.dividend_yield,
        )

    return OptionsResult(contract=contract, iv30=iv30)


def fetch_iv30(
    queries: Sequence[OptionQuery],
    *,
    today: date,
    expiries_fetcher: ExpiriesFetcher,
    chain_fetcher: ChainFetcher,
    throttle: Throttle | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> Iv30Outcome:
    """IV30 only, for the daily refresh (§3's `refresh`: "appends iv_snapshots").

    The refresh does not rescore, so it has no use for the 0.70-delta contract
    and no reason to pay for the LEAP chain that would price it — roughly a
    third of the option requests a full scan makes. What it does need is the
    daily snapshot cadence that §6's IV-rank bootstrap is defined on: 120
    snapshots is six months at five a week, and better than two years at one.
    """
    limiter = throttle if throttle is not None else Throttle(sleeper=sleeper)

    iv30: dict[str, float | None] = {}
    failed: list[str] = []
    for query in queries:
        expiries = _fetch_expiries(
            query.symbol,
            expiries_fetcher=expiries_fetcher,
            throttle=limiter,
            sleeper=sleeper,
            rng=rng,
        )
        if expiries is None:
            failed.append(query.symbol)
            continue

        chains = _ChainCache(
            query.symbol, chain_fetcher=chain_fetcher, throttle=limiter, sleeper=sleeper, rng=rng
        )
        iv30[query.symbol] = _iv30_from_chains(
            expiries, spot=query.spot, today=today, chains=chains
        )

    return Iv30Outcome(iv30=iv30, failed=tuple(failed))


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
        if result is None or result.chain_failed:
            failed.append(query.symbol)
        if result is not None:
            results[query.symbol] = result

    return OptionsOutcome(results=results, failed=tuple(failed))


@cache
def _yahoo_ticker(symbol: str):
    """One Ticker per symbol for the whole run.

    A fresh Ticker calling `option_chain(date)` first downloads the default
    chain just to learn the expiration map — a hidden second HTTP request per
    throttled fetch. Reusing the instance whose `.options` call already
    populated that map keeps each chain fetch to a single request, and pins
    the expiry list the pipeline chose from to the map the chain fetch
    validates against (no mid-run drift `ValueError`s).
    """
    import yfinance as yf

    return yf.Ticker(symbol, session=shared_session())


def yahoo_expiries(symbol: str) -> list[date]:
    """Default expiries fetcher: yfinance's listed option expiration dates."""
    return [date.fromisoformat(text) for text in _yahoo_ticker(symbol).options]


def yahoo_chain(symbol: str, expiry: date) -> OptionChain:
    """Default chain fetcher: one expiry's calls and puts."""
    chain = _yahoo_ticker(symbol).option_chain(expiry.isoformat())
    # yfinance returns None sides (not empty frames) when the payload is
    # absent; normalize at the boundary so nothing downstream sees None.
    return OptionChain(calls=_frame_or_empty(chain.calls), puts=_frame_or_empty(chain.puts))


def yahoo_risk_free_rate() -> float | None:
    """§5.2's r: the 13-week T-bill yield. ^IRX quotes the rate ×100.

    Zero is a valid rate (bills printed 0.00 through 2020-21); only a missing
    quote is None.
    """
    import yfinance as yf

    ticker = yf.Ticker(RISK_FREE_SYMBOL, session=shared_session())
    value = ticker.fast_info["lastPrice"]
    return float(value) / 100.0 if value is not None else None
