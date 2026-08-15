"""Scoring engine tests (SPEC.md §6, §10). Pure — no network, no database."""

from datetime import date

import pytest

from leaps_scanner import scoring
from leaps_scanner.fundamentals import Fundamentals
from leaps_scanner.indicators import Signals
from leaps_scanner.options import ContractEconomics


def signals(**overrides) -> Signals:
    base = dict(
        as_of_date=date(2026, 8, 14),
        spot=150.0,
        sma50=140.0,
        sma200=130.0,
        trend_pass=True,
        stoch_k=35.0,
        stoch_d=30.0,
        stoch_k_prev=32.0,
        in_zone=True,
        turning_up=True,
        pct_off_52w_high=-0.05,
        avg_volume_30d=1_000_000.0,
        share_above_sma50_60d=0.9,
        weeks_since_cross_up=1,
    )
    return Signals(**{**base, **overrides})


def fundamentals(**overrides) -> Fundamentals:
    base = dict(
        op_margin=0.175,  # clip_map(., 0.05, 0.30) = 50
        roe=0.19,  # clip_map(., 0.08, 0.30) = 50
        net_debt_ebitda=1.5,  # clip_map(3 − 1.5, 0, 3) = 50
        rev_growth=0.10,  # clip_map(., 0, 0.20) = 50
        fcf_margin=0.10,  # clip_map(., 0, 0.20) = 50
        fwd_pe=20.0,
        analyst_target=180.0,
        dividend_yield=0.01,
        next_earnings=date(2026, 9, 25),
    )
    return Fundamentals(**{**base, **overrides})


def contract(**overrides) -> ContractEconomics:
    base = dict(
        expiry=date(2027, 8, 20),
        dte=371,
        strike=130.0,
        delta=0.71,
        bid=33.0,
        ask=35.0,
        mid=34.0,
        spread_pct=2.0 / 34.0,
        oi=1200,
        iv=0.28,
        breakeven=164.0,
        breakeven_pct=164.0 / 150.0 - 1.0,
        cost_pct_spot=34.0 / 150.0,
    )
    return ContractEconomics(**{**base, **overrides})


class TestClipMap:
    """§10: clip_map edge cases."""

    def test_below_the_floor_is_zero(self):
        assert scoring.clip_map(-1.0, 0.0, 1.0) == 0.0

    def test_at_the_floor_is_zero(self):
        assert scoring.clip_map(0.0, 0.0, 1.0) == 0.0

    def test_at_the_ceiling_is_one_hundred(self):
        assert scoring.clip_map(1.0, 0.0, 1.0) == 100.0

    def test_above_the_ceiling_stays_one_hundred(self):
        assert scoring.clip_map(2.0, 0.0, 1.0) == 100.0

    def test_midpoint_is_fifty(self):
        assert scoring.clip_map(0.5, 0.0, 1.0) == pytest.approx(50.0)

    def test_interpolation_is_linear(self):
        assert scoring.clip_map(0.25, 0.0, 1.0) == pytest.approx(25.0)
        assert scoring.clip_map(0.15, 0.1, 0.3) == pytest.approx(25.0)

    def test_negative_bounds_work(self):
        assert scoring.clip_map(-0.5, -1.0, 0.0) == pytest.approx(50.0)

    def test_degenerate_bounds_are_rejected(self):
        with pytest.raises(ValueError):
            scoring.clip_map(0.5, 1.0, 1.0)
        with pytest.raises(ValueError):
            scoring.clip_map(0.5, 1.0, 0.0)


class TestPercentileOf:
    def test_lowest_and_highest_of_distinct_values(self):
        population = [10.0, 20.0, 30.0, 40.0]

        assert scoring.percentile_of(10.0, population) == pytest.approx(12.5)
        assert scoring.percentile_of(40.0, population) == pytest.approx(87.5)

    def test_ties_count_half(self):
        assert scoring.percentile_of(5.0, [5.0, 5.0]) == pytest.approx(50.0)

    def test_single_member_population_sits_at_fifty(self):
        assert scoring.percentile_of(7.0, [7.0]) == pytest.approx(50.0)

    def test_empty_population_is_an_error(self):
        with pytest.raises(ValueError):
            scoring.percentile_of(1.0, [])


