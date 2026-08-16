"""Track A statistics (SPEC-BACKTEST.md §6.1) and §6.3's benchmarks.

Per-trade, not per-portfolio: every §4.2 signal counted independently, which is
the primary result §6 asks for. The sleeve's portfolio arithmetic is §6.2 and
lives in `sleeve.py`. What lands here is what §6.1 pins — the distribution of
trade outcomes, and the per-trade benchmarks each track can carry — plus §6.3's
two buy-and-hold curves, which are portfolio-level but belong beside the
statistics they are compared against rather than inside the simulation.

`max_drawdown` and `cagr` are shared with the sleeve deliberately: §6.2 and
§6.3 both measure drawdown on a daily curve and growth over the same window, and
two implementations of one definition is how the sleeve and its own benchmark
end up not comparable.

§6.1 lists one table and reports it "for both the stock track and the LEAP
overlay", so there is one summarizer here and the track is a field on the
result. The single asymmetry is deliberate and is §6.1's own: **vehicle alpha is
reported on the overlay only**. On the stock track there is nothing to compute —
`r_trade` *is* the same-window hold, so the difference would be zero for every
trade by construction, and a column of zeros would look like a measurement.
`None` there means "this track has no such quantity", which is a different claim
from zero and from unmeasured.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from leaps_scanner.backtest import data
from leaps_scanner.backtest.data import ADJ_CLOSE
from leaps_scanner.backtest.engine import SkippedEntry, Trade
from leaps_scanner.backtest.synthetic import OverlayConfig, SyntheticTrade

RETURN_PERCENTILES = (5, 25, 50, 75, 95)

STOCK_TRACK = "stock"
OVERLAY_TRACK = "overlay"


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
    """§6.1's per-trade table for one track's one variant.

    `variant` names the §4.2 zone variant on the stock track and the §5.6
    configuration on the overlay; `parameters` carries that configuration's m, h
    and zone so the sensitivity table can be read off the rows themselves.
    """

    track: str
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
    # §6.1's vehicle alpha, on the overlay track only — see the module docstring
    # for why the stock track's is None rather than a column of zeros.
    vehicle_alpha: Mapping[str, float | None] | None = None
    # The §5.6 parameters behind an overlay row; None on the stock track, which
    # has none (m and h touch the synthetic layer only).
    parameters: Mapping[str, float | str] | None = None
    # §2.4 / acceptance 2: coverage below 85% must put a visible warning on every
    # headline table, and this *is* a headline table. Carried on the statistics
    # rather than left in a log line, so the warning cannot be separated from the
    # numbers it qualifies. `None` means coverage was not measured for this run —
    # which is not the same claim as "coverage was fine".
    coverage_ratio: float | None = None
    low_coverage_warning: bool | None = None

    def as_dict(self) -> dict:
        return {
            "track": self.track,
            "variant": self.variant,
            "parameters": None if self.parameters is None else dict(self.parameters),
            "coverage_ratio": self.coverage_ratio,
            "low_coverage_warning": self.low_coverage_warning,
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
            "vehicle_alpha": None if self.vehicle_alpha is None else dict(self.vehicle_alpha),
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


def _summarize(
    *,
    track: str,
    variant: str,
    trades: Sequence[Trade],
    returns: Sequence[float],
    skipped: Sequence[SkippedEntry],
    benchmark: BenchmarkPrices,
    alphas: Sequence[float] | None,
    parameters: Mapping[str, float | str] | None,
    coverage: data.Coverage | None,
) -> TrackAStats:
    """§6.1's table over one track's trades and that track's own returns.

    `trades` supplies the windows, the exits and the names; `returns` supplies
    what the track actually earned over them, which is `r_trade` for the shares
    and `r_overlay` for the option. Keeping them as two arguments is what lets
    the overlay be measured against the market on its own return without
    reimplementing the distribution beside this one.
    """
    values = np.array(returns, dtype="float64")
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
    for trade, value in zip(trades, values, strict=True):
        hold = benchmark.hold_return(trade)
        if hold is None:
            unpriced += 1
            continue
        deltas.append(float(value) - hold)
    delta_values = np.array(deltas, dtype="float64")

    wins = int(np.sum(values > 0)) if values.size else 0
    percentiles = (
        {
            f"p{level}": float(percentile)
            for level, percentile in zip(
                RETURN_PERCENTILES,
                np.percentile(values, RETURN_PERCENTILES),
                strict=True,
            )
        }
        if values.size
        else {f"p{level}": None for level in RETURN_PERCENTILES}
    )

    return TrackAStats(
        track=track,
        variant=variant,
        trades=len(trades),
        chains=len({trade.chain for trade in trades}),
        wins=wins,
        win_rate=(wins / len(trades)) if trades else None,
        mean_return=float(np.mean(values)) if values.size else None,
        median_return=float(np.median(values)) if values.size else None,
        profit_factor=_profit_factor(values) if values.size else None,
        return_percentiles=percentiles,
        holding_days=_summary(held),
        exit_reasons=dict(sorted(exit_reasons.items())),
        trades_by_year=dict(sorted(by_year.items())),
        skipped_entries=dict(sorted(skipped_counts.items())),
        market_delta=_summary(delta_values),
        market_delta_unpriced=unpriced,
        vehicle_alpha=(None if alphas is None else _summary(np.array(alphas, dtype="float64"))),
        parameters=parameters,
        coverage_ratio=None if coverage is None else round(coverage.ratio, 6),
        low_coverage_warning=None if coverage is None else coverage.low_coverage_warning,
    )


def track_a(
    variant: str,
    trades: Sequence[Trade],
    skipped: Sequence[SkippedEntry],
    benchmark: BenchmarkPrices,
    *,
    coverage: data.Coverage | None = None,
) -> TrackAStats:
    """§6.1 for the stock track: one §4.2 zone variant's trades.

    `coverage` travels with the table on purpose (acceptance 2): a headline
    statistic and the share of the universe behind it belong to the same object,
    so no caller can print one without the other.
    """
    return _summarize(
        track=STOCK_TRACK,
        variant=variant,
        trades=trades,
        returns=[trade.r_trade for trade in trades],
        skipped=skipped,
        benchmark=benchmark,
        alphas=None,
        parameters=None,
        coverage=coverage,
    )


def track_a_overlay(
    config: OverlayConfig,
    trades: Sequence[SyntheticTrade],
    skipped: Sequence[SkippedEntry],
    benchmark: BenchmarkPrices,
    *,
    coverage: data.Coverage | None = None,
) -> TrackAStats:
    """§6.1 for the LEAP overlay: one §5.6 configuration's trades.

    The overlay's exits differ from the stock track's, so its trade windows do
    too — which is exactly why the vehicle alpha here is measured against each
    overlay trade's *own* share hold rather than against the stock track's trade
    on the same signal.
    """
    return _summarize(
        track=OVERLAY_TRACK,
        variant=config.name,
        trades=[item.trade for item in trades],
        returns=[item.r_overlay for item in trades],
        skipped=skipped,
        benchmark=benchmark,
        alphas=[item.vehicle_alpha for item in trades],
        parameters={
            "zone": config.zone,
            "sigma_multiplier": config.sigma_multiplier,
            "friction": config.friction,
        },
        coverage=coverage,
    )


# ---------------------------------------------------------------------------
# Curve arithmetic, shared by §6.2's sleeve and §6.3's benchmarks.
# ---------------------------------------------------------------------------


def max_drawdown(dates: Sequence[date], curve: Sequence[float]) -> tuple[float | None, date | None]:
    """Deepest peak-to-trough fall on a daily curve, as a positive fraction.

    Returns the trough date alongside, so a report can say *when* rather than
    only how deep. `None` when there is no curve to measure — an empty run has
    no drawdown, which is a different claim from a drawdown of zero.
    """
    if not curve:
        return None, None
    peak = curve[0]
    worst = 0.0
    worst_date = dates[0]
    for day, value in zip(dates, curve, strict=True):
        if value > peak:
            peak = value
        if peak > 0:
            drop = 1.0 - value / peak
            if drop > worst:
                worst, worst_date = drop, day
    return worst, worst_date


def cagr(start: float, final: float, window: data.Window) -> float | None:
    """Compound annual growth from `start` to `final` over the whole window.

    `None` when it is not defined rather than a stand-in number: a curve that
    ended at or below zero has no real growth rate (the root of a non-positive
    ratio is not a real number), and reporting one would be inventing a figure at
    the exact moment the truth is worst.
    """
    years = (window.end - window.start).days / 365.25
    if years <= 0 or start <= 0 or final <= 0:
        return None
    return (final / start) ** (1.0 / years) - 1.0


# ---------------------------------------------------------------------------
# §6.3's benchmarks.
# ---------------------------------------------------------------------------

# §6.3.1's caption, normative rather than editorial: the sleeve risks at most 15%
# of equity by design while SPY is fully invested, so the two CAGRs are not
# like-for-like and the table must say so where the numbers are, not in a
# footnote a reader can skip.
EXPOSURE_CAVEAT = (
    "Not like-for-like: the sleeve holds at most 15% of equity in premium by "
    "design (SPEC.md §7) and the rest in cash at 0%, while SPY is 100% invested "
    "throughout. Compare the shapes, not the headline CAGRs."
)


@dataclass(frozen=True)
class BenchmarkResult:
    """One §6.3 buy-and-hold curve, normalized to 1.0 at the window start."""

    name: str
    label: str
    # A legend-length name. The full label describes the construction and does
    # not fit a chart key; truncating it there would clip mid-word, so the short
    # form is chosen rather than derived.
    short_label: str
    total_return: float | None
    cagr: float | None
    max_drawdown: float | None
    max_drawdown_date: date | None
    curve_dates: tuple[date, ...]
    curve_values: tuple[float, ...]
    # §6.3.2 only: how many names the curve actually holds, and how many entered
    # names had no usable price. An equal-weight benchmark that quietly dropped a
    # third of its constituents is not the benchmark §6.3.2 describes.
    constituents: int | None = None
    unpriced: int | None = None
    caveat: str | None = None
    coverage_ratio: float | None = None
    low_coverage_warning: bool | None = None

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "short_label": self.short_label,
            "total_return": self.total_return,
            "cagr": self.cagr,
            "max_drawdown": self.max_drawdown,
            "max_drawdown_date": (
                self.max_drawdown_date.isoformat() if self.max_drawdown_date else None
            ),
            "constituents": self.constituents,
            "unpriced": self.unpriced,
            "caveat": self.caveat,
            "coverage_ratio": self.coverage_ratio,
            "low_coverage_warning": self.low_coverage_warning,
        }


def _curve_result(
    name: str,
    label: str,
    short_label: str,
    dates: Sequence[date],
    values: Sequence[float],
    window: data.Window,
    **extra: object,
) -> BenchmarkResult:
    """Wrap a normalized curve in §6.3's reported statistics."""
    drawdown, drawdown_date = max_drawdown(dates, values)
    final = values[-1] if values else None
    return BenchmarkResult(
        name=name,
        label=label,
        short_label=short_label,
        total_return=(final - 1.0) if final is not None else None,
        cagr=cagr(1.0, final, window) if final is not None else None,
        max_drawdown=drawdown,
        max_drawdown_date=drawdown_date,
        curve_dates=tuple(dates),
        curve_values=tuple(values),
        **extra,  # type: ignore[arg-type]
    )


