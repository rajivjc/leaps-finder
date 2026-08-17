"""Does SPEC.md §6's Trend subscore predict a trade's outcome? (analysis)

SPEC-BACKTEST.md §0 Decision 2 excluded the §6 score from v2 on purpose, and
this module does not reverse that: the engine, the overlay and the sleeve are
untouched, and no threshold is re-pinned. What it adds is a measurement of the
score the backtest declined to use — the Trend subscore (25% of the composite)
and Entry (15%) are computed for every replayed trade at the moment the live
scanner would have scored it, and their buckets are compared against the market.

**Market delta, not raw return, is the quantity that answers the question.**
Trending names rose over any ten-year window that contained this one, so a
high-Trend bucket earning more than a low-Trend bucket says nothing. §6.1's
per-trade market delta — the trade's return less SPY's over the same days on the
same fill basis — is what has to move, because it is the only thing that can pay
for the friction of buying the option instead of the shares.

Two rules make the answer worth reading, and both are enforced rather than
merely documented:

* **The scoring frame is sliced to the signal date.** `indicators.evaluate`
  reads the last bar of whatever frame it is handed, so passing a chain's whole
  history scores every trade against ten years of hindsight — silently, with no
  exception raised and a beautiful result at the end. `score_trades` slices to
  `<= signal_date` and checks that the slice held.
* **The recomputed slowK must equal the one the engine already recorded.** Every
  trade carries `entry_slow_k`, written by the engine's own vectorized panel. If
  `indicators.evaluate` disagrees, the as-of alignment is wrong and every number
  downstream is meaningless — so that raises `AlignmentError` rather than warning
  and carrying on. It is a free, decisive gate over the whole analysis.

Scoring happens at the **signal date, not the entry date**. P1 fills at the next
session's open, so the signal date is the completed weekly bar a Saturday scan
would have seen; scoring at the entry date leaks one session of hindsight.

`leaps_scanner.scoring` stays the reference implementation, exactly as
`engine.py` treats `leaps_scanner.indicators`: the clip_map bounds and the
extension gate are called, never restated here.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats as scipy_stats

from leaps_scanner import indicators, scoring
from leaps_scanner.backtest import data
from leaps_scanner.backtest.engine import TIME_EXIT_DAYS, Trade
from leaps_scanner.backtest.metrics import BenchmarkPrices

logger = logging.getLogger(__name__)

# Percentile bootstrap settings. Both are fixed so two runs of the same input are
# byte-identical (§10.1): a confidence interval that moved between runs would be
# indistinguishable from the finding moving.
BOOTSTRAP_REPS = 10_000
BOOTSTRAP_SEED = 20260817
CONFIDENCE = 0.95

# Buckets for the reported tables.
DECILES = 10

# Floats are rounded before serialization so two machines agree.
JSON_FLOAT_PLACES = 10

# The held-out split. Chosen as the midpoint of the trade count, once, before
# any statistic was read — not tuned to make a result appear.
HELD_OUT_FROM_YEAR = 2022

# The §4.2 zone variant this analysis measures. Base alone: P13 reports Strict
# alongside as a sensitivity, and analysing both would invite quoting whichever
# came out kinder.
VARIANT = "base"


class AlignmentError(ValueError):
    """The recomputed signal disagreed with the one the engine recorded.

    Raised rather than logged because it invalidates the entire analysis: a
    mismatch means `evaluate` was handed the wrong window, and every score
    computed from it describes a different moment than the trade it is attached
    to.
    """


@dataclass(frozen=True)
class ScoredTrade:
    """One replayed trade with its §6 subscores, as of its own signal date."""

    chain: str
    symbol: str
    signal_date: date
    entry_date: date
    entry_year: int
    entry_month: str  # the bootstrap's resampling block
    holding_days: int
    exit_reason: str
    r_trade: float
    # None when the benchmark could not price the trade's own window.
    market_delta: float | None
    # None when no overlay trade opened on this chain and entry date.
    r_overlay: float | None
    s_trend: float
    s_entry: float

    @property
    def held_to_time_exit(self) -> bool:
        """Whether the position survived to §4.4's calendar cut."""
        return self.holding_days >= TIME_EXIT_DAYS


