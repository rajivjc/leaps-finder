"""B4 tests: SPEC-BACKTEST.md §6.3, §8 and §9, acceptance criteria 1, 2 and 6.

The one that matters most is `TestBanner::test_the_banner_is_spec_1_word_for_word`.
§1 says its text travels "word for word" into the report and into
`results.json`'s `banner` field, and `report.BANNER` is a copy — so without a
test the guarantee lasts exactly until someone edits the spec. This extracts §1
from `SPEC-BACKTEST.md` and compares, which turns "word for word" from a promise
into a build failure.
"""

from __future__ import annotations

import json
import math
import re
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from test_backtest_engine import business_days, price_frame
from test_backtest_sleeve import WINDOW, flat_cache, make_trade, run_sleeve

from leaps_scanner.backtest import data, metrics, report, synthetic

SPEC = Path(__file__).resolve().parents[2] / "SPEC-BACKTEST.md"


def spec_section_one() -> str:
    """§1's claims, extracted from the spec.

    Everything between the §1 heading and the sentence that *instructs* the
    report to carry the section — that instruction is a rule about the banner,
    not a claim the banner should repeat. `report.py`'s docstring states the same
    reading; this is where it is enforced.
    """
    text = SPEC.read_text(encoding="utf-8")
    body = text.split("## 1. What this backtest can and cannot claim", 1)[1]
    body = body.split("\n---", 1)[0]
    # The spec is hard-wrapped, so the instruction's own words straddle a line
    # break — matched on whitespace rather than a literal, which a reflow of the
    # paragraph would otherwise silently defeat.
    parts = re.split(
        r"\s*The\s+report\s+and\s+the\s+`/backtest`\s+page\s+must\s+carry", body, maxsplit=1
    )
    assert len(parts) == 2, "§1 no longer ends with the instruction the banner excludes"
    return parts[0].strip()


class TestBanner:
    def test_the_banner_is_spec_1_word_for_word(self) -> None:
        assert spec_section_one() == report.BANNER

    def test_the_banner_is_not_empty(self) -> None:
        assert report.BANNER.strip()

    def test_results_json_refuses_to_write_without_a_banner(self) -> None:
        """§8's gate on the writing side, matching the web build's on the reading side."""
        with pytest.raises(ValueError, match="banner"):
            report.results_json({"schema_version": 1})
        with pytest.raises(ValueError, match="banner"):
            report.results_json({"schema_version": 1, "banner": "   "})

    def test_the_report_carries_the_banner_the_register_and_the_grid(self, tmp_path: Path) -> None:
        """Acceptance 6, on the markdown side."""
        markdown = report.build_markdown(sample_inputs(tmp_path))
        assert report.BANNER in markdown
        # §7's register: every row, by number.
        for row in report.BIAS_REGISTER:
            assert f"| {row['row']} |" in markdown
        assert "## 3. Sensitivity grid (§5.6)" in markdown
        # The banner precedes any metric.
        assert markdown.index(report.BANNER) < markdown.index("## 1. Coverage")


def sample_inputs(tmp_path: Path) -> report.RunInputs:
    """A tiny but complete run, enough to render every section."""
    trades = [make_trade("AAA", date(2021, 3, 1), date(2021, 9, 1), symbol="AAA")]
    cache = flat_cache(tmp_path, ["AAA"])
    coverage = data.Coverage(
        window=WINDOW,
        run_date=date(2022, 12, 31),
        snapshot_date="2022-12-31",
        total_member_weeks=1000,
        covered_member_weeks=950,
        members=4,
        no_data_members=1,
        auxiliary=(("SPY", True), ("^IRX", True)),
    )
    benchmark = metrics.BenchmarkPrices(cache.load_benchmark())
    base_config = synthetic.CONFIGS[0]
    assert base_config.name == "base", "the headline row is §5.6's base configuration"
    stats = [
        metrics.track_a("base", [trades[0].trade], [], benchmark, coverage=coverage),
        metrics.track_a_overlay(base_config, trades, [], benchmark, coverage=coverage),
    ]
    result = run_sleeve(tmp_path / "sleeve", trades)
    spy = metrics.spy_buy_and_hold(cache, WINDOW, coverage=coverage)
    return report.RunInputs(
        run_date=date(2022, 12, 31),
        window=WINDOW,
        coverage=coverage,
        stats=stats,
        sleeves=[result],
        benchmarks=[spy] if spy else [],
        calendar=result.curve_dates,
        unlabelled_traded=1,
    )


