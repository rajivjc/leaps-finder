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

import pandas as pd

logger = logging.getLogger(__name__)

DEFAULT_PERIOD = "2y"
BATCH_SIZE = 25
MAX_REQUESTS_PER_SECOND = 5.0
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_JITTER_SECONDS = 0.5

OHLCV_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]

Downloader = Callable[[Sequence[str], str], pd.DataFrame]


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


def yahoo_downloader(symbols: Sequence[str], period: str) -> pd.DataFrame:
    """Default downloader: one batched yfinance request over a curl_cffi session."""
    import curl_cffi
    import yfinance as yf

    session = curl_cffi.requests.Session(impersonate="chrome")
    return yf.download(
        tickers=list(symbols),
        period=period,
        interval="1d",
        group_by="ticker",
        auto_adjust=False,
        actions=False,
        progress=False,
        threads=False,
        session=session,
    )


def split_batch_frame(frame: pd.DataFrame, symbols: Sequence[str]) -> dict[str, pd.DataFrame]:
    """Split a batched download into one clean OHLCV frame per symbol.

    yfinance returns MultiIndex columns for several symbols and flat columns for
    one, so both shapes are handled. Symbols with no usable rows are omitted
    rather than returned as empty frames.
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

        if not set(OHLCV_COLUMNS).issubset(per_symbol.columns):
            continue

        cleaned = per_symbol[OHLCV_COLUMNS].dropna(subset=["Close"]).sort_index()
        if not cleaned.empty:
            frames[symbol] = cleaned

    return frames


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
    """Fetch daily history for every symbol, batched and throttled.

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
        raw = _download_with_retries(
            batch,
            period=period,
            downloader=downloader,
            throttle=limiter,
            sleeper=sleeper,
            rng=jitter,
            max_retries=max_retries,
        )
        if raw is None:
            failed.extend(batch)
            continue

        fetched = split_batch_frame(raw, batch)
        frames.update(fetched)
        failed.extend(symbol for symbol in batch if symbol not in fetched)

    return FetchOutcome(frames=frames, failed=tuple(failed))


def _download_with_retries(
    batch: Sequence[str],
    *,
    period: str,
    downloader: Downloader,
    throttle: Throttle,
    sleeper: Callable[[float], None],
    rng: random.Random,
    max_retries: int,
) -> pd.DataFrame | None:
    for attempt in range(max_retries):
        throttle.wait()
        try:
            return downloader(batch, period)
        except Exception as exc:  # noqa: BLE001 - any transport error is retryable
            last_attempt = attempt == max_retries - 1
            logger.warning(
                "batch of %d failed (attempt %d/%d): %s",
                len(batch),
                attempt + 1,
                max_retries,
                exc,
            )
            if last_attempt:
                return None
            sleeper(BACKOFF_BASE_SECONDS * (2**attempt) + rng.uniform(0, BACKOFF_JITTER_SECONDS))

    return None
