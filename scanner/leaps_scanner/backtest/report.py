"""§8's deliverables: `report.md`, `results.json` and the equity-curve SVGs.

Three artefacts from one run, written under `docs/backtest/<run-date>/`.

**The banner is the load-bearing part.** §1's text travels word for word into
both the human report and `results.json`'s required top-level `banner` field,
and §8 makes the web build fail without it, so the page cannot render a metric
without the caveat that says what the metric does and does not mean. `BANNER`
below is that string; `tests/test_backtest_report.py` asserts it still matches
SPEC-BACKTEST.md §1 character for character, which is what keeps "word for word"
true as the spec evolves rather than merely true on the day it was typed.

One judgment call, stated rather than buried: §1 ends with a sentence *about* the
banner ("The report and the `/backtest` page must carry this section's text…").
That instruction is not a claim about the backtest, and reproducing it inside the
banner would print an editorial note where a caveat belongs. The banner is §1's
claims — everything up to that sentence — and the test pins exactly that span.

**Floats are rounded on the way out, deliberately.** Once `exp`/`log`/`erf` are
in the arithmetic, an x86-64 machine and an arm64 one disagree in the last ULP,
so a committed `results.json` regenerated on a different laptop would diff on
values that are numerically identical. Every float here is rounded — money to the
cent, everything else to `JSON_FLOAT_PLACES` — which makes those diffs vanish in
practice without pretending to more precision than the model has. It reduces the
noise rather than eliminating it: a value sitting exactly on a rounding boundary
can still straddle it. Acceptance 1 is unaffected either way, being about two
runs from one cache on one machine, which are bit-identical before rounding.

NaN and infinity are rejected rather than written: `json.dumps` emits bare `NaN`
and `Infinity` tokens that are not JSON at all, and a consumer that rejects them
(as the web build's parser does) would fail on a file that looked fine locally.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from leaps_scanner.backtest import data, metrics, sleeve

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# Rounding applied to every float written to `results.json` — see the module
# docstring. Money is rounded to the cent first; 9 places is what B3's golden
# file settled on for the same reason.
JSON_FLOAT_PLACES = 9
MONEY_PLACES = 2

# SPEC-BACKTEST.md §1, verbatim. Pinned by test, not by care.
BANNER = """\
**It evaluates:** the §4 timing signal — daily trend template plus weekly slow stochastic —
as an entry/exit discipline on S&P 500 members, and approximately what that timing is worth
when expressed through a ~1-year 0.70Δ call under stated assumptions.

**It does not evaluate:**
- the quality, valuation, IV-rank, or earnings-distance filters (no point-in-time data);
- the preset liquidity gates (spread %, OI — no historical chains);
- the live screener's ranking/score (§6 needs fundamentals);
- real option fills, IV dynamics, or early-exercise/assignment effects.

