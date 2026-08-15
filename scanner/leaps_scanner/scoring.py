"""Scoring engine (SPEC.md §6): clip_map, the five subscores, the composite,
IV rank, and the three preset filters. Pure functions only.

All subscores are raw 0-100 and the composite is the pinned weighted sum —
displayed as-is, never curved or rescaled (CLAUDE.md). Where the spec leaves a
gap, the decision is recorded here rather than improvised silently:

* While a symbol's IV history is warming up (< 120 snapshots), the
  cross-sectional percentile of iv30/rv20 stands in for `iv_rank` itself —
  same clip_map, same preset thresholds — so cheap vol keeps scoring high and
  the Strict/Balanced IV gates work from the first scan. (Read literally, §6
  would substitute the raw percentile for the *subscore*, which would reward
  expensive vol; confirmed with RC on 2026-08-15.)
* The standalone checklist booleans `quality_pass` / `iv_pass` use the
  Balanced thresholds (≥ 45, ≤ 50) and `valuation_pass` means positive
  haircut-adjusted upside; the spec's preset table never defines them.
* A missing metric is excluded from its subscore's mean for Quality (§6 says
  so) and Valuation (the same convention). Option economics instead requires
  all five terms — dropping an unknown IV term would let unknown vol outscore
  known-expensive vol. A composite with any missing subscore is null —
  reweighting the remainder would quietly inflate the visible number.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from statistics import fmean

from leaps_scanner.fundamentals import Fundamentals
from leaps_scanner.indicators import Signals
from leaps_scanner.options import ContractEconomics

WEIGHT_TREND = 0.25
WEIGHT_QUALITY = 0.25
WEIGHT_OPTION = 0.20
WEIGHT_VALUATION = 0.15
WEIGHT_ENTRY = 0.15

UPSIDE_HAIRCUT = 0.6

IV_RANK_MIN_SNAPSHOTS = 120
IV_RANK_WINDOW = 252
IV_RANK_STATUS_OK = "ok"
IV_RANK_STATUS_WARMING = "warming_up"

# M3 addendum (see module docstring): thresholds for the standalone checklist
# booleans, aligned with the Balanced preset.
QUALITY_PASS_MIN = 45.0
IV_PASS_MAX = 50.0


def clip_map(x: float, x0: float, x1: float) -> float:
    """Piecewise-linear map of x from [x0, x1] onto [0, 100] (SPEC.md §6)."""
    if x1 <= x0:
        raise ValueError("clip_map needs x1 > x0")
    if x <= x0:
        return 0.0
    if x >= x1:
        return 100.0
    return 100.0 * (x - x0) / (x1 - x0)


def percentile_of(value: float, population: Sequence[float]) -> float:
    """Mean-rank percentile of `value` within `population`, 0-100.

    The population is the scan's cross-section and includes the value itself.
    Ties count half, so identical values sit at 50 rather than 0 or 100.
    """
    if not population:
        raise ValueError("empty population")
    less = sum(1 for x in population if x < value)
    equal = sum(1 for x in population if x == value)
    return 100.0 * (less + 0.5 * equal) / len(population)


def trend_score(signals: Signals) -> float:
    """§6 Trend: mean of three clip_maps, then the extension gate.

    The gate multiplies the mean by clip_map(0.20 − (close/sma50 − 1), 0,
    0.10)/100 — more than 20% above the 50-day zeroes the subscore; parabolic
    is not better.
    """
    base = fmean(
        [
            clip_map(signals.spot / signals.sma200 - 1.0, 0.0, 0.25),
            clip_map(signals.sma50 / signals.sma200 - 1.0, 0.0, 0.10),
            clip_map(signals.share_above_sma50_60d, 0.5, 1.0),
        ]
    )
    extension = signals.spot / signals.sma50 - 1.0
    gate = min(clip_map(0.20 - extension, 0.0, 0.10) / 100.0, 1.0)
    return base * gate


def quality_score(fundamentals: Fundamentals) -> tuple[float | None, bool]:
    """§6 Quality: mean of the available metric maps.

    Returns (score, all_metrics_present); presence feeds the Strict preset.
    Net cash (negative net debt / EBITDA) maps above 3 and therefore to 100.
    """
    parts = []
    if fundamentals.op_margin is not None:
        parts.append(clip_map(fundamentals.op_margin, 0.05, 0.30))
    if fundamentals.roe is not None:
        parts.append(clip_map(fundamentals.roe, 0.08, 0.30))
    if fundamentals.net_debt_ebitda is not None:
        parts.append(clip_map(3.0 - fundamentals.net_debt_ebitda, 0.0, 3.0))
    if fundamentals.rev_growth is not None:
        parts.append(clip_map(fundamentals.rev_growth, 0.0, 0.20))
    if fundamentals.fcf_margin is not None:
        parts.append(clip_map(fundamentals.fcf_margin, 0.0, 0.20))

    return (fmean(parts) if parts else None), len(parts) == 5


def option_score(
    contract: ContractEconomics | None,
    iv30: float | None,
    iv_rank_value: float | None,
) -> float | None:
    """§6 Option economics: the mean of all five pinned terms, or nothing.

    `iv_rank_value` is the real IV rank or, while warming up, the substituted
    iv30/rv20 percentile. Unlike Quality, a missing term here is not excluded
    from the mean: unknown IV averaging over three terms would outscore
    known-expensive IV averaged over five, so a symbol without a contract or
    without IV data has no option subscore at all (and thus no composite).
    """
    if contract is None or iv30 is None or iv_rank_value is None:
        return None

    return fmean(
        [
            clip_map(50.0 - iv_rank_value, 0.0, 50.0),
            clip_map(0.40 - iv30, 0.0, 0.25),
            clip_map(0.30 - contract.cost_pct_spot, 0.0, 0.15),
            clip_map(0.10 - contract.spread_pct, 0.0, 0.08),
            clip_map(contract.oi, 100.0, 2000.0),
        ]
    )


def upside_adjusted(analyst_target: float | None, spot: float) -> float | None:
    """§6: analyst upside with the 40% haircut."""
    if analyst_target is None or spot <= 0:
        return None
    return UPSIDE_HAIRCUT * (analyst_target / spot - 1.0)


def valuation_score(upside_adj: float | None, fwd_pe_percentile: float | None) -> float | None:
    """§6 Valuation: haircut upside and the inverted forward-P/E percentile
    (cheaper ⇒ higher)."""
    parts = []
    if upside_adj is not None:
        parts.append(clip_map(upside_adj, 0.0, 0.25))
    if fwd_pe_percentile is not None:
        parts.append(100.0 - fwd_pe_percentile)
    return fmean(parts) if parts else None


def entry_score(stoch_k: float, weeks_since_cross_up: int | None) -> float:
    """§6 Entry: zone position (peak at slowK 25-45, linear to 0 at 20 and
    70) averaged with cross freshness (100 within 2 weeks, 60 within 4,
    else 30 — never included)."""
    zone = min(clip_map(stoch_k - 20.0, 0.0, 5.0), clip_map(70.0 - stoch_k, 0.0, 25.0))

    if weeks_since_cross_up is not None and weeks_since_cross_up <= 2:
        freshness = 100.0
    elif weeks_since_cross_up is not None and weeks_since_cross_up <= 4:
        freshness = 60.0
    else:
        freshness = 30.0

    return fmean([zone, freshness])


def composite_score(
    s_trend: float | None,
    s_quality: float | None,
    s_option: float | None,
    s_valuation: float | None,
    s_entry: float | None,
) -> float | None:
    """§6 composite with the pinned weights; null if any subscore is null."""
    parts = (s_trend, s_quality, s_option, s_valuation, s_entry)
    if any(part is None for part in parts):
        return None
    return (
        WEIGHT_TREND * s_trend
        + WEIGHT_QUALITY * s_quality
        + WEIGHT_OPTION * s_option
        + WEIGHT_VALUATION * s_valuation
        + WEIGHT_ENTRY * s_entry
    )


def iv_rank(history: Sequence[float], window: int = IV_RANK_WINDOW) -> float | None:
    """§6 IV rank ×100: (current − min)/(max − min) over the trailing window.

    `history` is ordered oldest→newest and includes the current snapshot as
    its last element. A flat window has no defined rank.
    """
    recent = [value for value in history if value is not None][-window:]
    if not recent:
        return None
    low, high = min(recent), max(recent)
    if high <= low:
        return None
    return 100.0 * (recent[-1] - low) / (high - low)


@dataclass(frozen=True)
class PresetFlags:
    """§6 presets: which hard-filter tiers a symbol clears."""

    strict: bool
    balanced: bool
    wide: bool


def preset_flags(
    *,
    trend_pass: bool,
    stoch_k: float,
    turning_up: bool,
    iv_rank_value: float | None,
    earnings_dte: int | None,
    spread_pct: float | None,
    oi: int | None,
    s_quality: float | None,
    quality_all_present: bool,
) -> PresetFlags:
    """Apply the §6 preset table.

    `iv_rank_value` is the real rank or the warming-up substitute — the
    thresholds apply to whichever is in force. A gate whose input is missing
    fails: unknown earnings distance or an unverifiable IV rank cannot clear
    Strict/Balanced, and a symbol with no contract has no spread or OI to
    clear any tier with.
    """
    liquidity_known = spread_pct is not None and oi is not None
    iv_known = iv_rank_value is not None
    earnings_known = earnings_dte is not None

    strict = (
        trend_pass
        and 20.0 <= stoch_k <= 55.0
        and turning_up
        and iv_known
        and iv_rank_value <= 30.0
        and earnings_known
        and earnings_dte >= 30
        and liquidity_known
        and spread_pct <= 0.05
        and oi >= 500
        and quality_all_present
        and s_quality is not None
        and s_quality >= 60.0
    )
    balanced = (
        trend_pass
        and 20.0 <= stoch_k <= 70.0
        and turning_up
        and iv_known
        and iv_rank_value <= 50.0
        and earnings_known
        and earnings_dte >= 14
        and liquidity_known
        and spread_pct <= 0.08
        and oi >= 200
        and s_quality is not None
        and s_quality >= 45.0
    )
    wide = (
        trend_pass
        and 10.0 <= stoch_k <= 80.0
        and liquidity_known
        and spread_pct <= 0.12
        and oi >= 100
    )
    return PresetFlags(strict=strict, balanced=balanced, wide=wide)