@dataclass(frozen=True)
class ScoreResult:
    """Every scored trade, plus what could not be scored and why.

    The counts are carried rather than logged: a bucket table computed over
    silently thinned trades is the "partial data looking complete" CLAUDE.md
    forbids, so the denominators travel with the numbers.
    """

    scored: tuple[ScoredTrade, ...]
    trades_in: int
    insufficient_history: int
    unpriced_chains: int
    unpriced_benchmark: int
    overlay_matched: int


def score_trades(
    trades: Sequence[Trade],
    cache: data.PriceCache,
    benchmark: BenchmarkPrices,
    *,
    overlay_returns: Mapping[tuple[str, date], float] | None = None,
) -> ScoreResult:
    """Compute §6's Trend and Entry subscores for every trade, at its signal date.

    One `indicators.evaluate` call per distinct (chain, signal date): several
    variants' trades can share a signal, and the call is the expensive part.

    Raises `AlignmentError` on the first trade whose recomputed slowK differs
    from the engine's — see the module docstring for why that is fatal.
    """
    overlay_returns = overlay_returns or {}
    by_chain: dict[str, list[Trade]] = {}
    for trade in trades:
        by_chain.setdefault(trade.chain, []).append(trade)

    scored: list[ScoredTrade] = []
    insufficient = 0
    unpriced_chains = 0
    unpriced_benchmark = 0
    overlay_matched = 0

    for chain, chain_trades in sorted(by_chain.items()):
        frame = cache.load(chain)
        if frame is None or frame.empty:
            # The engine priced this chain to produce the trades in hand, so a
            # frame that has since gone missing is a cache problem, not a
            # property of the data. Counted loudly rather than dropped.
            logger.warning("%s: no cached frame; %d trades unscored", chain, len(chain_trades))
            unpriced_chains += len(chain_trades)
            continue
        frame = frame.sort_index()

        signals: dict[date, indicators.Signals | None] = {}
        for trade in chain_trades:
            if trade.signal_date not in signals:
                signals[trade.signal_date] = _evaluate_at(frame, chain, trade.signal_date)
            computed = signals[trade.signal_date]
            if computed is None:
                insufficient += 1
                continue

            _check_alignment(trade, computed)

            hold = benchmark.hold_return(trade)
            if hold is None:
                unpriced_benchmark += 1
            overlay = overlay_returns.get((trade.chain, trade.entry_date))
            if overlay is not None:
                overlay_matched += 1

            scored.append(
                ScoredTrade(
                    chain=trade.chain,
                    symbol=trade.symbol,
                    signal_date=trade.signal_date,
                    entry_date=trade.entry_date,
                    entry_year=trade.entry_date.year,
                    entry_month=trade.entry_date.strftime("%Y-%m"),
                    holding_days=trade.holding_days,
                    exit_reason=trade.exit_reason,
                    r_trade=trade.r_trade,
                    market_delta=None if hold is None else trade.r_trade - hold,
                    r_overlay=overlay,
                    s_trend=scoring.trend_score(computed),
                    s_entry=scoring.entry_score(computed.stoch_k, computed.weeks_since_cross_up),
                )
            )

    return ScoreResult(
        scored=tuple(sorted(scored, key=lambda item: (item.entry_date, item.chain))),
        trades_in=len(trades),
        insufficient_history=insufficient,
        unpriced_chains=unpriced_chains,
        unpriced_benchmark=unpriced_benchmark,
        overlay_matched=overlay_matched,
    )


