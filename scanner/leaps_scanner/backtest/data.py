"""Cached historical data for the backtest (SPEC-BACKTEST.md §2.4, §3).

Extends the live scanner's fetch machinery rather than copying it: `prices.py`
owns the batching, throttling, backoff and empty-response-is-a-failure discipline
SPEC.md §9 pins, and this module supplies the three things ten years of history
need that a weekly scan does not — a start/end date range (no yfinance `period`
string expresses ~11.5 years), dividends fetched inside the same batched calls,
and `Adj Close` kept for §6.3's total-return benchmarks.

Everything downstream computes from the local Parquet cache, never from the
network, so a run is reproducible and the §10.1 determinism property is testable.
"""

from __future__ import annotations

import json
import logging
import random
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from leaps_scanner import indicators, prices
from leaps_scanner.backtest.membership import Membership, MembershipSpan

logger = logging.getLogger(__name__)

# §3.1: ten years of evaluation, warmed up by exactly 550 calendar days so
# SMA200, RV252 and the weekly stochastic are all defined at the first
# evaluation date. Pinned, not approximate — the cache contents feed §10.1.
WINDOW_YEARS = 10
WARMUP_DAYS = 550

# §3.2: raw (dividend-unadjusted, split-adjusted) prices, the live scanner's
# basis, plus Adj Close for total-return benchmarks only.
ADJ_CLOSE = "Adj Close"
PRICE_COLUMNS = [*prices.OHLCV_COLUMNS, ADJ_CLOSE]
DIVIDEND_COLUMN = "Dividends"

RATE_SYMBOL = "^IRX"  # §3.5: 13-week T-bill discount rate, the r input
RATE_CACHE_NAME = "IRX"

# §5.1's RV252 window, in log returns. One more close than returns is needed.
RV_SESSIONS = 252

CACHE_SCHEMA = 1
DEFAULT_CACHE_DIR = Path(__file__).resolve().parents[2] / ".backtest-cache"

# §2.4's floors: below the first the run is `failed` and no report is written;
# between the two every headline table carries a low-coverage warning.
COVERAGE_FAIL_BELOW = 0.80
COVERAGE_WARN_BELOW = 0.85


@dataclass(frozen=True)
class DateRange:
    """A start/end fetch request — the backtest's replacement for `period`."""

    start: date
    end: date  # inclusive; the yfinance call adds the exclusive-end day itself


@dataclass(frozen=True)
class Window:
    """The evaluation window and the fetch range that warms it up (§3.1)."""

    start: date
    end: date
    fetch_start: date

    @property
    def fetch_range(self) -> DateRange:
        return DateRange(start=self.fetch_start, end=self.end)


def last_completed_friday(run_date: date) -> date:
    """The most recent W-FRI week-ending strictly before `run_date`.

    Strictly before, because a Friday run happens while that week is still
    trading — the same no-repaint rule `indicators.completed_weekly_bars`
    applies, expressed on the calendar rather than on fetched bars.
    """
    offset = (run_date.weekday() - 4) % 7
    friday = run_date - timedelta(days=offset or 7)
    return friday


def evaluation_window(run_date: date) -> Window:
    """§3.1's window: ten years back from the last completed week."""
    end = last_completed_friday(run_date)
    try:
        start = end.replace(year=end.year - WINDOW_YEARS)
    except ValueError:  # 29 February: step to the 28th rather than into March
        start = end.replace(year=end.year - WINDOW_YEARS, day=28)
    return Window(start=start, end=end, fetch_start=start - timedelta(days=WARMUP_DAYS))


def week_ending(day: date) -> date:
    """The W-FRI bin a session falls in — the Friday at or after it."""
    return day + timedelta(days=(4 - day.weekday()) % 7)


def week_endings(start: date, end: date) -> tuple[date, ...]:
    """Every W-FRI week-ending in the inclusive range."""
    first = week_ending(start)
    fridays = []
    while first <= end:
        fridays.append(first)
        first += timedelta(days=7)
    return tuple(fridays)