class TestTrendScore:
    def test_over_twenty_percent_extension_zeroes_the_subscore(self):
        # extension = 250/200 − 1 = 0.25 > 0.20 → gate 0, whatever the base.
        s = signals(spot=250.0, sma50=200.0, sma200=200.0, share_above_sma50_60d=0.75)
        gated = scoring.trend_score(s)

        assert gated == pytest.approx(0.0)

    def test_unextended_uptrend_keeps_its_base_score(self):
        # extension = 210/210 − 1 = 0 → clip_map(0.20, 0, 0.10) = 100 → gate 1.
        s = signals(spot=210.0, sma50=210.0, sma200=200.0, share_above_sma50_60d=0.75)

        # base = mean(clip(0.05, 0, 0.25)=20, clip(0.05, 0, 0.10)=50, 50) = 40
        assert scoring.trend_score(s) == pytest.approx(40.0)

    def test_partial_extension_scales_linearly(self):
        # extension = 0.15 → clip_map(0.05, 0, 0.10) = 50 → gate 0.5.
        s = signals(spot=241.5, sma50=210.0, sma200=200.0, share_above_sma50_60d=0.75)
        full = signals(spot=210.0, sma50=210.0, sma200=200.0, share_above_sma50_60d=0.75)

        gated = scoring.trend_score(s)
        base = scoring.trend_score(full)

        assert 0.0 < gated < base


class TestQualityScore:
    def test_all_midpoint_metrics_score_fifty(self):
        score, all_present = scoring.quality_score(fundamentals())

        assert score == pytest.approx(50.0)
        assert all_present is True

    def test_net_cash_scores_one_hundred_on_that_metric(self):
        score, _ = scoring.quality_score(
            Fundamentals(net_debt_ebitda=-0.5)  # net cash
        )

        assert score == pytest.approx(100.0)

    def test_missing_metric_is_excluded_from_the_mean_not_zeroed(self):
        # Remaining four metrics all sit at 50; a missing fifth must not drag.
        score, all_present = scoring.quality_score(fundamentals(fcf_margin=None))

        assert score == pytest.approx(50.0)
        assert all_present is False

    def test_no_metrics_at_all_yields_none(self):
        score, all_present = scoring.quality_score(Fundamentals())

        assert score is None
        assert all_present is False


class TestOptionScore:
    def test_no_contract_means_no_score(self):
        assert scoring.option_score(None, 0.30, 25.0) is None

    def test_components_match_hand_computation(self):
        # iv_rank 25 → clip(25, 0, 50) = 50; iv30 0.275 → clip(0.125, 0, 0.25) = 50;
        # cost 0.225 → clip(0.075, 0, 0.15) = 50; spread 0.06 → clip(0.04, 0, 0.08) = 50;
        # oi 1050 → clip(1050, 100, 2000) = 50.
        econ = contract(cost_pct_spot=0.225, spread_pct=0.06, oi=1050)

        assert scoring.option_score(econ, 0.275, 25.0) == pytest.approx(50.0)

    def test_missing_iv_inputs_are_excluded_from_the_mean(self):
        econ = contract(cost_pct_spot=0.225, spread_pct=0.06, oi=1050)

        assert scoring.option_score(econ, None, None) == pytest.approx(50.0)


