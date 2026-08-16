"""Exit-rule tests (SPEC.md §7, §10).

§10 names "exit-rule evaluation incl. crossing semantics" as a required unit
test, so the crossing cases are first-class here rather than folded into the
pipeline suite. Every threshold is tested *on* its boundary, because every one
of them is written with an explicit inequality in the spec and getting the edge
wrong is the whole failure mode.
"""

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from leaps_scanner import indicators, risk

AS_OF = date(2026, 8, 14)
USER = "11111111-1111-1111-1111-111111111111"


def position(**overrides) -> risk.Position:
    base = dict(
        id=1,
        user_id=USER,
        symbol="AAPL",
        opened_on=date(2026, 2, 2),
        expiry=date(2027, 6, 18),
        strike=180.0,
        contracts=2,
        entry_premium=10.0,
        account_equity_at_entry=100_000.0,
    )
    base.update(overrides)
    return risk.Position(**base)


def series(values) -> pd.Series:
    return pd.Series(values, dtype="float64")


def trend(close=200.0, sma50=190.0, sma200=180.0) -> indicators.DailyTrend:
    return indicators.DailyTrend(
        as_of_date=AS_OF,
        close=close,
        sma50=sma50,
        sma200=sma200,
        trend_pass=bool(close > sma50 and close > sma200 and sma50 > sma200),
        trend_broken=bool(close < sma200 or sma50 < sma200),
    )


# --------------------------------------------------------------------------
# §4/§7 crossing semantics — the case §10 calls out by name
# --------------------------------------------------------------------------


class TestStochasticExit:
    def test_a_fresh_cross_below_20_fires(self):
        assert risk.is_stochastic_exit(series([35.0, 28.0, 22.5, 18.4]))

    def test_sitting_below_20_does_not_fire(self):
        # The distinction §10 pins: this %K has been under 20 for weeks. It is
        # not crossing anything, and an exit that re-fired here every week would
        # be indistinguishable from noise.
        assert not risk.is_stochastic_exit(series([18.0, 16.0, 14.0, 12.0]))

    def test_touching_20_from_above_without_breaking_it_does_not_fire(self):
        # Exactly 20 is still in the zone (§4: 20 ≤ slowK), so landing on it is
        # not a crossing.
        assert not risk.is_stochastic_exit(series([30.0, 25.0, 20.0]))

    def test_crossing_from_exactly_20_fires(self):
        assert risk.is_stochastic_exit(series([30.0, 20.0, 19.9]))

    def test_a_rise_back_above_20_does_not_fire(self):
        assert not risk.is_stochastic_exit(series([12.0, 15.0, 24.0]))

    def test_a_single_bar_cannot_cross(self):
        assert not risk.is_stochastic_exit(series([5.0]))

    def test_nan_padding_is_ignored(self):
        assert risk.is_stochastic_exit(series([np.nan, np.nan, 24.0, 19.0]))


# --------------------------------------------------------------------------
# The remaining rules in §7's table
# --------------------------------------------------------------------------


class TestTrendBreak:
    def test_close_below_the_200_day_breaks(self):
        assert risk.is_trend_broken(trend(close=175.0, sma50=190.0, sma200=180.0))

    def test_the_50_crossing_under_the_200_breaks(self):
        assert risk.is_trend_broken(trend(close=200.0, sma50=178.0, sma200=180.0))

    def test_a_healthy_trend_does_not_break(self):
        assert not risk.is_trend_broken(trend())

    def test_the_hold_band_is_neither_an_entry_nor_an_exit(self):
        # Below the 50-day, well above the 200, with the averages still stacked:
        # §4's entry filter fails and §7's exit does not trigger. Deriving one
        # from the other would collapse this band and force a sale.
        reading = trend(close=185.0, sma50=190.0, sma200=180.0)
        assert not reading.trend_pass
        assert not reading.trend_broken


class TestPremiumStop:
    def test_a_mid_below_half_the_entry_stops(self):
        assert risk.is_premium_stopped(10.0, 4.90)

    def test_exactly_half_stops(self):
        # §7 writes `≤`. A mark landing on the stop is a stop.
        assert risk.is_premium_stopped(10.0, 5.0)

    def test_a_cent_above_half_does_not(self):
        assert not risk.is_premium_stopped(10.0, 5.01)