def yahoo_dated_downloader(symbols: Sequence[str], request: prices.Request) -> pd.DataFrame:
    """Batched yfinance download over a date range, with actions.

    `auto_adjust=False` keeps the live scanner's price basis (§3.2/P2);
    `actions=True` brings dividends back in the same call rather than in a
    second per-symbol pass, which at ~900 symbols would be ~900 extra requests.
    """
    import yfinance as yf

    window = request
    assert isinstance(window, DateRange)
    return yf.download(
        tickers=list(symbols),
        # yfinance treats `end` as exclusive; §3.1's window end is inclusive.
        start=window.start.isoformat(),
        end=(window.end + timedelta(days=1)).isoformat(),
        interval="1d",
        group_by="ticker",
        auto_adjust=False,
        actions=True,
        progress=False,
        threads=False,
        session=prices.shared_session(),
    )


def split_dated_frame(frame: pd.DataFrame, symbols: Sequence[str]) -> dict[str, pd.DataFrame]:
    """Cut a batched response into per-symbol frames, keeping Adj Close.

    v1's splitter filters down to OHLCV, which would drop the two columns this
    backtest added the fetch for. Dividends are optional: a symbol that has
    never paid one can come back without the column entirely.
    """
    return prices.split_batch_frame(
        frame, symbols, required=PRICE_COLUMNS, optional=[DIVIDEND_COLUMN]
    )