class TestResultsJson:
    def test_it_is_strict_json_with_no_nan_tokens(self, tmp_path: Path) -> None:
        """Trap: `json.dumps` writes a bare `NaN` that is not JSON at all.

        Parsed here with a `parse_constant` that raises, which is what every
        strict consumer — including the web build's `JSON.parse` — does.
        """
        rendered = report.results_json(report.build_results(sample_inputs(tmp_path)))

        def reject(token: str) -> object:
            raise AssertionError(f"non-JSON constant in results.json: {token}")

        parsed = json.loads(rendered, parse_constant=reject)
        assert parsed["banner"] == report.BANNER
        assert parsed["schema_version"] == report.SCHEMA_VERSION

    def test_a_non_finite_float_is_rejected_rather_than_written(self) -> None:
        for bad in (math.nan, math.inf, -math.inf):
            with pytest.raises(ValueError, match="non-finite"):
                report.results_json({"banner": "x", "value": bad})

    def test_two_renders_of_one_run_are_byte_identical(self, tmp_path: Path) -> None:
        """Acceptance 1."""
        inputs = sample_inputs(tmp_path)
        first = report.results_json(report.build_results(inputs))
        second = report.results_json(report.build_results(inputs))
        assert first == second

    def test_floats_are_rounded_to_the_declared_precision(self) -> None:
        rendered = report.results_json({"banner": "x", "value": 1 / 3})
        assert json.loads(rendered)["value"] == round(1 / 3, report.JSON_FLOAT_PLACES)

    def test_curves_share_one_date_axis(self, tmp_path: Path) -> None:
        payload = report.build_results(sample_inputs(tmp_path))
        axis = payload["curve_dates"]
        assert axis, "a run with a sleeve has a curve"
        for values in payload["sleeve_curves"].values():
            assert len(values) == len(axis)
        for values in payload["benchmark_curves"].values():
            assert len(values) == len(axis)

    def test_a_curve_that_starts_late_is_null_before_it_starts(self) -> None:
        axis = [date(2021, 1, 1), date(2021, 1, 2), date(2021, 1, 3), date(2021, 1, 4)]
        aligned = report._aligned(axis, [date(2021, 1, 3)], [2.0])
        # Not zero, and not the first value back-filled: the series did not exist.
        assert aligned == [None, None, 2.0, 2.0]

    def test_the_written_files_are_the_four_deliverables(self, tmp_path: Path) -> None:
        written = report.write_report(sample_inputs(tmp_path), tmp_path / "out")
        assert sorted(path.name for path in written) == [
            "benchmarks.svg",
            "report.md",
            "results.json",
            "sleeve-equity.svg",
        ]
        for path in written:
            assert path.read_text(encoding="utf-8").strip()


class TestSvg:
    def test_it_renders_a_polyline_per_series_inside_the_plot(self) -> None:
        days = [date(2021, 1, 1) + timedelta(days=index) for index in range(10)]
        svg = report.equity_svg(
            [
                report.Series("A", days, [100.0 + index for index in range(10)]),
                report.Series("B", days, [100.0 - index for index in range(10)]),
            ],
            "Test",
            money=True,
        )
        assert svg.count("<polyline") == 2
        assert "A</text>" in svg and "B</text>" in svg
        coordinates = [
            tuple(float(part) for part in pair.split(","))
            for pair in re.findall(r'points="([^"]+)"', svg)[0].split()
        ]
        for x, y in coordinates:
            assert report.SVG_PAD_LEFT <= x <= report.SVG_WIDTH - report.SVG_PAD_RIGHT
            assert report.SVG_PAD_TOP <= y <= report.SVG_HEIGHT - report.SVG_PAD_BOTTOM

    def test_an_empty_series_list_renders_a_readable_placeholder(self) -> None:
        svg = report.equity_svg([], "Nothing", money=False)
        assert "no data" in svg and svg.startswith("<svg")

    def test_a_label_with_markup_characters_is_escaped(self) -> None:
        days = [date(2021, 1, 1), date(2021, 1, 2)]
        svg = report.equity_svg([report.Series("<A & B>", days, [1.0, 2.0])], "T", money=False)
        assert "&lt;A &amp; B&gt;" in svg
        assert "<A & B>" not in svg