class TestTimeExit:
    def test_179_days_exits(self):
        assert risk.is_time_exit(AS_OF + timedelta(days=179), AS_OF)

    def test_exactly_180_days_does_not(self):
        # §7 writes `DTE < 180`, so 180 is still inside the window.
        assert not risk.is_time_exit(AS_OF + timedelta(days=180), AS_OF)

    def test_an_expired_contract_exits(self):
        assert risk.is_time_exit(AS_OF - timedelta(days=1), AS_OF)


class TestEarningsSoon:
    def test_exactly_21_days_out_flags(self):
        assert risk.is_earnings_soon(AS_OF + timedelta(days=21), AS_OF)

    def test_22_days_out_does_not(self):
        assert not risk.is_earnings_soon(AS_OF + timedelta(days=22), AS_OF)

    def test_a_stale_past_date_is_not_imminent(self):
        assert not risk.is_earnings_soon(AS_OF - timedelta(days=3), AS_OF)

    def test_an_unknown_date_is_not_imminent(self):
        assert not risk.is_earnings_soon(None, AS_OF)


# --------------------------------------------------------------------------
# §7's circuit breaker
# --------------------------------------------------------------------------


class TestBreakerEquity:
    def test_the_newest_open_position_supplies_the_denominator(self):
        older = position(id=1, opened_on=date(2026, 1, 5), account_equity_at_entry=80_000.0)
        newer = position(id=2, opened_on=date(2026, 6, 1), account_equity_at_entry=120_000.0)

        assert risk.breaker_equity([older, newer]) == 120_000.0

    def test_ties_on_the_open_date_break_on_id(self):
        first = position(id=1, opened_on=date(2026, 6, 1), account_equity_at_entry=80_000.0)
        second = position(id=2, opened_on=date(2026, 6, 1), account_equity_at_entry=120_000.0)

        assert risk.breaker_equity([first, second]) == 120_000.0

    def test_positions_without_a_snapshot_are_skipped_not_zeroed(self):
        with_equity = position(id=1, opened_on=date(2026, 1, 5), account_equity_at_entry=80_000.0)
        without = position(id=2, opened_on=date(2026, 6, 1), account_equity_at_entry=None)

        assert risk.breaker_equity([with_equity, without]) == 80_000.0

    def test_no_snapshot_anywhere_yields_none(self):
        assert risk.breaker_equity([position(account_equity_at_entry=None)]) is None

    def test_no_positions_yields_none(self):
        assert risk.breaker_equity([]) is None

    def test_a_closed_position_can_supply_the_base(self):
        # A sleeve that was stopped out and fully closed has no open position to
        # read equity from — and that is exactly when §7's entry ban matters.
        closed = position(
            id=2,
            status=risk.STATUS_CLOSED,
            closed_on=AS_OF,
            exit_premium=1.0,
            account_equity_at_entry=150_000.0,
        )

        assert risk.breaker_equity([closed]) == 150_000.0