class PriceCache:
    """Parquet cache of fetched history — git-ignored, never committed (§3.4).

    Each symbol is one Parquet file plus a manifest entry recording the range it
    was fetched over, so a later run can tell "this symbol has no data" from
    "this symbol was never asked for over this range".
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.prices_dir = self.root / "prices"
        self.dividends_dir = self.root / "dividends"
        self.rates_dir = self.root / "rates"
        self.manifest_path = self.root / "manifest.json"

    def ensure_dirs(self) -> None:
        for directory in (self.prices_dir, self.dividends_dir, self.rates_dir):
            directory.mkdir(parents=True, exist_ok=True)

    # -- manifest ---------------------------------------------------------

    def read_manifest(self) -> dict:
        if not self.manifest_path.exists():
            return {"schema": CACHE_SCHEMA, "snapshot_date": None, "symbols": {}, "failed": []}
        with self.manifest_path.open(encoding="utf-8") as handle:
            return json.load(handle)

    def write_manifest(self, manifest: dict) -> None:
        self.ensure_dirs()
        with self.manifest_path.open("w", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2, sort_keys=True)
            handle.write("\n")

    # -- per-symbol data --------------------------------------------------

    def _path(self, directory: Path, symbol: str) -> Path:
        return directory / f"{symbol}.parquet"

    def store(self, symbol: str, frame: pd.DataFrame) -> None:
        """Split one fetched frame into its price and dividend halves."""
        self.ensure_dirs()
        price_columns = [column for column in PRICE_COLUMNS if column in frame.columns]
        frame[price_columns].sort_index().to_parquet(self._path(self.prices_dir, symbol))

        if DIVIDEND_COLUMN in frame.columns:
            paid = frame.loc[frame[DIVIDEND_COLUMN] > 0, [DIVIDEND_COLUMN]].sort_index()
            paid.to_parquet(self._path(self.dividends_dir, symbol))

    def load(self, symbol: str) -> pd.DataFrame | None:
        path = self._path(self.prices_dir, symbol)
        if not path.exists():
            return None
        return pd.read_parquet(path).sort_index()

    def load_dividends(self, symbol: str) -> pd.DataFrame | None:
        path = self._path(self.dividends_dir, symbol)
        if not path.exists():
            return None
        return pd.read_parquet(path).sort_index()

    def store_rates(self, frame: pd.DataFrame) -> None:
        self.ensure_dirs()
        frame.sort_index().to_parquet(self._path(self.rates_dir, RATE_CACHE_NAME))

    def has_rates(self) -> bool:
        return self._path(self.rates_dir, RATE_CACHE_NAME).exists()

    def load_rates(self) -> pd.DataFrame | None:
        path = self._path(self.rates_dir, RATE_CACHE_NAME)
        if not path.exists():
            return None
        return pd.read_parquet(path).sort_index()

    def covers(self, manifest: dict, symbol: str, wanted: DateRange) -> bool:
        """True when the cache already holds `symbol` over the wanted range."""
        entry = manifest.get("symbols", {}).get(symbol)
        if entry is None:
            return False
        return (
            date.fromisoformat(entry["requested_start"]) <= wanted.start
            and date.fromisoformat(entry["requested_end"]) >= wanted.end
        )

    def merge(self, symbol: str, frame: pd.DataFrame) -> None:
        """Append a freshly-fetched tail to whatever is already cached.

        Overlapping sessions resolve to the newer copy, which is what makes it
        safe for a top-up request to start a day early rather than trying to
        land exactly on the boundary.
        """
        existing = self.load(symbol)
        if existing is None:
            self.store(symbol, frame)
            return

        price_columns = [column for column in PRICE_COLUMNS if column in frame.columns]
        combined = pd.concat([existing, frame[price_columns]])
        combined = combined[~combined.index.duplicated(keep="last")].sort_index()
        combined.to_parquet(self._path(self.prices_dir, symbol))

        if DIVIDEND_COLUMN in frame.columns:
            paid = frame.loc[frame[DIVIDEND_COLUMN] > 0, [DIVIDEND_COLUMN]]
            held = self.load_dividends(symbol)
            merged = paid if held is None else pd.concat([held, paid])
            merged = merged[~merged.index.duplicated(keep="last")].sort_index()
            merged.to_parquet(self._path(self.dividends_dir, symbol))


@dataclass(frozen=True)
class FetchPlan:
    """Which symbols need the whole range, which need only the missing tail."""

    full: tuple[str, ...]
    tail: tuple[str, ...]
    tail_range: DateRange | None
    reused: tuple[str, ...]


def plan_fetch(
    manifest: dict,
    symbols: Sequence[str],
    wanted: DateRange,
    *,
    refresh: bool = False,
) -> FetchPlan:
    """Decide what actually has to go over the wire.

    Two rules earn their keep here:

    * **An empty result is not proof of absence.** A symbol recorded with no
      rows is asked again rather than trusted, because yfinance reports rate
      limiting by returning nothing — trusting that would turn a transient
      outage into a permanent hole and let §2.4's coverage ratio blame
      survivorship for an infrastructure failure. The retry is cheap: on a warm
      run those symbols are the only ones left, so they batch together and a
      dead batch costs three requests, not three per symbol.
    * **A stale tail is topped up, not re-fetched.** `wanted.end` advances every
      week, so keying the whole cache on it would repeat the 30-45 minute cold
      fetch for the sake of five new sessions per symbol.
    """
    entries = manifest.get("symbols", {})
    full: list[str] = []
    tail: list[str] = []
    reused: list[str] = []
    cached_ends: list[date] = []

    for symbol in symbols:
        entry = entries.get(symbol) if not refresh else None
        if entry is None or not entry.get("rows"):
            full.append(symbol)
            continue

        start = date.fromisoformat(entry["requested_start"])
        end = date.fromisoformat(entry["requested_end"])
        if start > wanted.start:
            full.append(symbol)  # missing warm-up history, not just a tail
        elif end >= wanted.end:
            reused.append(symbol)
        else:
            tail.append(symbol)
            cached_ends.append(end)

    # One request covers every tail, starting at the earliest session any of
    # them still needs. Re-downloading a few days that some already hold is
    # harmless — `PriceCache.merge` keeps the newer copy.
    tail_range = DateRange(start=min(cached_ends), end=wanted.end) if cached_ends else None

    return FetchPlan(
        full=tuple(full), tail=tuple(tail), tail_range=tail_range, reused=tuple(reused)
    )


@dataclass(frozen=True)
class FetchSummary:
    """What one cache-filling pass did."""

    requested: tuple[str, ...]
    fetched: tuple[str, ...]
    failed: tuple[str, ...]
    reused: tuple[str, ...]
    topped_up: tuple[str, ...]
    rates_ok: bool


def fill_cache(
    symbols: Sequence[str],
    window: Window,
    cache: PriceCache,
    *,
    run_date: date,
    downloader: prices.Downloader = yahoo_dated_downloader,
    batch_size: int = prices.BATCH_SIZE,
    throttle: prices.Throttle | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
    refresh: bool = False,
) -> FetchSummary:
    """Fetch whatever the cache is missing, at v1's throttle and retry discipline.

    Symbols already cached over the wanted range are left alone, and one whose
    cache merely stops short of the window end gets only the missing tail: the
    cold fetch is 30-45 minutes of network time (§3.3), and re-running the
    analysis a week later must not repeat it. `refresh` forces a full re-fetch.
    """
    manifest = cache.read_manifest()
    wanted = window.fetch_range
    ordered = list(dict.fromkeys(symbols))

    plan = plan_fetch(manifest, ordered, wanted, refresh=refresh)
    logger.info(
        "cache: %d reusable, %d to fetch in full, %d needing only a tail from %s",
        len(plan.reused),
        len(plan.full),
        len(plan.tail),
        plan.tail_range.start if plan.tail_range else "-",
    )

    limiter = throttle if throttle is not None else prices.Throttle(sleeper=sleeper)
    entries = dict(manifest.get("symbols", {}))

    def recorder(asked: DateRange, *, topping_up: bool) -> prices.BatchCallback:
        """Persist one batch as it lands.

        The cold fetch runs for half an hour or more; writing only at the end
        would throw all of it away on a Ctrl-C or a dropped connection, and the
        next run would start from nothing.
        """

        def record(fetched: dict[str, pd.DataFrame], batch: Sequence[str]) -> None:
            for symbol in batch:
                frame = fetched.get(symbol)
                if frame is not None:
                    cache.merge(symbol, frame) if topping_up else cache.store(symbol, frame)

                previous = entries.get(symbol, {}) if topping_up else {}
                rows = int(len(frame)) if frame is not None else 0
                entries[symbol] = {
                    "requested_start": min(
                        asked.start,
                        date.fromisoformat(previous["requested_start"])
                        if previous.get("requested_start")
                        else asked.start,
                    ).isoformat(),
                    # Recorded even when the tail came back empty: we asked over
                    # this range and there was nothing more, which is the normal
                    # state of a symbol that stopped trading mid-window. Only a
                    # symbol with *no* rows at all is asked again next run.
                    "requested_end": asked.end.isoformat(),
                    "rows": rows + int(previous.get("rows", 0)),
                    "first_session": previous.get("first_session")
                    or (_iso_index(frame, 0) if frame is not None else None),
                    "last_session": (_iso_index(frame, -1) if frame is not None else None)
                    or previous.get("last_session"),
                }
            cache.write_manifest({**manifest, "schema": CACHE_SCHEMA, "symbols": entries})

        return record

    def run(group: Sequence[str], asked: DateRange, *, topping_up: bool) -> prices.FetchOutcome:
        if not group:
            return prices.FetchOutcome(frames={}, failed=())
        return prices.fetch_batched(
            group,
            request=asked,
            batch_size=batch_size,
            downloader=downloader,
            splitter=split_dated_frame,
            throttle=limiter,
            sleeper=sleeper,
            rng=rng,
            on_batch=recorder(asked, topping_up=topping_up),
        )

    outcome = run(plan.full, wanted, topping_up=False)
    tail_outcome = run(plan.tail, plan.tail_range or wanted, topping_up=True)

    rates_ok = _fill_rates(cache, wanted, downloader, limiter, sleeper, rng, refresh=refresh)

    # A symbol that failed on an earlier pass and succeeded on this one has to
    # drop off the failed list, or the cache would remember a name as dead long
    # after it started returning data.
    recovered = set(outcome.frames) | set(tail_outcome.frames)
    previously_failed = set(manifest.get("failed", [])) - recovered
    manifest = {
        "schema": CACHE_SCHEMA,
        "snapshot_date": run_date.isoformat(),
        "window_start": window.start.isoformat(),
        "window_end": window.end.isoformat(),
        "fetch_start": window.fetch_start.isoformat(),
        "symbols": entries,
        # Only a full-range fetch that returned nothing counts as a failure. A
        # tail that comes back empty means the symbol simply has no new sessions.
        "failed": sorted(previously_failed | set(outcome.failed)),
        "rates_cached": cache.has_rates(),
    }
    cache.write_manifest(manifest)

    return FetchSummary(
        requested=tuple(ordered),
        fetched=tuple(sorted(outcome.frames)),
        failed=tuple(sorted(outcome.failed)),
        reused=plan.reused,
        topped_up=tuple(sorted(tail_outcome.frames)),
        rates_ok=rates_ok,
    )


def _fill_rates(
    cache: PriceCache,
    wanted: DateRange,
    downloader: prices.Downloader,
    throttle: prices.Throttle,
    sleeper: Callable[[float], None],
    rng: random.Random | None,
    *,
    refresh: bool,
) -> bool:
    """Fetch ^IRX through the same batched path (§3.5)."""
    if not refresh and cache.has_rates():
        return True

    outcome = prices.fetch_batched(
        [RATE_SYMBOL],
        request=wanted,
        batch_size=1,
        downloader=downloader,
        splitter=split_dated_frame,
        throttle=throttle,
        sleeper=sleeper,
        rng=rng,
    )
    frame = outcome.frames.get(RATE_SYMBOL)
    if frame is None:
        logger.warning("%s history unavailable; r inputs will be missing", RATE_SYMBOL)
        return False

    cache.store_rates(frame)
    return True


def _iso_index(frame: pd.DataFrame, position: int) -> str | None:
    if frame.empty:
        return None
    return pd.Timestamp(frame.index[position]).date().isoformat()


def first_eligible_date(frame: pd.DataFrame | None) -> date | None:
    """The first session at which every warm-up-hungry input is defined (§3.1).

    §3.1: "A symbol becomes eligible on the first date all three are defined" —
    the SMA200 (200 closes), RV252 (252 log returns, so 253 closes) and the
    weekly slow stochastic (`indicators.MIN_WEEKLY_BARS` completed weekly bars).
    The 550-day warm-up exists to put this date at or before the window start
    for a symbol with continuous history; a symbol listed mid-window becomes
    eligible later, and one with too little history never does.

    Returns None when the frame cannot satisfy all three.
    """
    if frame is None or frame.empty:
        return None

    sessions = pd.DatetimeIndex(frame.sort_index().index)
    if len(sessions) < max(indicators.SMA_SLOW, RV_SESSIONS + 1):
        return None

    sma_ready = sessions[indicators.SMA_SLOW - 1]
    rv_ready = sessions[RV_SESSIONS]  # 253rd close: the 252nd log return

    weekly = indicators.weekly_bars(frame.sort_index())
    if len(weekly) < indicators.MIN_WEEKLY_BARS:
        return None
    stoch_ready = pd.Timestamp(weekly.index[indicators.MIN_WEEKLY_BARS - 1])

    return max(sma_ready, rv_ready, stoch_ready).date()


@dataclass(frozen=True)
class UncoveredSymbol:
    """A member whose price history does not cover all of its member-weeks."""

    symbol: str
    member_weeks: int
    covered_weeks: int
    spans: tuple[MembershipSpan, ...]
    has_data: bool

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "member_weeks": self.member_weeks,
            "covered_weeks": self.covered_weeks,
            "has_data": self.has_data,
            "spans": [
                {
                    "added": span.added.isoformat(),
                    "removed": span.removed.isoformat() if span.removed else None,
                }
                for span in self.spans
            ],
        }


@dataclass(frozen=True)
class Coverage:
    """§2.4's coverage accounting — the honesty gate on the whole backtest."""

    window: Window
    run_date: date
    snapshot_date: str | None
    total_member_weeks: int
    covered_member_weeks: int
    members: int
    no_data_members: int
    uncovered: tuple[UncoveredSymbol, ...] = field(default=())

    @property
    def ratio(self) -> float:
        if not self.total_member_weeks:
            return 0.0
        return self.covered_member_weeks / self.total_member_weeks

    @property
    def status(self) -> str:
        """`failed` below §2.4's floor — partial data must never look complete."""
        return "failed" if self.ratio < COVERAGE_FAIL_BELOW else "ok"

    @property
    def low_coverage_warning(self) -> bool:
        return self.ratio < COVERAGE_WARN_BELOW

    def as_dict(self) -> dict:
        return {
            "run_date": self.run_date.isoformat(),
            "snapshot_date": self.snapshot_date,
            "window": {
                "start": self.window.start.isoformat(),
                "end": self.window.end.isoformat(),
                "fetch_start": self.window.fetch_start.isoformat(),
            },
            "members": self.members,
            "total_member_weeks": self.total_member_weeks,
            "covered_member_weeks": self.covered_member_weeks,
            "coverage_ratio": round(self.ratio, 6),
            "no_data_members": self.no_data_members,
            "status": self.status,
            "low_coverage_warning": self.low_coverage_warning,
            "uncovered": [symbol.as_dict() for symbol in self.uncovered],
        }

    def to_json(self) -> str:
        """Stable serialization — §10.1 wants two runs byte-identical."""
        return json.dumps(self.as_dict(), indent=2, sort_keys=True) + "\n"