class TestCurveArithmetic:
    def test_max_drawdown_is_the_deepest_peak_to_trough(self) -> None:
        days = [date(2021, 1, 1) + timedelta(days=index) for index in range(5)]
        drop, when = metrics.max_drawdown(days, [100.0, 120.0, 60.0, 90.0, 130.0])
        assert drop == pytest.approx(0.5)  # 120 → 60
        assert when == days[2]

    def test_an_empty_curve_has_no_drawdown_rather_than_zero(self) -> None:
        assert metrics.max_drawdown([], []) == (None, None)

    def test_cagr_over_the_window(self) -> None:
        window = data.Window(
            start=date(2016, 1, 1), end=date(2026, 1, 1), fetch_start=date(2015, 1, 1)
        )
        # Ten years (3,653 days / 365.25) doubling: 2^(1/10) − 1 ≈ 7.18%.
        assert metrics.cagr(1.0, 2.0, window) == pytest.approx(0.0717, abs=1e-4)

    def test_a_wiped_out_curve_has_no_growth_rate_rather_than_minus_100_percent(self) -> None:
        window = data.Window(
            start=date(2016, 1, 1), end=date(2026, 1, 1), fetch_start=date(2015, 1, 1)
        )
        assert metrics.cagr(1.0, 0.0, window) is None
        assert metrics.cagr(0.0, 1.0, window) is None


# ---------------------------------------------------------------------------
# §6.3's benchmarks.
# ---------------------------------------------------------------------------

BENCH_START = date(2018, 1, 1)
BENCH_END = date(2022, 12, 30)
BENCH_WINDOW = data.Window(start=date(2021, 1, 4), end=date(2022, 12, 30), fetch_start=BENCH_START)


def bench_cache(root: Path, series: dict[str, pd.DataFrame]) -> data.PriceCache:
    cache = data.PriceCache(root / "cache")
    for symbol, frame in series.items():
        cache.store(symbol, frame)
    return cache


def flat_series(days: list[date], values: np.ndarray, *, adj: np.ndarray | None = None):
    return pd.DataFrame(
        {
            "Open": values,
            "High": values,
            "Low": values,
            "Close": values,
            "Adj Close": values if adj is None else adj,
            "Volume": np.full(len(days), 1_000_000.0),
        },
        index=pd.DatetimeIndex([pd.Timestamp(day) for day in days]),
    )


class TestSpyBenchmark:
    def test_it_uses_adj_close_because_6_3_1_says_total_return(self, tmp_path: Path) -> None:
        """The raw close would silently strip a decade of dividends."""
        days = business_days(BENCH_START, BENCH_END)
        close = np.full(len(days), 100.0)
        adj = np.linspace(50.0, 100.0, len(days))  # a total-return series that grew
        cache = bench_cache(tmp_path, {data.BENCHMARK_SYMBOL: flat_series(days, close, adj=adj)})

        result = metrics.spy_buy_and_hold(cache, BENCH_WINDOW)
        assert result is not None
        # Flat on Close, up on Adj Close: the benchmark must see the second.
        assert result.total_return > 0.1

    def test_it_carries_6_3_1s_exposure_caveat(self, tmp_path: Path) -> None:
        days = business_days(BENCH_START, BENCH_END)
        cache = bench_cache(
            tmp_path,
            {data.BENCHMARK_SYMBOL: flat_series(days, np.linspace(100.0, 200.0, len(days)))},
        )
        result = metrics.spy_buy_and_hold(cache, BENCH_WINDOW)
        assert result is not None
        assert result.caveat == metrics.EXPOSURE_CAVEAT
        assert "15%" in result.caveat and "100%" in result.caveat

    def test_a_missing_benchmark_is_none_rather_than_a_flat_line(self, tmp_path: Path) -> None:
        assert metrics.spy_buy_and_hold(bench_cache(tmp_path, {}), BENCH_WINDOW) is None