Therefore: **a good backtest result validates the timing component only.** It is evidence
about two of the five filters. The live strategy could still underperform (bad fundamental
filters, unmodeled IV crush) or outperform (the untested filters may add value)."""

# §7's register. The table is the spec's; the `note` column is where a run's own
# measured values land, which is what "values updated per run where applicable"
# asks for. Rows 7 and 8a carry this milestone's amendments.
BIAS_REGISTER: tuple[dict[str, str], ...] = (
    {
        "row": "1",
        "assumption": (
            "Residual survivorship: delisted members with no yfinance data are unpriceable"
        ),
        "direction": "Flatters (missing names skew toward failures)",
        "note": "Coverage ratio and the uncovered list are reported in full (§2.4)",
    },
    {
        "row": "2",
        "assumption": "Flat IV over each trade",
        "direction": (
            "Likely flatters (entries follow pullbacks, when true IV is typically elevated)"
        ),
        "note": "Stated; sensitivity on σ level only — path dynamics are out of scope",
    },
    {
        "row": "3",
        "assumption": "σ = 1.1 × RV252",
        "direction": "Unknown",
        "note": "Sensitivity at m ∈ {1.0, 1.2} (§5.6)",
    },
    {
        "row": "4",
        "assumption": "No liquidity gates; fills at model value ± h",
        "direction": "Flatters (some trades untradeable at modeled prices)",
        "note": "h haircut plus sensitivity at h ∈ {0.00, 0.06}; non-comparability banner",
    },
    {
        "row": "5",
        "assumption": "Continuous strike (no grid)",
        "direction": "Neutral/minor",
        "note": "—",
    },
    {
        "row": "6",
        "assumption": "r and q flat per trade",
        "direction": "Minor",
        "note": "—",
    },
    {
        "row": "7",
        "assumption": (
            "Current sector labels applied historically, and 208 of 711 point-in-time "
            "members have none"
        ),
        "direction": "Minor; the unlabelled bucket tightens rather than loosens the cap",
        "note": (
            "Affects only the 2-per-sector cap. §3.6's source covers current members only; "
            "RC's call (2026-08-16) buckets unlabelled names as `Unknown`, so the cap binds "
            "across unrelated companies — conservative by construction. The count of "
            "unlabelled names that actually traded is reported with the sleeve"
        ),
    },
    {
        "row": "8",
        "assumption": "Membership CSV errors possible",
        "direction": "Unknown",
        "note": "Source and retrieval date recorded in the CSV header",
    },
    {
        "row": "8a",
        "assumption": "Rename effective dates and price aliases (§2.3a) are hand-sourced",
        "direction": "Unknown — a wrong alias splices two securities into one series",
        "note": (
            "Each pair carries its source in the compiler; alias is span-scoped and unit-tested"
        ),
    },
    {
        "row": "9",
        "assumption": "Earnings-distance gate not simulated",
        "direction": "Unknown (the backtest enters where live Strict/Balanced would wait)",
        "note": "Stated",
    },
    {
        "row": "10",
        "assumption": "Stock-track returns exclude dividends",
        "direction": "Hurts the stock track slightly vs. total-return intuition",
        "note": "The vehicle is a call; benchmarks are labeled with their basis",
    },
    {
        "row": "11",
        "assumption": "Cash earns 0% in the sleeve",
        "direction": "Hurts the sleeve",
        "note": "Conservative by construction",
    },
    {
        "row": "12",
        "assumption": "Quality/valuation/IV filters untested",
        "direction": "Unknown — the live strategy may differ in either direction",
        "note": "§1 banner (§8's `banner` field)",
    },
)


# ---------------------------------------------------------------------------
# Serialization: rounding, and the NaN gate.
# ---------------------------------------------------------------------------


def _money(value: float | None) -> float | None:
    return None if value is None else round(float(value), MONEY_PLACES)


def _portable(payload: object) -> object:
    """Round every float for cross-machine stability; reject NaN and infinity.

    Raising rather than coercing is the point. A NaN reaching this function means
    an upstream statistic is undefined and was not caught as `None`, and writing
    `null` in its place here would hide the bug in the one file a reader trusts.
    """
    if isinstance(payload, bool) or payload is None or isinstance(payload, str | int):
        return payload
    if isinstance(payload, float):
        if payload != payload or payload in (float("inf"), float("-inf")):
            raise ValueError(f"non-finite float in results.json: {payload!r}")
        return round(payload, JSON_FLOAT_PLACES)
    if isinstance(payload, Mapping):
        return {key: _portable(value) for key, value in payload.items()}
    if isinstance(payload, Sequence):
        return [_portable(item) for item in payload]
    raise TypeError(f"not JSON-serializable: {type(payload).__name__}")


def results_json(payload: Mapping[str, object]) -> str:
    """Stable serialization — acceptance 1 wants two runs byte-identical."""
    if not str(payload.get("banner") or "").strip():
        # §8: the web build fails without it, so the writer refuses to produce a
        # file the build is guaranteed to reject.
        raise ValueError("results.json requires a non-empty top-level `banner` (§8)")
    return json.dumps(_portable(payload), indent=2, sort_keys=True) + "\n"


# ---------------------------------------------------------------------------
# Formatting helpers for `report.md`.
# ---------------------------------------------------------------------------


def _pct(value: float | None, places: int = 2, *, signed: bool = True) -> str:
    """A percentage, or `n/a`. Never `+0.00%` for a quantity that was not measured."""
    if value is None:
        return "n/a"
    return f"{100 * value:{'+' if signed else ''}.{places}f}%"


def _num(value: float | None, places: int = 2) -> str:
    return "n/a" if value is None else f"{value:.{places}f}"


def _usd(value: float | None) -> str:
    return "n/a" if value is None else f"${value:,.2f}"


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join("---" for _ in headers) + "|",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(lines)


def _coverage_warning(ratio: float | None, warning: bool | None) -> str:
    """Acceptance 2's warning, in the form every headline table carries it.

    `None` means coverage was not measured, which is reported as such — it is not
    the same claim as coverage being fine.
    """
    if warning is None:
        return "\n> **Coverage not measured for this run.**\n"
    if not warning:
        return ""
    return (
        f"\n> ⚠️ **Low coverage: {_pct(ratio, 2, signed=False)} of member-weeks.** "
        f"Below the {_pct(data.COVERAGE_WARN_BELOW, 0, signed=False)} floor for a "
        "clean read — every figure in this table is computed over a thinned universe.\n"
    )


# ---------------------------------------------------------------------------
# The equity-curve SVGs.
# ---------------------------------------------------------------------------

SVG_WIDTH = 900
SVG_HEIGHT = 380
SVG_PAD_LEFT = 78
SVG_PAD_RIGHT = 210
SVG_PAD_TOP = 24
SVG_PAD_BOTTOM = 40

# Colour-blind-safe, and distinguishable when printed grey.
SERIES_COLOURS = ("#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#ff7f0e")


@dataclass(frozen=True)
class Series:
    """One labelled line on a chart."""

    label: str
    dates: Sequence[date]
    values: Sequence[float]


def _svg_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def equity_svg(series: Sequence[Series], title: str, *, money: bool) -> str:
    """A hand-rolled line chart — no plotting dependency for three static files.

    The report's SVGs exist for `report.md` alone; §8 charts the web page from
    the JSON with lightweight-charts, so this needs to be legible, not
    interactive. Every series shares one x-axis of calendar dates and one y-axis,
    which is only meaningful because the caller normalizes mixed units before
    passing them in.
    """
    drawn = [item for item in series if item.dates and item.values]
    if not drawn:
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {SVG_WIDTH} 80" '
            f'width="{SVG_WIDTH}" height="80"><text x="12" y="44" font-family="sans-serif" '
            f'font-size="14">{_svg_escape(title)}: no data</text></svg>\n'
        )

    first = min(item.dates[0] for item in drawn)
    last = max(item.dates[-1] for item in drawn)
    span = max((last - first).days, 1)
    low = min(min(item.values) for item in drawn)
    high = max(max(item.values) for item in drawn)
    if high <= low:
        high = low + 1.0

    plot_width = SVG_WIDTH - SVG_PAD_LEFT - SVG_PAD_RIGHT
    plot_height = SVG_HEIGHT - SVG_PAD_TOP - SVG_PAD_BOTTOM

    def x_of(day: date) -> float:
        return SVG_PAD_LEFT + plot_width * (day - first).days / span

    def y_of(value: float) -> float:
        return SVG_PAD_TOP + plot_height * (1.0 - (value - low) / (high - low))

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {SVG_WIDTH} {SVG_HEIGHT}" '
        f'width="{SVG_WIDTH}" height="{SVG_HEIGHT}" role="img" '
        f'aria-label="{_svg_escape(title)}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        f'<text x="{SVG_PAD_LEFT}" y="16" font-family="sans-serif" font-size="13" '
        f'fill="#111111">{_svg_escape(title)}</text>',
    ]

    # Horizontal gridlines with value labels.
    for step in range(5):
        value = low + (high - low) * step / 4
        y = y_of(value)
        label = f"${value:,.0f}" if money else f"{value:.2f}×"
        parts.append(
            f'<line x1="{SVG_PAD_LEFT}" y1="{y:.1f}" x2="{SVG_PAD_LEFT + plot_width}" '
            f'y2="{y:.1f}" stroke="#e6e6e6" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{SVG_PAD_LEFT - 8}" y="{y + 4:.1f}" text-anchor="end" '
            f'font-family="sans-serif" font-size="11" fill="#555555">{label}</text>'
        )

    # Year ticks along the bottom.
    for year in range(first.year, last.year + 1):
        tick = date(year, 1, 1)
        if not (first <= tick <= last):
            continue
        x = x_of(tick)
        parts.append(
            f'<line x1="{x:.1f}" y1="{SVG_PAD_TOP}" x2="{x:.1f}" '
            f'y2="{SVG_PAD_TOP + plot_height}" stroke="#f0f0f0" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{x:.1f}" y="{SVG_HEIGHT - 16}" text-anchor="middle" '
            f'font-family="sans-serif" font-size="11" fill="#555555">{year}</text>'
        )

    for index, item in enumerate(drawn):
        colour = SERIES_COLOURS[index % len(SERIES_COLOURS)]
        points = " ".join(
            f"{x_of(day):.1f},{y_of(value):.1f}"
            for day, value in zip(item.dates, item.values, strict=True)
        )
        parts.append(
            f'<polyline fill="none" stroke="{colour}" stroke-width="1.6" points="{points}"/>'
        )
        legend_y = SVG_PAD_TOP + 14 + index * 18
        parts.append(
            f'<line x1="{SVG_WIDTH - SVG_PAD_RIGHT + 12}" y1="{legend_y - 4}" '
            f'x2="{SVG_WIDTH - SVG_PAD_RIGHT + 32}" y2="{legend_y - 4}" '
            f'stroke="{colour}" stroke-width="2.4"/>'
        )
        parts.append(
            f'<text x="{SVG_WIDTH - SVG_PAD_RIGHT + 38}" y="{legend_y}" '
            f'font-family="sans-serif" font-size="11" fill="#111111">'
            f"{_svg_escape(item.label)}</text>"
        )

    parts.append("</svg>")
    return "\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# The run's inputs, and the three artefacts built from them.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunInputs:
    """Everything a report is rendered from — one run, already computed.

    The report generator deliberately computes nothing itself. Every figure it
    prints came from `metrics`, `sleeve` or `data`, so there is exactly one
    implementation of each statistic and no chance of the human report and the
    machine JSON disagreeing about what the run found.
    """

    run_date: date
    window: data.Window
    coverage: data.Coverage
    stats: Sequence[metrics.TrackAStats]
    sleeves: Sequence[sleeve.SleeveResult]
    benchmarks: Sequence[metrics.BenchmarkResult]
    # The session calendar every curve is reported against — see `_curve_axis`.
    calendar: Sequence[date] = ()
    unlabelled_traded: int = 0

    def _by_track(self, track: str) -> list[metrics.TrackAStats]:
        return [item for item in self.stats if item.track == track]

    @property
    def stock_stats(self) -> list[metrics.TrackAStats]:
        return self._by_track(metrics.STOCK_TRACK)

    @property
    def overlay_stats(self) -> list[metrics.TrackAStats]:
        return self._by_track(metrics.OVERLAY_TRACK)

    def headline(self, track: str) -> metrics.TrackAStats | None:
        """The `base` variant — P13 keeps Strict a variant, never the headline."""
        rows = self._by_track(track)
        for item in rows:
            if item.variant == "base":
                return item
        return rows[0] if rows else None

    def benchmark(self, name: str) -> metrics.BenchmarkResult | None:
        for item in self.benchmarks:
            if item.name == name:
                return item
        return None


def _curve_axis(inputs: RunInputs) -> list[date]:
    """The one date axis every curve in the payload is reported against.

    The sleeve marks daily on the session calendar and §6.3's curves are built
    over the same sessions, so all four series already share an axis in practice.
    Writing it once and giving each series a bare value array — rather than
    repeating a date beside every point — is what keeps `results.json` a few
    hundred kilobytes instead of a megabyte, and the page that charts it from
    shipping four copies of the same 2,500 dates to the browser.
    """
    if inputs.calendar:
        return list(inputs.calendar)
    for item in inputs.sleeves:
        if item.curve_dates:
            return list(item.curve_dates)
    for item in inputs.benchmarks:
        if item.curve_dates:
            return list(item.curve_dates)
    return []


def _aligned(
    axis: Sequence[date], dates: Sequence[date], values: Sequence[float]
) -> list[float | None]:
    """Sample a curve onto the shared axis, carrying the last value forward.

    Forward-fill is the right reading for an equity curve: a session the series
    has no point for is a session on which its value did not change. Before the
    series begins there is nothing to carry, so the sample is `None` — not zero,
    and not the first value back-filled, either of which would draw a line
    through a period the series did not exist for.
    """
    lookup = dict(zip(dates, values, strict=True))
    samples: list[float | None] = []
    carried: float | None = None
    for day in axis:
        value = lookup.get(day)
        if value is not None:
            carried = value
        samples.append(carried)
    return samples


def build_results(inputs: RunInputs) -> dict:
    """§8's machine-readable payload.

    Trade-level records are *not* included: six configurations × ~6,000 trades
    would put tens of megabytes of derived rows in a public repo for a file whose
    job is to feed one web page. The sleeve's funded positions are here — a few
    hundred rows, and the only place they exist at all — and the full trade lists
    stay behind `--trades-json`, which writes outside the committed directory.
    """
    axis = _curve_axis(inputs)
    return {
        "schema_version": SCHEMA_VERSION,
        # §8: required, non-empty. The web build fails without it.
        "banner": BANNER,
        "run_date": inputs.run_date.isoformat(),
        "window": {
            "start": inputs.window.start.isoformat(),
            "end": inputs.window.end.isoformat(),
            "fetch_start": inputs.window.fetch_start.isoformat(),
        },
        "coverage": inputs.coverage.as_dict(),
        "track_a": {
            track: {item.variant: item.as_dict() for item in inputs._by_track(track)}
            for track in (metrics.STOCK_TRACK, metrics.OVERLAY_TRACK)
        },
        # `sort_keys=True` makes the file byte-stable but alphabetizes the
        # variants, which loses §5.6's ordering — the grid reads as one parameter
        # moved at a time around the base case, and `h0.00, h0.06, m1.0, m1.2`
        # does not. Presentation order is therefore stated rather than inferred.
        "track_a_order": {
            track: [item.variant for item in inputs._by_track(track)]
            for track in (metrics.STOCK_TRACK, metrics.OVERLAY_TRACK)
        },
        "sleeves": [item.as_dict() for item in inputs.sleeves],
        # One shared date axis, then one bare value array per series — see
        # `_curve_axis`. Sleeve curves are in dollars, benchmark curves are
        # normalized to 1.0 at each series' first point.
        "curve_dates": [day.isoformat() for day in axis],
        "sleeve_curves": {
            item.name: [
                _money(value) for value in _aligned(axis, item.curve_dates, item.curve_equity)
            ]
            for item in inputs.sleeves
        },
        "sleeve_positions": {
            item.name: [position.as_dict() for position in item.positions]
            for item in inputs.sleeves
        },
        "benchmarks": [item.as_dict() for item in inputs.benchmarks],
        "benchmark_curves": {
            item.name: _aligned(axis, item.curve_dates, item.curve_values)
            for item in inputs.benchmarks
        },
        "bias_register": [dict(row) for row in BIAS_REGISTER],
        "notes": {
            "sector_labels_unlabelled_traded": inputs.unlabelled_traded,
            "sector_unknown_bucket": sleeve.SECTOR_UNKNOWN,
            "exposure_caveat": metrics.EXPOSURE_CAVEAT,
        },
    }


def _track_a_rows(rows: Sequence[metrics.TrackAStats]) -> str:
    return _table(
        (
            "Variant",
            "Trades",
            "Names",
            "Win rate",
            "Mean",
            "Median",
            "Profit factor",
            "p5",
            "p95",
            "Market delta",
        ),
        [
            (
                item.variant,
                str(item.trades),
                str(item.chains),
                _pct(item.win_rate, 1, signed=False),
                _pct(item.mean_return),
                _pct(item.median_return),
                _num(item.profit_factor),
                _pct(item.return_percentiles.get("p5")),
                _pct(item.return_percentiles.get("p95")),
                (
                    _pct(item.market_delta.get("mean"))
                    if item.market_delta.get("mean") is not None
                    else f"n/a ({item.market_delta_unpriced} unpriced)"
                ),
            )
            for item in rows
        ],
    )


def _sensitivity_table(rows: Sequence[metrics.TrackAStats]) -> str:
    """§5.6's grid — acceptance 6 requires it in every generated report."""
    return _table(
        ("Config", "Zone", "m", "h", "Trades", "Win rate", "Mean", "Median", "PF", "Vehicle alpha"),
        [
            (
                item.variant,
                str((item.parameters or {}).get("zone", "—")),
                _num(float((item.parameters or {}).get("sigma_multiplier", 0)) or None, 2),
                _num(float((item.parameters or {}).get("friction", 0)), 2),
                str(item.trades),
                _pct(item.win_rate, 1, signed=False),
                _pct(item.mean_return),
                _pct(item.median_return),
                _num(item.profit_factor),
                _pct((item.vehicle_alpha or {}).get("mean")),
            )
            for item in rows
        ],
    )