class TestValuationScore:
    def test_upside_and_inverted_percentile_average(self):
        # upside 0.125 → 50; percentile 30 → inverted 70.
        assert scoring.valuation_score(0.125, 30.0) == pytest.approx(60.0)

    def test_cheaper_forward_pe_scores_higher(self):
        cheap = scoring.valuation_score(0.125, 10.0)
        rich = scoring.valuation_score(0.125, 90.0)

        assert cheap > rich

    def test_upside_haircut_is_forty_percent(self):
        assert scoring.upside_adjusted(180.0, 150.0) == pytest.approx(0.6 * 0.2)
        assert scoring.upside_adjusted(None, 150.0) is None

    def test_missing_inputs_are_excluded(self):
        assert scoring.valuation_score(0.125, None) == pytest.approx(50.0)
        assert scoring.valuation_score(None, 30.0) == pytest.approx(70.0)
        assert scoring.valuation_score(None, None) is None


class TestEntryScore:
    @pytest.mark.parametrize(
        ("stoch_k", "expected_zone"),
        [
            (20.0, 0.0),
            (22.5, 50.0),
            (25.0, 100.0),
            (35.0, 100.0),
            (45.0, 100.0),
            (57.5, 50.0),
            (70.0, 0.0),
            (15.0, 0.0),
            (75.0, 0.0),
        ],
    )
    def test_zone_peaks_at_25_to_45_and_falls_to_zero_at_20_and_70(self, stoch_k, expected_zone):
        # Freshness pinned at 100 so the zone term is readable in isolation.
        assert scoring.entry_score(stoch_k, 1) == pytest.approx((expected_zone + 100.0) / 2.0)

    @pytest.mark.parametrize(
        ("weeks", "expected_freshness"),
        [(1, 100.0), (2, 100.0), (3, 60.0), (4, 60.0), (5, 30.0), (None, 30.0)],
    )
    def test_freshness_tiers(self, weeks, expected_freshness):
        assert scoring.entry_score(35.0, weeks) == pytest.approx((100.0 + expected_freshness) / 2.0)


class TestCompositeScore:
    def test_uses_the_pinned_weights(self):
        score = scoring.composite_score(100.0, 80.0, 60.0, 40.0, 20.0)

        assert score == pytest.approx(0.25 * 100 + 0.25 * 80 + 0.20 * 60 + 0.15 * 40 + 0.15 * 20)

    def test_any_missing_subscore_nulls_the_composite(self):
        assert scoring.composite_score(100.0, None, 60.0, 40.0, 20.0) is None


class TestIvRank:
    def test_matches_the_pinned_formula_times_one_hundred(self):
        history = [0.20, 0.60, 0.40]  # current 0.40 in [0.20, 0.60] → 50

        assert scoring.iv_rank(history) == pytest.approx(50.0)

    def test_at_the_window_minimum_is_zero_and_maximum_one_hundred(self):
        assert scoring.iv_rank([0.60, 0.20]) == pytest.approx(0.0)
        assert scoring.iv_rank([0.20, 0.60]) == pytest.approx(100.0)

    def test_only_the_trailing_window_counts(self):
        # The ancient spike outside the 252-snapshot window must not compress
        # today's rank.
        history = [5.0] + [0.20] * 251 + [0.40]

        assert scoring.iv_rank(history) == pytest.approx(100.0)

    def test_flat_history_has_no_defined_rank(self):
        assert scoring.iv_rank([0.30, 0.30, 0.30]) is None
        assert scoring.iv_rank([]) is None


def flags(**overrides) -> scoring.PresetFlags:
    base = dict(
        trend_pass=True,
        stoch_k=35.0,
        turning_up=True,
        iv_rank_value=25.0,
        earnings_dte=45,
        spread_pct=0.04,
        oi=800,
        s_quality=65.0,
        quality_all_present=True,
    )
    return scoring.preset_flags(**{**base, **overrides})