def spy_buy_and_hold(
    cache: data.PriceCache,
    window: data.Window,
    *,
    coverage: data.Coverage | None = None,
) -> BenchmarkResult | None:
    """§6.3.1: SPY total return over the full window.

    `Adj Close`, not `Close` — §6.3.1 says total return in so many words, and P2
    retains the column for exactly this. Using the raw close here would silently
    strip a decade of dividends out of the benchmark the strategy is judged
    against, which is the one direction that would flatter the result.

    `None` when SPY is not cached: §3.5 lets a run continue without it, and a
    missing benchmark is reported as missing rather than rendered as a flat line.
    """
    frame = cache.load_benchmark()
    if frame is None or frame.empty or ADJ_CLOSE not in frame:
        return None

    frame = frame.sort_index()
    stamps = pd.DatetimeIndex(frame.index)
    inside = (stamps.date >= window.start) & (stamps.date <= window.end)
    values = frame[ADJ_CLOSE].to_numpy(dtype="float64")[inside]
    days = [stamp.date() for stamp in stamps[inside]]

    usable = np.isfinite(values) & (values > 0)
    values, days = values[usable], [day for day, keep in zip(days, usable, strict=True) if keep]
    if values.size == 0:
        return None

    return _curve_result(
        "spy_total_return",
        f"{data.BENCHMARK_SYMBOL} buy-and-hold (total return)",
        f"{data.BENCHMARK_SYMBOL} (total return)",
        days,
        (values / values[0]).tolist(),
        window,
        caveat=EXPOSURE_CAVEAT,
        coverage_ratio=None if coverage is None else round(coverage.ratio, 6),
        low_coverage_warning=None if coverage is None else coverage.low_coverage_warning,
    )