def _evaluate_at(frame: pd.DataFrame, chain: str, signal_date: date) -> indicators.Signals | None:
    """`indicators.evaluate` as of one signal date, or None without the history.

    `today = signal_date + 1 day` is what keeps the signal's own week. §4's
    entry signals only ever fire on a Friday whose bar completed at that close,
    and `completed_weekly_bars` drops the newest bar when its last session is
    *today*; a Saturday reference is exactly the live scanner's own cadence.
    """
    sliced = frame.loc[: pd.Timestamp(signal_date)]
    if sliced.empty:
        return None
    # The whole analysis rests on this window. Checked, not assumed — and with a
    # real exception, because `assert` disappears under `python -O`.
    #
    # `max()`, not `index[-1]`: `evaluate` sorts before reading its last bar, so
    # a later session sitting anywhere in the frame leaks, not only one at the
    # end. Reading the last row would assume the sortedness this is meant to
    # verify — and `loc[:x]` on an unsorted index does not guarantee it.
    latest = sliced.index.max().date()
    if latest > signal_date:
        raise AlignmentError(f"{chain}: slice through {signal_date} leaked session {latest}")

    try:
        return indicators.evaluate(sliced, today=signal_date + timedelta(days=1))
    except indicators.InsufficientHistory:
        return None


def _check_alignment(trade: Trade, computed: indicators.Signals) -> None:
    """The engine's `entry_slow_k` against `evaluate`'s, for one trade."""
    recorded = trade.entry_slow_k
    if not np.isfinite(recorded):
        raise AlignmentError(f"{trade.chain} {trade.signal_date}: trade carries no entry_slow_k")
    # Both sides derive from the same `slow_stochastic` over the same bars, so
    # they agree bit for bit in practice; the tolerance is for float replay
    # across platforms, not for a genuine difference of opinion.
    if abs(computed.stoch_k - recorded) > 1e-9:
        raise AlignmentError(
            f"{trade.chain} {trade.signal_date}: evaluate slowK {computed.stoch_k!r} "
            f"but the engine recorded {recorded!r} — the as-of alignment is wrong"
        )


# ---------------------------------------------------------------------------
# Statistics.
# ---------------------------------------------------------------------------


def _profit_factor(values: np.ndarray) -> float | None:
    """Gross gains ÷ gross losses; None when nothing lost (see `metrics`)."""
    losses = float(-np.sum(values[values < 0]))
    if losses <= 0:
        return None
    return float(np.sum(values[values > 0])) / losses


def block_bootstrap_ci(
    values: Sequence[float],
    blocks: Sequence[str],
    *,
    reps: int = BOOTSTRAP_REPS,
    seed: int = BOOTSTRAP_SEED,
    confidence: float = CONFIDENCE,
) -> tuple[float, float] | None:
    """Percentile CI on the mean, resampling whole blocks with replacement.

    The block is the entry month. Trades overlap in time — many are open
    simultaneously and share the same market moves — so resampling individual
    trades would treat correlated observations as independent and report an
    interval roughly √2 too narrow. Resampling whole months keeps
    within-month correlation intact.

    None when there is nothing to resample, which is a different statement from
    an interval of zero width.
    """
    array = np.asarray(values, dtype="float64")
    if array.size == 0 or len(blocks) != array.size:
        return None

    grouped: dict[str, list[int]] = {}
    for index, block in enumerate(blocks):
        grouped.setdefault(block, []).append(index)
    # Sorted so the bootstrap does not depend on dict insertion order.
    members = [np.asarray(grouped[key], dtype="int64") for key in sorted(grouped)]
    if not members:
        return None

    rng = np.random.default_rng(seed)
    count = len(members)
    means = np.empty(reps, dtype="float64")
    for rep in range(reps):
        picked = rng.integers(0, count, count)
        means[rep] = array[np.concatenate([members[index] for index in picked])].mean()

    tail = 100.0 * (1.0 - confidence) / 2.0
    return float(np.percentile(means, tail)), float(np.percentile(means, 100.0 - tail))