class TestPresetFlags:
    """§10: preset filter logic against the §6 table."""

    def test_the_reference_candidate_clears_all_three_tiers(self):
        result = flags()

        assert result == scoring.PresetFlags(strict=True, balanced=True, wide=True)

    def test_trend_failure_disqualifies_every_tier(self):
        result = flags(trend_pass=False)

        assert result == scoring.PresetFlags(strict=False, balanced=False, wide=False)

    @pytest.mark.parametrize(
        ("stoch_k", "strict", "balanced", "wide"),
        [
            (15.0, False, False, True),  # below 20: only Wide's 10-80 band
            (60.0, False, True, True),  # above 55: out of Strict's band
            (75.0, False, False, True),  # above 70: out of Balanced's band
            (85.0, False, False, False),  # above 80: out of every band
        ],
    )
    def test_stochastic_bands_per_tier(self, stoch_k, strict, balanced, wide):
        result = flags(stoch_k=stoch_k)

        assert (result.strict, result.balanced, result.wide) == (strict, balanced, wide)

    def test_wide_does_not_require_turning_up(self):
        result = flags(turning_up=False)

        assert result == scoring.PresetFlags(strict=False, balanced=False, wide=True)

    @pytest.mark.parametrize(
        ("iv_rank_value", "strict", "balanced"),
        [(30.0, True, True), (40.0, False, True), (50.0, False, True), (60.0, False, False)],
    )
    def test_iv_rank_thresholds(self, iv_rank_value, strict, balanced):
        result = flags(iv_rank_value=iv_rank_value)

        assert (result.strict, result.balanced) == (strict, balanced)
        assert result.wide is True  # no IV gate on Wide

    def test_unknown_iv_rank_fails_strict_and_balanced_not_wide(self):
        result = flags(iv_rank_value=None)

        assert result == scoring.PresetFlags(strict=False, balanced=False, wide=True)

    @pytest.mark.parametrize(
        ("earnings_dte", "strict", "balanced"),
        [(30, True, True), (20, False, True), (14, False, True), (10, False, False)],
    )
    def test_earnings_distance_thresholds(self, earnings_dte, strict, balanced):
        result = flags(earnings_dte=earnings_dte)

        assert (result.strict, result.balanced) == (strict, balanced)

    def test_unknown_earnings_date_fails_strict_and_balanced_not_wide(self):
        result = flags(earnings_dte=None)

        assert result == scoring.PresetFlags(strict=False, balanced=False, wide=True)

    @pytest.mark.parametrize(
        ("spread_pct", "strict", "balanced", "wide"),
        [
            (0.05, True, True, True),
            (0.07, False, True, True),
            (0.10, False, False, True),
            (0.13, False, False, False),
        ],
    )
    def test_spread_thresholds(self, spread_pct, strict, balanced, wide):
        result = flags(spread_pct=spread_pct)

        assert (result.strict, result.balanced, result.wide) == (strict, balanced, wide)

    @pytest.mark.parametrize(
        ("oi", "strict", "balanced", "wide"),
        [
            (500, True, True, True),
            (300, False, True, True),
            (150, False, False, True),
            (50, False, False, False),
        ],
    )
    def test_open_interest_thresholds(self, oi, strict, balanced, wide):
        result = flags(oi=oi)

        assert (result.strict, result.balanced, result.wide) == (strict, balanced, wide)

    def test_no_contract_fails_every_tier(self):
        result = flags(spread_pct=None, oi=None)

        assert result == scoring.PresetFlags(strict=False, balanced=False, wide=False)

    def test_strict_needs_all_quality_metrics_present(self):
        result = flags(quality_all_present=False)

        assert result.strict is False
        assert result.balanced is True

    @pytest.mark.parametrize(
        ("s_quality", "strict", "balanced"),
        [(60.0, True, True), (50.0, False, True), (40.0, False, False)],
    )
    def test_quality_thresholds(self, s_quality, strict, balanced):
        result = flags(s_quality=s_quality)

        assert (result.strict, result.balanced) == (strict, balanced)

    def test_missing_quality_score_fails_strict_and_balanced_not_wide(self):
        result = flags(s_quality=None, quality_all_present=False)

        assert result == scoring.PresetFlags(strict=False, balanced=False, wide=True)