def _exit_reason_table(item: metrics.TrackAStats) -> str:
    if not item.exit_reasons:
        return "_No trades._"
    total = sum(item.exit_reasons.values())
    return _table(
        ("Exit reason", "Trades", "Share"),
        [
            (reason, str(count), _pct(count / total, 1, signed=False))
            for reason, count in sorted(item.exit_reasons.items(), key=lambda pair: -pair[1])
        ],
    )


def _sleeve_section(item: sleeve.SleeveResult) -> str:
    counts = item.skipped_counts
    skipped_total = sum(counts.values())
    body = [
        f"### {item.name}",
        "",
        _coverage_warning(item.coverage_ratio, item.low_coverage_warning),
        _table(
            ("Metric", "Value"),
            [
                ("Start equity", _usd(item.start_equity)),
                ("Final equity", _usd(item.final_equity)),
                ("Total return", _pct(item.total_return)),
                ("CAGR", _pct(item.cagr)),
                (
                    "Max drawdown (daily equity)",
                    f"{_pct(item.max_drawdown, 2, signed=False)}"
                    + (f" on {item.max_drawdown_date}" if item.max_drawdown_date else ""),
                ),
                ("Positions funded", str(len(item.positions))),
                ("Distinct names", str(len({position.chain for position in item.positions}))),
                ("Win rate", _pct(item.win_rate, 1, signed=False)),
                ("Average open positions", _num(item.average_open_positions)),
                ("Average premium exposure", _pct(item.average_premium_exposure, 2, signed=False)),
                ("Circuit-breaker activations", str(len(item.breaker_dates))),
            ],
        ),
        "",
        f"**Entries declined** ({skipped_total:,} of "
        f"{skipped_total + len(item.positions):,} available signals):",
        "",
        _table(
            ("Cause", "Count", "Share of declines"),
            [
                (
                    reason,
                    f"{count:,}",
                    _pct(count / skipped_total, 1, signed=False) if skipped_total else "n/a",
                )
                for reason, count in counts.items()
            ],
        ),
        "",
        "**Calendar-year returns** (the first and last years are partial — the window "
        "starts and ends mid-year, and a stub year is reported as it happened rather "
        "than annualized):",
        "",
        _table(
            ("Year", "Return"),
            [(year, _pct(value)) for year, value in item.calendar_year_returns.items()],
        ),
    ]
    if item.breaker_dates:
        shown = ", ".join(day.isoformat() for day in item.breaker_dates[:12])
        more = "" if len(item.breaker_dates) <= 12 else f" (+{len(item.breaker_dates) - 12} more)"
        body.extend(["", f"**Circuit breaker tripped on:** {shown}{more}"])
    return "\n".join(body)