def _spearman(x: Sequence[float], y: Sequence[float]) -> tuple[float | None, float | None]:
    """Rank correlation, or (None, None) when it is not defined.

    The p-value assumes independent observations, which these are not — see
    `block_bootstrap_ci`. It is reported because a correlation without one is
    unreadable, and qualified wherever it is quoted.
    """
    if len(x) < 3:
        return None, None
    result = scipy_stats.spearmanr(np.asarray(x, dtype="float64"), np.asarray(y, dtype="float64"))
    rho, p_value = float(result.statistic), float(result.pvalue)
    if not np.isfinite(rho):
        return None, None
    return rho, (p_value if np.isfinite(p_value) else None)


def _quantile_buckets(values: np.ndarray, buckets: int) -> np.ndarray:
    """Bucket index per value, split at quantiles.

    Ties are not broken: a subscore with a third of its mass on one value (Entry
    is exactly that — §4.2 already requires `turning_up`, so most entries score
    full marks on freshness) produces genuinely uneven buckets. Reporting the
    real `n` per bucket is honest; forcing equal counts would split identical
    scores into "high" and "low" and invent a difference.
    """
    edges = np.quantile(values, np.linspace(0.0, 1.0, buckets + 1)[1:-1])
    return np.searchsorted(edges, values, side="right")


def _delta_pairs(rows: Sequence[ScoredTrade]) -> tuple[list[float], list[str]]:
    """Market deltas and their entry months, skipping trades the benchmark missed."""
    values = [row.market_delta for row in rows if row.market_delta is not None]
    months = [row.entry_month for row in rows if row.market_delta is not None]
    return values, months


def bucket_row(label: str, rows: Sequence[ScoredTrade]) -> dict:
    """§6.1's columns over one bucket, plus what this analysis adds."""
    returns = np.array([row.r_trade for row in rows], dtype="float64")
    deltas, months = _delta_pairs(rows)
    delta_array = np.asarray(deltas, dtype="float64")
    overlay = np.array(
        [row.r_overlay for row in rows if row.r_overlay is not None], dtype="float64"
    )
    interval = block_bootstrap_ci(deltas, months)

    return {
        "bucket": label,
        "trades": len(rows),
        "win_rate": float(np.mean(returns > 0)) if returns.size else None,
        "mean_return": float(np.mean(returns)) if returns.size else None,
        "median_return": float(np.median(returns)) if returns.size else None,
        "profit_factor": _profit_factor(returns) if returns.size else None,
        "market_delta_mean": float(np.mean(delta_array)) if delta_array.size else None,
        "market_delta_median": float(np.median(delta_array)) if delta_array.size else None,
        "market_delta_ci_low": None if interval is None else interval[0],
        "market_delta_ci_high": None if interval is None else interval[1],
        "market_delta_priced": int(delta_array.size),
        "overlay_mean": float(np.mean(overlay)) if overlay.size else None,
        "held_to_time_exit": (
            float(np.mean([row.held_to_time_exit for row in rows])) if rows else None
        ),
        "mean_holding_days": (float(np.mean([row.holding_days for row in rows])) if rows else None),
    }


def bucket_table(rows: Sequence[ScoredTrade], field: str, buckets: int = DECILES) -> list[dict]:
    """One row per quantile bucket of `field`, low to high."""
    if not rows:
        return []
    values = np.array([getattr(row, field) for row in rows], dtype="float64")
    assigned = _quantile_buckets(values, buckets)

    table: list[dict] = []
    for index in range(buckets):
        members = [row for row, bucket in zip(rows, assigned, strict=True) if bucket == index]
        if not members:
            continue
        scores = [getattr(row, field) for row in members]
        label = f"{min(scores):.1f}–{max(scores):.1f}"
        table.append(bucket_row(label, members))
    return table


