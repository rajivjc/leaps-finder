"""Track A statistics for the stock track (SPEC-BACKTEST.md §6.1).

Per-trade, not per-portfolio: every §4.2 signal counted independently, which is
the primary result §6 asks for. The sleeve's portfolio arithmetic is §6.2 and
belongs to B4, and §6.3's buy-and-hold benchmarks are reported beside it there.
What lands here is what §6.1 pins — the distribution of trade outcomes, and the
one per-trade benchmark the stock track can carry.

§6.1 asks for **no** vehicle alpha on this track, and there is none to compute:
`r_trade` *is* the same-window hold, so the difference would be zero for every
trade by construction. Reporting a column of zeros would look like a measurement.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from leaps_scanner.backtest.engine import SkippedEntry, Trade

RETURN_PERCENTILES = (5, 25, 50, 75, 95)


def _summary(values: np.ndarray) -> dict[str, float | None]:
    if values.size == 0:
        return {"mean": None, "median": None, "min": None, "max": None}
    return {
        "mean": float(np.mean(values)),
        "median": float(np.median(values)),
        "min": float(np.min(values)),
        "max": float(np.max(values)),
    }


def _profit_factor(returns: np.ndarray) -> float | None:
    """Gross gains ÷ gross losses.

    Undefined rather than infinite when nothing lost: `inf` is not
    representable in JSON, and writing a huge number in its place would read as
    a measured value. Null says "no losing trades", which is the fact.
    """
    losses = float(-np.sum(returns[returns < 0]))
    if losses <= 0:
        return None
    return float(np.sum(returns[returns > 0])) / losses


@dataclass(frozen=True)
class TrackAStats:
    """§6.1's per-trade table for one §4.2 zone variant."""

    variant: str
    trades: int
    chains: int
    wins: int
    win_rate: float | None
    mean_return: float | None
    median_return: float | None
    profit_factor: float | None
    return_percentiles: Mapping[str, float | None]
    holding_days: Mapping[str, float | None]
    exit_reasons: Mapping[str, int]
    trades_by_year: Mapping[str, int]
    skipped_entries: Mapping[str, int]
    # §6.1's per-trade market delta: r_trade − r_SPY over the same window.
    # `unpriced` counts trades whose window the benchmark could not cover, so a
    # thin benchmark shows up as a count rather than as a quietly smaller mean.
    market_delta: Mapping[str, float | None]
    market_delta_unpriced: int

    def as_dict(self) -> dict:
        return {
            "variant": self.variant,
            "trades": self.trades,
            "chains": self.chains,
            "wins": self.wins,
            "win_rate": self.win_rate,
            "mean_return": self.mean_return,
            "median_return": self.median_return,
            "profit_factor": self.profit_factor,
            "return_percentiles": dict(self.return_percentiles),
            "holding_days": dict(self.holding_days),
            "exit_reasons": dict(self.exit_reasons),
            "trades_by_year": dict(self.trades_by_year),
            "skipped_entries": dict(self.skipped_entries),
            "market_delta": dict(self.market_delta),
            "market_delta_unpriced": self.market_delta_unpriced,
        }


class BenchmarkPrices:
    """SPY on the P2 basis, read at whichever price a fill actually used (§6.1).

    The market delta has to be measured over the trade's own window on the
    trade's own basis, so a trade filled at an open is compared against SPY's
    open and one marked at a close against SPY's close. Mixing the two would
    quietly inject an overnight gap into every `delisted` trade's alpha.
    """

    def __init__(self, frame: pd.DataFrame | None) -> None:
        self._by_date: dict[date, tuple[float, float]] = {}
        if frame is None or frame.empty:
            return
        frame = frame.sort_index()
        opens = frame["Open"].to_numpy(dtype="float64")
        closes = frame["Close"].to_numpy(dtype="float64")
        for position, stamp in enumerate(pd.DatetimeIndex(frame.index)):
            self._by_date[stamp.date()] = (float(opens[position]), float(closes[position]))

    def price(self, day: date, basis: str) -> float | None:
        row = self._by_date.get(day)
        if row is None:
            return None
        value = row[0] if basis == "open" else row[1]
        return value if np.isfinite(value) and value > 0 else None

    def hold_return(self, trade: Trade) -> float | None:
        entry = self.price(trade.entry_date, trade.entry_basis)
        exit_ = self.price(trade.exit_date, trade.exit_basis)
        if entry is None or exit_ is None:
            return None
        return exit_ / entry - 1.0


def track_a(
    variant: str,
    trades: Sequence[Trade],
    skipped: Sequence[SkippedEntry],
    benchmark: BenchmarkPrices,
) -> TrackAStats:
    """Summarize one variant's trades exactly as §6.1 lists them."""
    returns = np.array([trade.r_trade for trade in trades], dtype="float64")
    held = np.array([trade.holding_days for trade in trades], dtype="float64")

    exit_reasons: dict[str, int] = {}
    by_year: dict[str, int] = {}
    for trade in trades:
        exit_reasons[trade.exit_reason] = exit_reasons.get(trade.exit_reason, 0) + 1
        year = str(trade.entry_date.year)
        by_year[year] = by_year.get(year, 0) + 1

    skipped_counts: dict[str, int] = {}
    for entry in skipped:
        skipped_counts[entry.reason] = skipped_counts.get(entry.reason, 0) + 1

    deltas: list[float] = []
    unpriced = 0
    for trade in trades:
        hold = benchmark.hold_return(trade)
        if hold is None:
            unpriced += 1
            continue
        deltas.append(trade.r_trade - hold)
    delta_values = np.array(deltas, dtype="float64")

    wins = int(np.sum(returns > 0)) if returns.size else 0
    percentiles = (
        {
            f"p{level}": float(value)
            for level, value in zip(
                RETURN_PERCENTILES,
                np.percentile(returns, RETURN_PERCENTILES),
                strict=True,
            )
        }
        if returns.size
        else {f"p{level}": None for level in RETURN_PERCENTILES}
    )

    return TrackAStats(
        variant=variant,
        trades=len(trades),
        chains=len({trade.chain for trade in trades}),
        wins=wins,
        win_rate=(wins / len(trades)) if trades else None,
        mean_return=float(np.mean(returns)) if returns.size else None,
        median_return=float(np.median(returns)) if returns.size else None,
        profit_factor=_profit_factor(returns) if returns.size else None,
        return_percentiles=percentiles,
        holding_days=_summary(held),
        exit_reasons=dict(sorted(exit_reasons.items())),
        trades_by_year=dict(sorted(by_year.items())),
        skipped_entries=dict(sorted(skipped_counts.items())),
        market_delta=_summary(delta_values),
        market_delta_unpriced=unpriced,
    )


def track_a_json(stats: Sequence[TrackAStats]) -> str:
    """Stable serialization — §10.1 wants two runs byte-identical."""
    payload = {item.variant: item.as_dict() for item in stats}
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"