class TestSleevePnl:
    def test_unrealized_uses_the_mark_against_cost_basis(self):
        held = position(contracts=2, entry_premium=10.0)
        marks = {1: risk.Mark(position_id=1, mark_date=AS_OF, bid=5.9, ask=6.1, mid=6.0)}

        pnl = risk.sleeve_pnl([held], [], marks, AS_OF)

        # (6.00 − 10.00) × 2 contracts × 100 shares
        assert pnl.unrealized == pytest.approx(-800.0)
        assert pnl.realized == 0.0
        assert pnl.complete

    def test_realized_counts_only_the_trailing_four_weeks(self):
        recent = position(
            id=2,
            status=risk.STATUS_CLOSED,
            closed_on=AS_OF - timedelta(days=10),
            entry_premium=10.0,
            exit_premium=6.0,
            contracts=1,
        )
        ancient = position(
            id=3,
            status=risk.STATUS_CLOSED,
            closed_on=AS_OF - timedelta(days=200),
            entry_premium=10.0,
            exit_premium=1.0,
            contracts=1,
        )

        pnl = risk.sleeve_pnl([], [recent, ancient], {}, AS_OF)

        # Only the recent close counts: (6 − 10) × 1 × 100. A bad quarter two
        # years ago must not hold the sleeve shut forever.
        assert pnl.realized == pytest.approx(-400.0)

    def test_a_close_exactly_on_the_window_edge_counts(self):
        edge = position(
            id=2,
            status=risk.STATUS_CLOSED,
            closed_on=AS_OF - timedelta(days=risk.CIRCUIT_BREAKER_DAYS),
            entry_premium=10.0,
            exit_premium=8.0,
            contracts=1,
        )

        assert risk.sleeve_pnl([], [edge], {}, AS_OF).realized == pytest.approx(-200.0)

    def test_an_unmarked_open_position_makes_the_reading_incomplete(self):
        pnl = risk.sleeve_pnl([position(id=1), position(id=2)], [], {}, AS_OF)

        assert pnl.open_count == 2
        assert pnl.marked_count == 0
        assert not pnl.complete

    def test_a_fully_closed_sleeve_still_has_an_equity_base(self):
        wipeout = position(
            id=2,
            status=risk.STATUS_CLOSED,
            closed_on=AS_OF - timedelta(days=3),
            entry_premium=50.0,
            exit_premium=5.0,
            contracts=3,
            account_equity_at_entry=200_000.0,
        )

        pnl = risk.sleeve_pnl([], [wipeout], {}, AS_OF)

        assert pnl.equity == 200_000.0
        # (5 − 50) × 3 × 100
        assert pnl.realized == pytest.approx(-13_500.0)
        # 13,500 / 200,000 = 6.75% — under the breaker, but measurable, which is
        # the point: before this it was not measurable at all.
        assert pnl.loss_fraction == pytest.approx(0.0675)

    def test_a_wiped_out_sleeve_trips_the_breaker_after_everything_is_closed(self):
        wipeout = position(
            id=2,
            status=risk.STATUS_CLOSED,
            closed_on=AS_OF - timedelta(days=1),
            entry_premium=50.0,
            exit_premium=5.0,
            contracts=5,
            account_equity_at_entry=200_000.0,
        )

        pnl = risk.sleeve_pnl([], [wipeout], {}, AS_OF)
        alert = risk.circuit_breaker_alert(pnl, USER, AS_OF)

        # (5 − 50) × 5 × 100 = −22,500, or 11.25% of 200,000 — past the 8%
        # breaker. Before the sleeve included closed positions there was no
        # denominator here at all, and the breaker stayed silent.
        assert pnl.loss_fraction == pytest.approx(0.1125)
        assert alert is not None
        assert "$22,500" in alert.message
        assert "$200,000" in alert.message

    def test_an_equity_snapshot_outside_the_window_is_not_used(self):
        # A position closed six months ago is not part of this sleeve; its
        # realised loss is excluded, so its equity must be too.
        ancient = position(
            id=2,
            status=risk.STATUS_CLOSED,
            closed_on=AS_OF - timedelta(days=200),
            exit_premium=1.0,
            account_equity_at_entry=999_000.0,
        )

        assert risk.sleeve_pnl([], [ancient], {}, AS_OF).equity is None

    def test_a_close_without_an_exit_premium_is_skipped(self):
        unpriced = position(id=2, status=risk.STATUS_CLOSED, closed_on=AS_OF, exit_premium=None)

        assert risk.sleeve_pnl([], [unpriced], {}, AS_OF).realized == 0.0