def tail_spread(rows: Sequence[ScoredTrade], field: str, share: float) -> dict:
    """Top-share minus bottom-share mean market delta, with a bootstrap CI.

    The cut is recomputed inside every replicate, so the interval covers the
    whole selection procedure rather than the averaging that follows a threshold
    already chosen on the full sample.
    """
    priced = [row for row in rows if row.market_delta is not None]
    if not priced:
        return {"share": share, "observed": None, "ci_low": None, "ci_high": None}

    values = np.array([getattr(row, field) for row in priced], dtype="float64")
    deltas = np.array([row.market_delta for row in priced], dtype="float64")
    observed = _spread(values, deltas, share)

    grouped: dict[str, list[int]] = {}
    for index, row in enumerate(priced):
        grouped.setdefault(row.entry_month, []).append(index)
    members = [np.asarray(grouped[key], dtype="int64") for key in sorted(grouped)]

    rng = np.random.default_rng(BOOTSTRAP_SEED)
    count = len(members)
    spreads = np.empty(BOOTSTRAP_REPS, dtype="float64")
    for rep in range(BOOTSTRAP_REPS):
        index = np.concatenate([members[pick] for pick in rng.integers(0, count, count)])
        spreads[rep] = _spread(values[index], deltas[index], share)

    tail = 100.0 * (1.0 - CONFIDENCE) / 2.0
    return {
        "share": share,
        "observed": observed,
        "ci_low": float(np.percentile(spreads, tail)),
        "ci_high": float(np.percentile(spreads, 100.0 - tail)),
    }


def _spread(values: np.ndarray, deltas: np.ndarray, share: float) -> float:
    high = np.quantile(values, 1.0 - share)
    low = np.quantile(values, share)
    top, bottom = deltas[values >= high], deltas[values <= low]
    if top.size == 0 or bottom.size == 0:
        return float("nan")
    return float(top.mean() - bottom.mean())


def correlations(rows: Sequence[ScoredTrade], field: str) -> dict:
    """Rank correlations of one subscore against the outcomes that matter."""
    priced = [row for row in rows if row.market_delta is not None]
    scores = [getattr(row, field) for row in priced]
    out: dict[str, dict[str, float | None]] = {}
    for name, series in (
        ("market_delta", [row.market_delta for row in priced]),
        ("r_trade", [row.r_trade for row in priced]),
        ("holding_days", [float(row.holding_days) for row in priced]),
    ):
        rho, p_value = _spearman(scores, series)
        out[name] = {"spearman": rho, "p_value": p_value}

    with_overlay = [row for row in rows if row.r_overlay is not None]
    rho, p_value = _spearman(
        [getattr(row, field) for row in with_overlay],
        [row.r_overlay for row in with_overlay],
    )
    out["r_overlay"] = {"spearman": rho, "p_value": p_value, "trades": len(with_overlay)}
    return out


def analyse_field(rows: Sequence[ScoredTrade], field: str) -> dict:
    """Everything reported for one subscore: buckets, correlations, tails, split."""
    in_sample = [row for row in rows if row.entry_year < HELD_OUT_FROM_YEAR]
    held_out = [row for row in rows if row.entry_year >= HELD_OUT_FROM_YEAR]
    values = np.array([getattr(row, field) for row in rows], dtype="float64")

    return {
        "field": field,
        "trades": len(rows),
        "distribution": {
            "mean": float(values.mean()) if values.size else None,
            "median": float(np.median(values)) if values.size else None,
            "min": float(values.min()) if values.size else None,
            "max": float(values.max()) if values.size else None,
        },
        "deciles": bucket_table(rows, field),
        "correlations": correlations(rows, field),
        "tails": [tail_spread(rows, field, share) for share in (0.25, 0.10)],
        "held_out": {
            "split_year": HELD_OUT_FROM_YEAR,
            "in_sample": {
                "trades": len(in_sample),
                "correlations": correlations(in_sample, field),
                "tail_10pct": tail_spread(in_sample, field, 0.10),
            },
            "held_out": {
                "trades": len(held_out),
                "correlations": correlations(held_out, field),
                "tail_10pct": tail_spread(held_out, field, 0.10),
            },
        },
    }