def _benchmark_table(benchmarks: Sequence[metrics.BenchmarkResult]) -> str:
    return _table(
        ("Benchmark", "Total return", "CAGR", "Max drawdown", "Constituents"),
        [
            (
                item.label,
                _pct(item.total_return),
                _pct(item.cagr),
                _pct(item.max_drawdown, 2, signed=False)
                + (f" on {item.max_drawdown_date}" if item.max_drawdown_date else ""),
                "—" if item.constituents is None else f"{item.constituents:,}",
            )
            for item in benchmarks
        ],
    )


def build_markdown(inputs: RunInputs) -> str:
    """§8's human report: the banner, every §6 table, §7's register, §5.6's grid.

    Acceptance 6 is satisfied structurally rather than by inspection — the banner,
    the bias register and the sensitivity table are unconditional sections here,
    so a report that omits one cannot be generated.
    """
    coverage = inputs.coverage
    warning = _coverage_warning(round(coverage.ratio, 6), coverage.low_coverage_warning)
    stock = inputs.headline(metrics.STOCK_TRACK)
    overlay = inputs.headline(metrics.OVERLAY_TRACK)
    spy = inputs.benchmark("spy_total_return")

    sections: list[str] = [
        f"# LEAPS Finder backtest — {inputs.run_date.isoformat()}",
        "",
        f"Evaluation window **{inputs.window.start} → {inputs.window.end}** "
        f"(data fetched from {inputs.window.fetch_start} for warm-up). "
        f"Cache snapshot: {coverage.snapshot_date or 'unrecorded'}. "
        f"Run status: **{coverage.status}**.",
        "",
        "## What this backtest can and cannot claim",
        "",
        BANNER,
        "",
        "---",
        "",
        "## Headline",
        "",
        _headline_prose(stock, overlay, inputs.sleeves, spy),
        "",
        "---",
        "",
        "## 1. Coverage (§2.4)",
        "",
        warning,
        _table(
            ("Metric", "Value"),
            [
                ("Member-weeks covered", f"{coverage.covered_member_weeks:,}"),
                ("Member-weeks total", f"{coverage.total_member_weeks:,}"),
                ("Coverage ratio", _pct(coverage.ratio, 2, signed=False)),
                ("Point-in-time members", f"{coverage.members:,}"),
                ("Members with no data at all", f"{coverage.no_data_members:,}"),
                ("Symbols with incomplete coverage", f"{len(coverage.uncovered):,}"),
            ],
        ),
        "",
        "Auxiliary series: "
        + ", ".join(
            f"`{symbol}` {'cached' if cached else '**missing**'}"
            for symbol, cached in coverage.auxiliary
        )
        + ".",
        "",
        "---",
        "",
        "## 2. Track A — per-trade (§6.1)",
        "",
        "Every §4.2 signal counted independently (P8: one open trade per name per "
        "track). This is the primary result; the sleeve below is secondary.",
        "",
        warning,
        "### Stock track",
        "",
        _track_a_rows(inputs.stock_stats),
        "",
        "### LEAP overlay",
        "",
        "An approximation layer, not the live screener: a synthesized 0.70Δ strike, "
        "no liquidity gates, flat IV. `base` is the headline configuration "
        "(m = 1.1, h = 0.04); the rest are §5.6's sensitivities.",
        "",
        _track_a_rows(inputs.overlay_stats),
        "",
        "### Exit reasons",
        "",
        "**Stock track (base):**",
        "",
        _exit_reason_table(stock) if stock else "_No trades._",
        "",
        "**LEAP overlay (base):**",
        "",
        _exit_reason_table(overlay) if overlay else "_No trades._",
        "",
        "---",
        "",
        "## 3. Sensitivity grid (§5.6)",
        "",
        "One parameter moved at a time around the base case. The §4 signals and the "
        "stock track are computed once and shared across all six replays (§5.6's "
        "scope rule), so every row differs only in the vehicle.",
        "",
        warning,
        _sensitivity_table(inputs.overlay_stats),
        "",
        "---",
        "",
        "## 4. Track B — sleeve simulation (§6.2)",
        "",
        "SPEC.md §7's discipline applied to the overlay's base-configuration signals: "
        "3% premium budget per position, integer contracts, at most 5 open and 2 per "
        "sector, open premium at cost capped at 15% of equity, cash at 0%, and the 8% "
        "circuit breaker enforced as a hard 28-day entry ban (§6.2's stated hardening "
        "of v1's advisory banner). Positions are a strict subset of the overlay track's "
        "trades: the sleeve chooses which signals to fund, and each funded position "
        "runs to that trade's own exit.",
        "",
        f"Sector labels cover 503 of the window's 711 point-in-time members; "
        f"**{inputs.unlabelled_traded} of the names that actually traded have no label** "
        f"and share a single `{sleeve.SECTOR_UNKNOWN}` bucket, so the 2-per-sector cap "
        "binds across unrelated companies among them. That tightens the sleeve rather "
        "than loosening it (bias register row 7).",
        "",
    ]

    for item in inputs.sleeves:
        sections.extend([_sleeve_section(item), ""])

    sections.extend(
        [
            "---",
            "",
            "## 5. Benchmarks (§6.3)",
            "",
            warning,
            _benchmark_table(inputs.benchmarks),
            "",
            f"> **{metrics.EXPOSURE_CAVEAT}**",
            "",
            "The equal-weight benchmark holds the distinct entered names over the "
            "**full** evaluation window — each bought at its first eligible session "
            "(§3.1's warm-up rule) and held to the end, a delisted name holding its "
            "last close and then sitting in cash. It is deliberately not a "
            "same-trade-window hold: that quantity is arithmetically the stock track "
            "itself, which is why §0's Decision 7 was amended in review.",
            "",
            "![Sleeve equity](sleeve-equity.svg)",
            "",
            "![Sleeve vs benchmarks](benchmarks.svg)",
            "",
            "---",
            "",
            "## 6. Bias register (§7)",
            "",
            "Every approximation in this backtest, with the direction it is expected "
            "to push the result. Nothing here is a caveat added after the fact — the "
            "register is part of the spec and travels with every run.",
            "",
            _table(
                ("#", "Assumption", "Direction", "Mitigation / note"),
                [
                    (row["row"], row["assumption"], row["direction"], row["note"])
                    for row in BIAS_REGISTER
                ],
            ),
            "",
            "---",
            "",
            "## Reproducing this run",
            "",
            "```bash",
            f"python -m leaps_scanner.backtest --run-date {inputs.run_date.isoformat()} "
            "--report-dir docs/backtest",
            "```",
            "",
            "Runs are deterministic from a given cache (acceptance 1). The price cache "
            "itself is git-ignored — raw Yahoo data is never committed — so a fresh "
            "machine re-fetches before it can reproduce these figures, and a later "
            "snapshot may differ where Yahoo has revised its history.",
            "",
            "---",
            "",
            "*Educational/personal tooling on delayed, unofficial data. This is a "
            "simulation built on the stated approximations above. Past performance — "
            "simulated or otherwise — does not predict future results. "
            "**Nothing here is financial advice.***",
            "",
        ]
    )
    # The coverage warning is an unconditional section that renders to nothing
    # when coverage is fine, which leaves runs of blank lines behind it. Collapsed
    # here rather than made conditional at each call site, so the warning stays
    # impossible to omit by accident (acceptance 2).
    return re.sub(r"\n{3,}", "\n\n", "\n".join(sections))