class TestCircuitBreakerAlert:
    def _pnl(self, total: float, equity: float | None = 100_000.0) -> risk.SleevePnl:
        return risk.SleevePnl(
            realized=0.0,
            unrealized=total,
            equity=equity,
            open_count=1,
            marked_count=1,
            realized_since=AS_OF - timedelta(days=risk.CIRCUIT_BREAKER_DAYS),
        )

    def test_exactly_eight_percent_trips(self):
        assert risk.circuit_breaker_alert(self._pnl(-8_000.0), USER, AS_OF) is not None

    def test_just_under_eight_percent_does_not(self):
        assert risk.circuit_breaker_alert(self._pnl(-7_999.0), USER, AS_OF) is None

    def test_a_profitable_sleeve_does_not(self):
        assert risk.circuit_breaker_alert(self._pnl(12_000.0), USER, AS_OF) is None

    def test_no_equity_base_means_no_verdict(self):
        assert risk.circuit_breaker_alert(self._pnl(-50_000.0, equity=None), USER, AS_OF) is None

    def test_the_message_states_the_date_the_ban_lifts(self):
        alert = risk.circuit_breaker_alert(self._pnl(-9_000.0), USER, AS_OF)

        assert alert is not None
        assert alert.position_id is None  # sleeve-level, not about one position
        assert alert.as_of_date == AS_OF
        assert (AS_OF + timedelta(days=risk.CIRCUIT_BREAKER_DAYS)).isoformat() in alert.message
        assert "9.0%" in alert.message

    def test_an_incomplete_reading_says_so_in_the_message(self):
        pnl = risk.SleevePnl(
            realized=0.0,
            unrealized=-9_000.0,
            equity=100_000.0,
            open_count=3,
            marked_count=1,
            realized_since=AS_OF - timedelta(days=risk.CIRCUIT_BREAKER_DAYS),
        )

        alert = risk.circuit_breaker_alert(pnl, USER, AS_OF)

        assert alert is not None
        assert "1 of 3" in alert.message


# --------------------------------------------------------------------------
# Suppression
# --------------------------------------------------------------------------


def alert(kind: str, position_id: int | None = 1) -> risk.Alert:
    return risk.Alert(
        user_id=USER, kind=kind, message="…", as_of_date=AS_OF, position_id=position_id
    )


class TestAlertLedger:
    def test_an_empty_ledger_suppresses_nothing(self):
        ledger = risk.AlertLedger([])

        assert not ledger.suppresses(alert(risk.TREND_BREAK))

    def test_an_unacknowledged_state_alert_suppresses_its_twin(self):
        ledger = risk.AlertLedger(
            [
                risk.ExistingAlert(
                    kind=risk.TREND_BREAK,
                    created_on=AS_OF - timedelta(days=3),
                    position_id=1,
                    as_of_date=AS_OF - timedelta(days=3),
                    acknowledged=False,
                )
            ]
        )

        assert ledger.suppresses(alert(risk.TREND_BREAK))

    def test_acknowledging_re_arms_a_still_true_state_rule(self):
        # A monitor that goes quiet because it was dismissed once is not doing
        # its job — the trend is still broken today.
        ledger = risk.AlertLedger(
            [
                risk.ExistingAlert(
                    kind=risk.TREND_BREAK,
                    created_on=AS_OF - timedelta(days=3),
                    position_id=1,
                    as_of_date=AS_OF - timedelta(days=3),
                    acknowledged=True,
                )
            ]
        )

        assert not ledger.suppresses(alert(risk.TREND_BREAK))

    def test_a_rerun_of_the_same_day_never_double_writes(self):
        ledger = risk.AlertLedger(
            [
                risk.ExistingAlert(
                    kind=risk.PREMIUM_STOP,
                    created_on=AS_OF,
                    position_id=1,
                    as_of_date=AS_OF,
                    acknowledged=True,
                )
            ]
        )

        assert ledger.suppresses(alert(risk.PREMIUM_STOP))

    def test_a_different_position_is_not_suppressed(self):
        ledger = risk.AlertLedger(
            [
                risk.ExistingAlert(
                    kind=risk.TREND_BREAK,
                    created_on=AS_OF,
                    position_id=2,
                    as_of_date=AS_OF,
                    acknowledged=False,
                )
            ]
        )

        assert not ledger.suppresses(alert(risk.TREND_BREAK, position_id=1))

    def test_the_breaker_latches_for_four_weeks(self):
        ledger = risk.AlertLedger(
            [
                risk.ExistingAlert(
                    kind=risk.CIRCUIT_BREAKER,
                    created_on=AS_OF - timedelta(days=20),
                    position_id=None,
                    as_of_date=AS_OF - timedelta(days=20),
                    acknowledged=True,
                )
            ]
        )

        assert ledger.suppresses(alert(risk.CIRCUIT_BREAKER, position_id=None))

    def test_the_breaker_re_arms_once_the_ban_has_run(self):
        ledger = risk.AlertLedger(
            [
                risk.ExistingAlert(
                    kind=risk.CIRCUIT_BREAKER,
                    created_on=AS_OF - timedelta(days=risk.CIRCUIT_BREAKER_DAYS + 1),
                    position_id=None,
                    as_of_date=AS_OF - timedelta(days=risk.CIRCUIT_BREAKER_DAYS + 1),
                    acknowledged=True,
                )
            ]
        )

        assert not ledger.suppresses(alert(risk.CIRCUIT_BREAKER, position_id=None))

    def test_one_earnings_heads_up_per_report(self):
        earnings = AS_OF + timedelta(days=10)
        ledger = risk.AlertLedger(
            [
                risk.ExistingAlert(
                    kind=risk.EARNINGS_SOON,
                    created_on=AS_OF - timedelta(days=5),
                    position_id=1,
                    as_of_date=AS_OF - timedelta(days=5),
                    acknowledged=True,
                )
            ]
        )

        assert ledger.suppresses(alert(risk.EARNINGS_SOON), next_earnings=earnings)

    def test_the_next_quarter_gets_its_own_heads_up(self):
        earnings = AS_OF + timedelta(days=10)
        ledger = risk.AlertLedger(
            [
                risk.ExistingAlert(
                    kind=risk.EARNINGS_SOON,
                    created_on=AS_OF - timedelta(days=80),
                    position_id=1,
                    as_of_date=AS_OF - timedelta(days=80),
                    acknowledged=True,
                )
            ]
        )

        assert not ledger.suppresses(alert(risk.EARNINGS_SOON), next_earnings=earnings)

    def test_a_second_crossing_is_a_second_alert(self):
        # `crosses_below` already makes this rare; when it does happen it is a
        # genuinely new event, acknowledged or not.
        ledger = risk.AlertLedger(
            [
                risk.ExistingAlert(
                    kind=risk.STOCH_BELOW_20,
                    created_on=AS_OF - timedelta(days=21),
                    position_id=1,
                    as_of_date=AS_OF - timedelta(days=21),
                    acknowledged=False,
                )
            ]
        )

        assert not ledger.suppresses(alert(risk.STOCH_BELOW_20))