def equal_weight_entered(
    chains: Sequence[str],
    cache: data.PriceCache,
    window: data.Window,
    calendar: Sequence[date],
    *,
    closes: data.ChainCloses | None = None,
    coverage: data.Coverage | None = None,
) -> BenchmarkResult | None:
    """§6.3.2: equal-weight buy-and-hold of the entered names, full window.

    **Full window, not each trade's window** — the amendment §0's Decision 7
    records. A same-trade-window hold is arithmetically the stock track itself
    (`r_trade` *is* that hold), so the original wording compared the strategy
    against a restatement of itself. Over the full window it answers the question
    it was meant to: does the *timing* add anything beyond the names?

    Each name is bought at its first eligible session (§3.1's warm-up rule, so a
    name is held only from the date the strategy could have evaluated it) and
    held to the window end. A name that delists mid-window holds to its last
    close and then sits in cash — its wealth relative simply stops moving, which
    is what `close_on`'s last-price-at-or-before lookup does by construction.
    Before its own start a name contributes 1.0, i.e. uninvested cash, so the
    curve is a portfolio that fills up as names become eligible rather than one
    that pretends to have owned them from the start.

    P2 basis (raw `Close`), matching the stock track it is compared against.

    `closes` is shared with the sleeve when the caller has one. The entered names
    are exactly the chains §6.2 just walked, so building a second `ChainCloses`
    re-reads all of them from Parquet for no benefit — the same reason
    `sleeve.simulate_all` threads one instance through both of its sleeves.
    """
    if not calendar:
        return None

    closes = closes if closes is not None else data.ChainCloses(cache)
    weights: list[np.ndarray] = []
    unpriced = 0

    for chain in sorted(set(chains)):
        frame = cache.load(chain)
        eligible = data.first_eligible_date(frame)
        if eligible is None:
            unpriced += 1
            continue
        start_day = max(eligible, window.start)

        basis: float | None = None
        relative = np.ones(len(calendar), dtype="float64")
        for position, day in enumerate(calendar):
            if day < start_day:
                continue
            price = closes.close_on(chain, day)
            if price is None:
                continue
            if basis is None:
                basis = price
            relative[position] = price / basis
        if basis is None:
            unpriced += 1
            continue
        weights.append(relative)

    if not weights:
        return None

    curve = np.mean(np.vstack(weights), axis=0)
    return _curve_result(
        "equal_weight_entered",
        "Equal-weight buy-and-hold of entered names (full window)",
        "Equal-weight entered names",
        list(calendar),
        curve.tolist(),
        window,
        constituents=len(weights),
        unpriced=unpriced,
        coverage_ratio=None if coverage is None else round(coverage.ratio, 6),
        low_coverage_warning=None if coverage is None else coverage.low_coverage_warning,
    )


def track_a_json(stats: Sequence[TrackAStats]) -> str:
    """Stable serialization — §10.1 wants two runs byte-identical.

    Grouped by track: "base" names a zone variant on one and a §5.6
    configuration on the other, so a flat mapping would collide.
    """
    payload: dict[str, dict[str, dict]] = {}
    for item in stats:
        payload.setdefault(item.track, {})[item.variant] = item.as_dict()
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"