def build_results(result: ScoreResult, *, run_date: date, variant: str) -> dict:
    """The whole analysis as one serializable payload."""
    rows = result.scored
    deltas, months = _delta_pairs(rows)
    interval = block_bootstrap_ci(deltas, months)

    return {
        "run_date": run_date.isoformat(),
        "variant": variant,
        "generated_by": "python -m leaps_scanner.backtest --subscore-dir",
        "gate": {
            "trades_in": result.trades_in,
            "scored": len(rows),
            "insufficient_history": result.insufficient_history,
            "unpriced_chains": result.unpriced_chains,
            "market_delta_unpriced": result.unpriced_benchmark,
            "overlay_matched": result.overlay_matched,
            "alignment": "every scored trade's recomputed slowK equals the engine's entry_slow_k",
        },
        "whole_sample": {
            "trades": len(rows),
            "chains": len({row.chain for row in rows}),
            "mean_return": float(np.mean([row.r_trade for row in rows])) if rows else None,
            "market_delta_mean": float(np.mean(deltas)) if deltas else None,
            "market_delta_median": float(np.median(deltas)) if deltas else None,
            "market_delta_ci_low": None if interval is None else interval[0],
            "market_delta_ci_high": None if interval is None else interval[1],
        },
        "bootstrap": {
            "replicates": BOOTSTRAP_REPS,
            "seed": BOOTSTRAP_SEED,
            "confidence": CONFIDENCE,
            "block": "entry month",
        },
        "subscores": [analyse_field(rows, field) for field in ("s_trend", "s_entry")],
    }


# ---------------------------------------------------------------------------
# Output.
# ---------------------------------------------------------------------------


def _portable(payload: object) -> object:
    """Round floats for cross-machine stability; reject non-finite.

    Not `report.results_json`: that one mandates §8's banner, which belongs to
    the published run and not to an analysis artefact sitting beside it.
    """
    if isinstance(payload, bool) or payload is None or isinstance(payload, str | int):
        return payload
    if isinstance(payload, float):
        if not np.isfinite(payload):
            raise ValueError(f"non-finite float in the subscore payload: {payload!r}")
        return round(payload, JSON_FLOAT_PLACES)
    if isinstance(payload, Mapping):
        return {key: _portable(value) for key, value in payload.items()}
    if isinstance(payload, Sequence):
        return [_portable(item) for item in payload]
    raise TypeError(f"not JSON-serializable: {type(payload).__name__}")


def results_json(payload: Mapping[str, object]) -> str:
    """Stable serialization — two runs of one input must be byte-identical."""
    return json.dumps(_portable(payload), indent=2, sort_keys=True) + "\n"


def _pct(value: float | None, places: int = 2, *, signed: bool = True) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:+.{places}f}%" if signed else f"{value * 100:.{places}f}%"


def _num(value: float | None, places: int = 2) -> str:
    return "n/a" if value is None else f"{value:.{places}f}"


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    lines = [f"| {' | '.join(headers)} |", f"|{'---|' * len(headers)}"]
    lines.extend(f"| {' | '.join(row)} |" for row in rows)
    return "\n".join(lines)


def _decile_table(table: Sequence[Mapping[str, object]]) -> str:
    return _table(
        [
            "Score range",
            "n",
            "Win rate",
            "Mean r",
            "Median r",
            "PF",
            "Mkt delta",
            "95% CI",
            "Median delta",
            "Overlay",
            "P(hold ≥ 186d)",
        ],
        [
            [
                str(row["bucket"]),
                str(row["trades"]),
                _pct(row["win_rate"], 1, signed=False),  # type: ignore[arg-type]
                _pct(row["mean_return"]),  # type: ignore[arg-type]
                _pct(row["median_return"]),  # type: ignore[arg-type]
                _num(row["profit_factor"]),  # type: ignore[arg-type]
                _pct(row["market_delta_mean"]),  # type: ignore[arg-type]
                f"[{_pct(row['market_delta_ci_low'])}, {_pct(row['market_delta_ci_high'])}]",  # type: ignore[arg-type]
                _pct(row["market_delta_median"]),  # type: ignore[arg-type]
                _pct(row["overlay_mean"]),  # type: ignore[arg-type]
                _pct(row["held_to_time_exit"], 1, signed=False),  # type: ignore[arg-type]
            ]
            for row in table
        ],
    )