# --------------------------------------------------------------------------
# Per-position evaluation
# --------------------------------------------------------------------------


class TestEvaluatePosition:
    def test_daily_rules_fire_and_name_their_evidence(self):
        held = position(expiry=AS_OF + timedelta(days=100), entry_premium=10.0)
        mark = risk.Mark(position_id=1, mark_date=AS_OF, bid=3.9, ask=4.1, mid=4.0)

        outcome = risk.evaluate_position(
            risk.PositionInputs(
                position=held,
                trend=trend(close=170.0, sma50=190.0, sma200=180.0),
                mark=mark,
                next_earnings=AS_OF + timedelta(days=7),
            ),
            AS_OF,
        )

        kinds = {alert.kind for alert in outcome.alerts}
        assert kinds == {
            risk.TREND_BREAK,
            risk.PREMIUM_STOP,
            risk.TIME_EXIT,
            risk.EARNINGS_SOON,
        }
        assert outcome.unevaluated == ()

        by_kind = {alert.kind: alert.message for alert in outcome.alerts}
        # Each message has to be checkable against a specific day's data.
        assert AS_OF.isoformat() in by_kind[risk.PREMIUM_STOP]
        assert "4.00" in by_kind[risk.PREMIUM_STOP]
        assert "40%" in by_kind[risk.PREMIUM_STOP]
        assert "170.00" in by_kind[risk.TREND_BREAK]

    def test_the_weekly_rule_is_not_run_on_the_daily_cadence(self):
        outcome = risk.evaluate_position(
            risk.PositionInputs(position=position(), slow_k=series([30.0, 15.0])),
            AS_OF,
            rules=risk.DAILY_RULES,
        )

        assert all(alert.kind != risk.STOCH_BELOW_20 for alert in outcome.alerts)
        # Not scheduled is not the same as not evaluated.
        assert risk.STOCH_BELOW_20 not in outcome.unevaluated

    def test_the_weekly_cadence_runs_only_the_stochastic_rule(self):
        outcome = risk.evaluate_position(
            risk.PositionInputs(
                position=position(expiry=AS_OF + timedelta(days=10)),
                slow_k=series([30.0, 22.0, 15.0]),
            ),
            AS_OF,
            rules=risk.WEEKLY_RULES,
        )

        assert [alert.kind for alert in outcome.alerts] == [risk.STOCH_BELOW_20]
        # The contract is 10 days from expiry, but time exit is a daily rule and
        # was not asked for — so it is neither fired nor reported as a gap.
        assert outcome.unevaluated == ()

    def test_missing_data_is_reported_not_swallowed(self):
        outcome = risk.evaluate_position(
            risk.PositionInputs(position=position(expiry=AS_OF + timedelta(days=400))),
            AS_OF,
        )

        assert outcome.alerts == ()
        assert set(outcome.unevaluated) == {
            risk.TREND_BREAK,
            risk.PREMIUM_STOP,
            risk.EARNINGS_SOON,
        }

    def test_the_time_exit_needs_no_market_data(self):
        outcome = risk.evaluate_position(
            risk.PositionInputs(position=position(expiry=AS_OF + timedelta(days=30))),
            AS_OF,
        )

        assert any(alert.kind == risk.TIME_EXIT for alert in outcome.alerts)
        assert risk.TIME_EXIT not in outcome.unevaluated

    def test_the_ledger_filters_the_result(self):
        ledger = risk.AlertLedger(
            [
                risk.ExistingAlert(
                    kind=risk.TIME_EXIT,
                    created_on=AS_OF - timedelta(days=1),
                    position_id=1,
                    as_of_date=AS_OF - timedelta(days=1),
                    acknowledged=False,
                )
            ]
        )

        outcome = risk.evaluate_position(
            risk.PositionInputs(position=position(expiry=AS_OF + timedelta(days=30))),
            AS_OF,
            ledger=ledger,
        )

        assert outcome.alerts == ()