class TestEqualWeightBenchmark:
    """§6.3.2, including the amendment §0's Decision 7 records."""

    def universe(self, root: Path, *, delist_on: date | None) -> tuple[data.PriceCache, list[date]]:
        days = business_days(BENCH_START, BENCH_END)
        steady = price_frame(days, base=100.0, drift=0.0004)

        faller = days if delist_on is None else [day for day in days if day <= delist_on]
        stopped = price_frame(faller, base=100.0, drift=0.0004)

        cache = bench_cache(
            root,
            {
                "STEADY": steady,
                "STOPS": stopped,
                data.BENCHMARK_SYMBOL: price_frame(days, base=300.0, drift=0.0003),
            },
        )
        calendar = [day for day in days if BENCH_WINDOW.start <= day <= BENCH_WINDOW.end]
        return cache, calendar

    def test_a_delisted_name_holds_its_last_close_and_then_sits_in_cash(
        self, tmp_path: Path
    ) -> None:
        delist_on = date(2021, 6, 30)
        cache, calendar = self.universe(tmp_path, delist_on=delist_on)
        result = metrics.equal_weight_entered(["STEADY", "STOPS"], cache, BENCH_WINDOW, calendar)

        assert result is not None
        assert result.constituents == 2
        assert result.unpriced == 0

        # Reconstruct the delisted leg: after its last close it must not move.
        closes = data.ChainCloses(cache)
        last = closes.close_on("STOPS", BENCH_WINDOW.end)
        assert last == pytest.approx(closes.close_on("STOPS", delist_on))

        # And the portfolio still moves, because the surviving name still does.
        assert result.curve_values[0] == pytest.approx(1.0)
        assert result.curve_values[-1] != pytest.approx(1.0)

    def test_it_is_not_a_same_trade_window_hold(self, tmp_path: Path) -> None:
        """The whole point of the amendment: the window is the evaluation window.

        Every constituent is held from its first eligible session to the window
        end, so the curve runs the full calendar rather than only the days some
        trade happened to be open.
        """
        cache, calendar = self.universe(tmp_path, delist_on=None)
        result = metrics.equal_weight_entered(["STEADY", "STOPS"], cache, BENCH_WINDOW, calendar)
        assert result is not None
        assert len(result.curve_dates) == len(calendar)
        assert result.curve_dates[0] == calendar[0]
        assert result.curve_dates[-1] == calendar[-1]

    def test_equal_weight_is_the_mean_of_the_wealth_relatives(self, tmp_path: Path) -> None:
        days = business_days(BENCH_START, BENCH_END)
        # One name doubles, one halves; equal-weighted that is (2 + 0.5) / 2.
        cache = bench_cache(
            tmp_path,
            {
                "UP": flat_series(days, np.linspace(100.0, 200.0, len(days))),
                "DOWN": flat_series(days, np.linspace(100.0, 50.0, len(days))),
                data.BENCHMARK_SYMBOL: flat_series(days, np.full(len(days), 300.0)),
            },
        )
        calendar = [day for day in days if BENCH_WINDOW.start <= day <= BENCH_WINDOW.end]
        result = metrics.equal_weight_entered(["UP", "DOWN"], cache, BENCH_WINDOW, calendar)

        assert result is not None
        closes = data.ChainCloses(cache)
        expected = (
            closes.close_on("UP", BENCH_WINDOW.end) / closes.close_on("UP", calendar[0])
            + closes.close_on("DOWN", BENCH_WINDOW.end) / closes.close_on("DOWN", calendar[0])
        ) / 2
        assert result.curve_values[-1] == pytest.approx(expected, rel=1e-9)

    def test_a_name_with_too_little_history_is_counted_not_dropped(self, tmp_path: Path) -> None:
        """An unpriceable constituent shows as a count, never as a smaller mean."""
        days = business_days(BENCH_START, BENCH_END)
        short = business_days(date(2022, 10, 1), BENCH_END)
        cache = bench_cache(
            tmp_path,
            {
                "STEADY": price_frame(days, base=100.0),
                "TINY": price_frame(short, base=100.0),
                data.BENCHMARK_SYMBOL: price_frame(days, base=300.0),
            },
        )
        calendar = [day for day in days if BENCH_WINDOW.start <= day <= BENCH_WINDOW.end]
        result = metrics.equal_weight_entered(["STEADY", "TINY"], cache, BENCH_WINDOW, calendar)

        assert result is not None
        assert result.constituents == 1
        assert result.unpriced == 1

    def test_no_priceable_constituent_is_none_rather_than_a_flat_line(self, tmp_path: Path) -> None:
        cache, calendar = self.universe(tmp_path, delist_on=None)
        assert metrics.equal_weight_entered(["ABSENT"], cache, BENCH_WINDOW, calendar) is None

    def test_the_low_coverage_warning_rides_on_the_benchmark_table(self, tmp_path: Path) -> None:
        """Acceptance 2: *every* headline table, benchmarks included."""
        cache, calendar = self.universe(tmp_path, delist_on=None)
        thin = data.Coverage(
            window=BENCH_WINDOW,
            run_date=date(2022, 12, 31),
            snapshot_date=None,
            total_member_weeks=1000,
            covered_member_weeks=800,
            members=2,
            no_data_members=0,
        )
        result = metrics.equal_weight_entered(
            ["STEADY"], cache, BENCH_WINDOW, calendar, coverage=thin
        )
        assert result is not None
        assert result.low_coverage_warning is True
        assert result.coverage_ratio == pytest.approx(0.8)