def _headline_prose(
    stock: metrics.TrackAStats | None,
    overlay: metrics.TrackAStats | None,
    sleeves: Sequence[sleeve.SleeveResult],
    spy: metrics.BenchmarkResult | None,
) -> str:
    """The finding, in the direction the numbers actually point.

    Generated from the run rather than written once, so it cannot drift out of
    agreement with the tables below it. Every comparative word is derived from a
    sign test — there is no fixed adjective waiting to flatter a future run.
    """
    if stock is None or overlay is None:
        return "_This run produced no trades._"

    lines = [
        f"- **Stock signal (base):** {stock.trades:,} trades over {stock.chains:,} names, "
        f"win rate {_pct(stock.win_rate, 1, signed=False)}, mean "
        f"{_pct(stock.mean_return)}, median {_pct(stock.median_return)}, "
        f"profit factor {_num(stock.profit_factor)}.",
        f"- **Through the synthetic LEAP (base, m = 1.1, h = 0.04):** "
        f"{overlay.trades:,} trades, win rate {_pct(overlay.win_rate, 1, signed=False)}, "
        f"mean {_pct(overlay.mean_return)}, median {_pct(overlay.median_return)}, "
        f"profit factor {_num(overlay.profit_factor)}. Mean vehicle alpha "
        f"{_pct((overlay.vehicle_alpha or {}).get('mean'))} — what the option cost or "
        "added against simply owning the shares over the same window.",
    ]

    if overlay.mean_return is not None and stock.mean_return is not None:
        verdict = (
            "The vehicle does not survive the pinned friction: the signal's edge on the "
            "shares is negative through the option."
            if overlay.mean_return < 0 <= stock.mean_return
            else "Both tracks point the same way at the pinned friction."
            if (overlay.mean_return < 0) == (stock.mean_return < 0)
            else "The option track and the share track disagree in sign."
        )
        lines.append(f"- **{verdict}**")

    for item in sleeves:
        lines.append(
            f"- **Sleeve {item.name}:** {_pct(item.total_return)} total "
            f"({_pct(item.cagr)} CAGR), max drawdown "
            f"{_pct(item.max_drawdown, 2, signed=False)}, {len(item.positions):,} "
            f"positions funded of {len(item.skipped) + len(item.positions):,} available "
            f"signals."
        )

    if spy is not None:
        lines.append(
            f"- **{spy.label} over the same window:** {_pct(spy.total_return)} total "
            f"({_pct(spy.cagr)} CAGR), max drawdown "
            f"{_pct(spy.max_drawdown, 2, signed=False)}. See §5's exposure caveat before "
            "reading that as like-for-like."
        )

    if len(sleeves) >= 2:
        funded = [{(p.chain, p.entry_date) for p in item.positions} for item in sleeves]
        common = len(funded[0] & funded[1])
        lines.append(
            f"- **Read the sleeve carefully.** The two starting equities share only "
            f"{common:,} of their {len(funded[0]):,} / {len(funded[1]):,} funded "
            "positions. With five slots against thousands of signals, *which* trades get "
            "funded is decided mostly by arrival order and the P10 tie-break, so the "
            "sleeve's headline figure carries far more sampling noise than Track A's and "
            "should not be read as a second estimate of the signal's edge."
        )

    return "\n".join(lines)