def _p_value(value: float | None) -> str:
    """A p-value in a form that survives being very small.

    Fixed decimals print every interesting p as `0.000000`, which reads as zero
    and throws away the one thing the number carries — how far from chance the
    correlation sits. These run to e−27.
    """
    return "n/a" if value is None else f"{value:.3g}"


def _correlation_table(correlation: Mapping[str, Mapping[str, object]]) -> str:
    return _table(
        ["Against", "Spearman", "p (independence assumed)"],
        [
            [name, _num(item["spearman"], 4), _p_value(item["p_value"])]  # type: ignore[arg-type]
            for name, item in correlation.items()
        ],
    )


LABELS = {"s_trend": "Trend subscore (25% of the composite)", "s_entry": "Entry subscore (15%)"}


def build_markdown(payload: Mapping[str, object]) -> str:
    """The committed report for one analysis run."""
    gate = payload["gate"]
    whole = payload["whole_sample"]
    boot = payload["bootstrap"]

    parts = [
        f"# §6 subscore test — run {payload['run_date']}",
        "",
        "**Analysis, not a milestone.** SPEC-BACKTEST.md §0 Decision 2 excluded the §6 score",
        "from v2 and this does not reverse it: no threshold is re-pinned, the engine and the",
        "published run under `docs/backtest/2026-08-16/` are untouched. The question here is",
        "narrow — does the Trend subscore, or Entry, order a trade's outcome?",
        "",
        "**Market delta is the quantity that decides it**, not raw return. Trending names rose",
        "over this window, so a high-Trend bucket earning more says nothing on its own; beating",
        "SPY over the trade's own days is the only thing that can pay for the friction of buying",
        "the option instead of the shares.",
        "",
        "## The alignment gate",
        "",
        "Each trade is scored at its **signal date**, not its entry date — P1 fills at the next",
        "session's open, so the signal date is the completed weekly bar a Saturday scan would",
        "have seen. The chain's frame is sliced to that date before `indicators.evaluate` is",
        "called; an unsliced frame would score against the whole ten years without raising",
        "anything. The check that catches it: the recomputed slowK must equal the",
        "`entry_slow_k` the engine already recorded, for every trade.",
        "",
        _table(
            [
                "Trades in",
                "Scored",
                "Insufficient history",
                "Unpriced chains",
                "Market delta unpriced",
                "Overlay matched",
            ],
            [
                [
                    str(gate["trades_in"]),
                    str(gate["scored"]),  # type: ignore[index]
                    str(gate["insufficient_history"]),
                    str(gate["unpriced_chains"]),  # type: ignore[index]
                    str(gate["market_delta_unpriced"]),
                    str(gate["overlay_matched"]),  # type: ignore[index]
                ]
            ],
        ),
        "",
        f"Variant `{payload['variant']}`, {whole['chains']} rename chains.",  # type: ignore[index]
        f"Mean `r_trade` {_pct(whole['mean_return'])}; "  # type: ignore[index]
        f"mean market delta {_pct(whole['market_delta_mean'])} "  # type: ignore[index]
        f"(95% CI [{_pct(whole['market_delta_ci_low'])}, "  # type: ignore[index]
        f"{_pct(whole['market_delta_ci_high'])}]), "  # type: ignore[index]
        f"median {_pct(whole['market_delta_median'])}.",  # type: ignore[index]
        "",
        f"Confidence intervals are percentile bootstraps over {boot['replicates']:,} "  # type: ignore[index]
        f"replicates resampling whole **{boot['block']}** blocks "  # type: ignore[index]
        f"(seed {boot['seed']}). Trades overlap in time, so resampling"  # type: ignore[index]
        " individual trades would report an interval roughly √2 too narrow.",
        "",
    ]

    for item in payload["subscores"]:  # type: ignore[union-attr]
        parts.extend(_subscore_section(item))

    parts.extend(
        [
            "## Caveats",
            "",
            "- Every threshold is exactly as SPEC.md §6 pins it. Nothing here proposes a change.",
            "- The other 60% of the composite — Quality, Option economics, Valuation — is",
            "  untested, because it needs point-in-time fundamentals and historical option",
            "  chains this project does not have. That is unmeasured, not measured and found",
            "  wanting.",
            "- The backtest enters on §4's trend and stochastic signals alone, with none of the",
            "  liquidity, IV, earnings-distance or quality gates the presets add, so this",
            "  population is broader than the live screener's.",
            "- Holding period is an *outcome*. Where a subscore ranks against it, that is not a",
            "  selectable strategy.",
            "- Every bias in SPEC-BACKTEST.md §7 applies unchanged; the trades are the same ones.",
            "",
            "Educational and personal tooling on delayed, unofficial data. Not financial advice.",
            "",
        ]
    )
    return "\n".join(parts)