def covered_weeks(frame: pd.DataFrame | None) -> frozenset[date]:
    """The W-FRI weeks a cached price frame has at least one session in."""
    if frame is None or frame.empty:
        return frozenset()
    sessions = pd.DatetimeIndex(frame.index)
    return frozenset(week_ending(stamp.date()) for stamp in sessions)


def compute_coverage(
    membership: Membership,
    cache: PriceCache,
    window: Window,
    *,
    run_date: date,
) -> Coverage:
    """Measure how much of the point-in-time universe the cache can actually price.

    Missing names are counted and listed, never dropped: a delisted member with
    no yfinance data is exactly the survivorship gap §2 exists to expose, so it
    has to appear in the denominator and in the uncovered list rather than
    quietly shrinking the universe.
    """
    manifest = cache.read_manifest()
    fridays = week_endings(window.start, window.end)
    member_weeks = membership.member_weeks(fridays)

    total = 0
    covered_total = 0
    no_data = 0
    uncovered: list[UncoveredSymbol] = []

    for symbol, weeks in member_weeks.items():
        frame = cache.load(symbol)
        available = covered_weeks(frame)
        hits = sum(1 for friday in weeks if friday in available)

        total += len(weeks)
        covered_total += hits
        has_data = bool(available)
        if not has_data:
            no_data += 1
        if hits < len(weeks):
            uncovered.append(
                UncoveredSymbol(
                    symbol=symbol,
                    member_weeks=len(weeks),
                    covered_weeks=hits,
                    spans=membership.spans_for(symbol),
                    has_data=has_data,
                )
            )

    uncovered.sort(key=lambda item: (item.covered_weeks - item.member_weeks, item.symbol))

    return Coverage(
        window=window,
        run_date=run_date,
        snapshot_date=manifest.get("snapshot_date"),
        total_member_weeks=total,
        covered_member_weeks=covered_total,
        members=len(member_weeks),
        no_data_members=no_data,
        uncovered=tuple(uncovered),
    )