# --------------------------------------------------------------------------
# Row parsing
# --------------------------------------------------------------------------


class TestRowParsing:
    def test_a_position_row_round_trips(self):
        parsed = risk.Position.from_row(
            {
                "id": 4,
                "user_id": USER,
                "symbol": "MSFT",
                "opened_on": "2026-03-02",
                "expiry": "2027-06-18",
                "strike": "480.0",
                "contracts": 3,
                "entry_premium": "62.5",
                "account_equity_at_entry": "250000",
                "status": "open",
                "closed_on": None,
                "exit_premium": None,
            }
        )

        assert parsed.expiry == date(2027, 6, 18)
        assert parsed.strike == 480.0
        assert parsed.cost_basis == pytest.approx(18_750.0)
        assert parsed.closed_on is None
        assert parsed.describe() == "MSFT 2027-06-18 480C"

    def test_an_alert_row_takes_the_utc_day_from_its_timestamp(self):
        parsed = risk.ExistingAlert.from_row(
            {
                "kind": risk.TREND_BREAK,
                "created_at": "2026-08-14T22:31:05.123456+00:00",
                "position_id": 9,
                "as_of_date": "2026-08-14",
                "acknowledged": False,
            }
        )

        assert parsed.created_on == date(2026, 8, 14)
        assert parsed.as_of_date == date(2026, 8, 14)

    def test_a_sleeve_alert_has_no_position(self):
        parsed = risk.ExistingAlert.from_row(
            {
                "kind": risk.CIRCUIT_BREAKER,
                "created_at": "2026-08-14T22:31:05+00:00",
                "position_id": None,
                "as_of_date": "2026-08-14",
                "acknowledged": False,
            }
        )

        assert parsed.position_id is None

    def test_an_alert_row_carries_its_provenance(self):
        row = alert(risk.PREMIUM_STOP).to_row(scan_id=12)

        assert row["scan_id"] == 12
        assert row["as_of_date"] == AS_OF.isoformat()
        assert row["user_id"] == USER