def write_report(inputs: RunInputs, directory: Path) -> list[Path]:
    """Write §8's three artefacts into `directory`, creating it if needed."""
    directory.mkdir(parents=True, exist_ok=True)

    payload = build_results(inputs)
    written = [
        _write(directory / "results.json", results_json(payload)),
        _write(directory / "report.md", build_markdown(inputs)),
        _write(
            directory / "sleeve-equity.svg",
            equity_svg(
                [Series(item.name, item.curve_dates, item.curve_equity) for item in inputs.sleeves],
                "Sleeve equity (§6.2)",
                money=True,
            ),
        ),
        _write(
            directory / "benchmarks.svg",
            equity_svg(
                [
                    *(
                        Series(
                            f"Sleeve {item.name}",
                            item.curve_dates,
                            [
                                value / item.start_equity if item.start_equity else 0.0
                                for value in item.curve_equity
                            ],
                        )
                        for item in inputs.sleeves
                    ),
                    *(
                        Series(item.short_label, item.curve_dates, item.curve_values)
                        for item in inputs.benchmarks
                    ),
                ],
                "Sleeve vs benchmarks, normalized to 1.0 at the window start (§6.3)",
                money=False,
            ),
        ),
    ]
    return written


def _write(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    logger.info("wrote %s (%d bytes)", path, len(content.encode("utf-8")))
    return path
