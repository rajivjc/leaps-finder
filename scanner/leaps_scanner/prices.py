"""Daily OHLCV fetching (SPEC.md §3.2, §9).

Yahoo is free and unofficial, so it is treated as unreliable by construction:
requests go out in batches, throttled to at most five per second, and each batch
gets three retries with exponential backoff and jitter. Whatever is still
missing at the end stays missing — the caller counts the gaps and fails the scan
if too much of the universe dropped out.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from functools import lru_cache

import pandas as pd

logger = logging.getLogger(__name__)

DEFAULT_PERIOD = "2y"
BATCH_SIZE = 25
MAX_REQUESTS_PER_SECOND = 5.0
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_JITTER_SECONDS = 0.5

OHLCV_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]

# A batch download is described by a request object the batching machinery never
# inspects — a `period` string for the live scanner, a start/end date range for
# the backtest (SPEC-BACKTEST.md §3.3, which no `period` string can express).
Request = object
Downloader = Callable[[Sequence[str], Request], pd.DataFrame]
Splitter = Callable[[pd.DataFrame, Sequence[str]], dict[str, pd.DataFrame]]


class Throttle:
    """Spaces calls at least `1 / rate` seconds apart."""

    def __init__(
        self,
        rate: float = MAX_REQUESTS_PER_SECOND,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        self._min_interval = 1.0 / rate
        self._sleep = sleeper
        self._clock = clock
        self._last_call: float | None = None

    def wait(self) -> None:
        now = self._clock()
        if self._last_call is not None:
            remaining = self._min_interval - (now - self._last_call)
            if remaining > 0:
                self._sleep(remaining)
                now = self._clock()
        self._last_call = now


@dataclass(frozen=True)
class FetchOutcome:
    """Fetched history plus the symbols that never arrived."""

    frames: dict[str, pd.DataFrame]
    failed: tuple[str, ...]

    @property
    def failure_rate(self) -> float:
        total = len(self.frames) + len(self.failed)
        return len(self.failed) / total if total else 0.0


def batched(items: Sequence[str], size: int = BATCH_SIZE) -> list[list[str]]:
    if size < 1:
        raise ValueError("batch size must be at least 1")
    return [list(items[i : i + size]) for i in range(0, len(items), size)]


@lru_cache(maxsize=1)
def shared_session():
    """One curl_cffi session for the whole run.

    A fresh session per request means a fresh TLS handshake per request, which
    is both slow and a good way to look like something worth rate-limiting.
    """
    import curl_cffi

    return curl_cffi.requests.Session(impersonate="chrome")


def yahoo_downloader(symbols: Sequence[str], period: Request) -> pd.DataFrame:
    """Default downloader: one batched yfinance request over a curl_cffi session."""
    import yfinance as yf

    session = shared_session()
    return yf.download(
        tickers=list(symbols),
        period=period,  # type: ignore[arg-type]  # v1 always passes a period string
        interval="1d",
        group_by="ticker",
        auto_adjust=False,
        actions=False,
        progress=False,
        threads=False,
        session=session,
    )


def split_batch_frame(
    frame: pd.DataFrame,
    symbols: Sequence[str],
    *,
    required: Sequence[str] = OHLCV_COLUMNS,
    optional: Sequence[str] = (),
) -> dict[str, pd.DataFrame]:
    """Split a batched download into one clean frame per symbol.

    yfinance returns MultiIndex columns for several symbols and flat columns for
    one, so both shapes are handled. Symbols with no usable rows are omitted
    rather than returned as empty frames.

    A symbol missing any `required` column is dropped — a frame without the
    columns the caller asked for is not a usable frame. `optional` columns are
    kept when present and simply absent otherwise, which is how the backtest
    treats `Dividends`: a symbol that never paid one may come back without the
    column at all (SPEC-BACKTEST.md §3.3).
    """
    frames: dict[str, pd.DataFrame] = {}
    if frame is None or frame.empty:
        return frames

    for symbol in symbols:
        if isinstance(frame.columns, pd.MultiIndex):
            if symbol not in frame.columns.get_level_values(0):
                continue
            per_symbol = frame[symbol]
        else:
            per_symbol = frame

        if not set(required).issubset(per_symbol.columns):
            continue

        keep = list(required) + [column for column in optional if column in per_symbol.columns]
        cleaned = per_symbol[keep].dropna(subset=["Close"]).sort_index()
        if not cleaned.empty:
            frames[symbol] = cleaned

    return frames


def fetch_batched(
    symbols: Iterable[str],
    *,
    request: Request,
    batch_size: int = BATCH_SIZE,
    downloader: Downloader,
    splitter: Splitter = split_batch_frame,
    throttle: Throttle | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
    max_retries: int = MAX_RETRIES,
    on_batch: Callable[[dict[str, pd.DataFrame], Sequence[str]], None] | None = None,
) -> FetchOutcome:
    """Fetch history for every symbol, batched and throttled.

    The batching, throttling and retry discipline SPEC.md §9 pins lives here and
    nowhere else; what varies between callers is what a batch *asks for*
    (`request`, handed to the downloader untouched) and how the response is cut
    into per-symbol frames (`splitter`). The backtest's dated, dividend-carrying
    fetch is a different request and splitter over this same loop — not a second
    copy of it.

    `on_batch` is called with each batch's frames as soon as they arrive, so a
    long fetch can persist progress rather than holding everything in memory
    until the end and losing it all to an interruption.

    A batch that fails every attempt contributes its symbols to `failed`; one
    bad batch never aborts the run, because a scan that covers most of the
    universe is still worth recording (as long as §9's 20% ceiling holds).
    """
    ordered = list(dict.fromkeys(symbols))  # de-duplicate, keep order
    limiter = throttle if throttle is not None else Throttle(sleeper=sleeper)
    jitter = rng if rng is not None else random.Random()

    frames: dict[str, pd.DataFrame] = {}
    failed: list[str] = []

    for batch in batched(ordered, batch_size):
        fetched = _fetch_batch_with_retries(
            batch,
            request=request,
            downloader=downloader,
            splitter=splitter,
            throttle=limiter,
            sleeper=sleeper,
            rng=jitter,
            max_retries=max_retries,
        )
        frames.update(fetched)
        failed.extend(symbol for symbol in batch if symbol not in fetched)
        if on_batch is not None:
            on_batch(fetched, batch)

    return FetchOutcome(frames=frames, failed=tuple(failed))


def fetch_daily_ohlcv(
    symbols: Iterable[str],
    *,
    period: str = DEFAULT_PERIOD,
    batch_size: int = BATCH_SIZE,
    downloader: Downloader = yahoo_downloader,
    throttle: Throttle | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
    max_retries: int = MAX_RETRIES,
) -> FetchOutcome:
    """Fetch daily OHLCV over a `period` string — the live scanner's fetch."""
    return fetch_batched(
        symbols,
        request=period,
        batch_size=batch_size,
        downloader=downloader,
        throttle=throttle,
        sleeper=sleeper,
        rng=rng,
        max_retries=max_retries,
    )


def retry_fetch(
    fetch: Callable[[], object],
    *,
    describe: str,
    throttle: Throttle,
    sleeper: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
    max_retries: int = MAX_RETRIES,
    is_empty: Callable[[object], bool] | None = None,
):
    """Run one throttled fetch with the standard backoff, returning None on defeat.

    The generic sibling of `_fetch_batch_with_retries` for single-object
    lookups (option chains, fundamentals, rates). The same yfinance quirk
    applies everywhere: failure usually arrives as an empty result rather than
    an exception, so `is_empty` results are retried exactly like errors.
    """
    jitter = rng if rng is not None else random.Random()
    empty = is_empty if is_empty is not None else (lambda result: result is None)

    for attempt in range(max_retries):
        throttle.wait()
        try:
            # The emptiness probe stays inside the try: a malformed result
            # that makes it raise is exactly as retryable as a transport
            # error, and must never escape to abort the caller's whole run.
            result = fetch()
            usable = result is not None and not empty(result)
        except Exception as exc:  # noqa: BLE001 - any transport error is retryable
            reason = f"{type(exc).__name__}: {exc}"
        else:
            if usable:
                return result
            reason = "empty response"

        logger.warning(
            "%s yielded nothing (attempt %d/%d): %s", describe, attempt + 1, max_retries, reason
        )
        if attempt < max_retries - 1:
            sleeper(BACKOFF_BASE_SECONDS * (2**attempt) + jitter.uniform(0, BACKOFF_JITTER_SECONDS))

    return None


def _fetch_batch_with_retries(
    batch: Sequence[str],
    *,
    request: Request,
    downloader: Downloader,
    splitter: Splitter,
    throttle: Throttle,
    sleeper: Callable[[float], None],
    rng: random.Random,
    max_retries: int,
) -> dict[str, pd.DataFrame]:
    """Download one batch, retrying until it yields something usable.

    yfinance reports most failures — rate limiting included — by returning an
    empty frame rather than raising, so retrying only on exceptions would leave
    the common case unretried. A batch that comes back with nothing usable is
    therefore treated exactly like a transport error. A batch that returns
    *some* symbols is accepted as-is: the absent ones are far more likely to be
    delisted than throttled, and retrying the whole batch to chase them would
    cost more requests than it saves.
    """
    for attempt in range(max_retries):
        throttle.wait()
        try:
            raw = downloader(batch, request)
        except Exception as exc:  # noqa: BLE001 - any transport error is retryable
            reason = f"{type(exc).__name__}: {exc}"
        else:
            fetched = splitter(raw, batch)
            if fetched:
                return fetched
            reason = "empty response"

        logger.warning(
            "batch of %d yielded nothing (attempt %d/%d): %s",
            len(batch),
            attempt + 1,
            max_retries,
            reason,
        )
        if attempt == max_retries - 1:
            return {}
        sleeper(BACKOFF_BASE_SECONDS * (2**attempt) + rng.uniform(0, BACKOFF_JITTER_SECONDS))

    return {}