def _subscore_section(item: Mapping[str, object]) -> list[str]:
    field = str(item["field"])
    distribution = item["distribution"]
    correlation = item["correlations"]
    held = item["held_out"]

    lines = [
        f"## {LABELS.get(field, field)}",
        "",
        f"Distribution: mean {_num(distribution['mean'], 1)}, "  # type: ignore[index]
        f"median {_num(distribution['median'], 1)}, "  # type: ignore[index]
        f"range {_num(distribution['min'], 1)}–{_num(distribution['max'], 1)}.",  # type: ignore[index]
        "",
        _decile_table(item["deciles"]),  # type: ignore[arg-type]
        "",
        _correlation_table(correlation),  # type: ignore[arg-type]
        "",
        "Top-minus-bottom mean market delta, with the cut recomputed inside every replicate:",
        "",
        _table(
            ["Cut", "Spread", "95% CI"],
            [
                [
                    f"top/bottom {tail['share']:.0%}",  # type: ignore[index]
                    _pct(tail["observed"]),  # type: ignore[index]
                    f"[{_pct(tail['ci_low'])}, {_pct(tail['ci_high'])}]",  # type: ignore[index]
                ]
                for tail in item["tails"]  # type: ignore[union-attr]
            ],
        ),
        "",
        f"Held out at {held['split_year']} — "  # type: ignore[index]
        f"{held['in_sample']['trades']} trades in, "  # type: ignore[index]
        f"{held['held_out']['trades']} held out:",  # type: ignore[index]
        "",
        _table(
            ["Period", "Spearman vs market delta", "Top/bottom 10% spread"],
            [
                [
                    label,
                    _num(part["correlations"]["market_delta"]["spearman"], 4),  # type: ignore[index]
                    _pct(part["tail_10pct"]["observed"]),  # type: ignore[index]
                ]
                for label, part in (
                    (f"< {held['split_year']}", held["in_sample"]),  # type: ignore[index]
                    (f"≥ {held['split_year']}", held["held_out"]),  # type: ignore[index]
                )
            ],
        ),
        "",
    ]
    return lines


def write_report(payload: Mapping[str, object], directory: Path) -> list[Path]:
    """Write the analysis artefacts into `directory`, creating it if needed."""
    directory.mkdir(parents=True, exist_ok=True)
    written = [
        _write(directory / "results.json", results_json(payload)),
        _write(directory / "report.md", build_markdown(payload)),
    ]
    return written


def _write(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    logger.info("wrote %s (%d bytes)", path, len(content.encode("utf-8")))
    return path
