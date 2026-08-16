"""Point-in-time membership tests (SPEC-BACKTEST.md §2.1-§2.3, §9)."""

from datetime import date

import pytest

from leaps_scanner.backtest.membership import (
    Membership,
    MembershipSpan,
    load_spans,
    membership_text,
    normalize_symbol,
)

ALIAS_HEADER = "symbol,added,removed,price_symbol\n"


def write_csv(tmp_path, rows: str, header: str = "symbol,added,removed\n"):
    path = tmp_path / "membership.csv"
    path.write_text("# a comment line, as the real file carries\n" + header + rows)
    return path


class TestBoundarySemantics:
    """§2.2: inclusive of `added`, exclusive of `removed`."""

    span = MembershipSpan(symbol="ABC", added=date(2020, 1, 10), removed=date(2021, 6, 30))

    def test_the_addition_date_is_inside_the_span(self):
        assert self.span.covers(date(2020, 1, 10))

    def test_the_day_before_addition_is_outside(self):
        assert not self.span.covers(date(2020, 1, 9))

    def test_the_removal_date_is_outside_the_span(self):
        assert not self.span.covers(date(2021, 6, 30))

    def test_the_day_before_removal_is_inside(self):
        assert self.span.covers(date(2021, 6, 29))

    def test_an_open_span_covers_every_later_date(self):
        open_span = MembershipSpan(symbol="ABC", added=date(2020, 1, 10), removed=None)

        assert open_span.covers(date(2099, 1, 1))
        assert not open_span.covers(date(2019, 12, 31))

    def test_same_day_add_and_remove_covers_nothing(self):
        # A replacement announced and effective the same day leaves no day of
        # membership — the half-open reading, not an off-by-one.
        same_day = MembershipSpan(symbol="ABC", added=date(2020, 1, 10), removed=date(2020, 1, 10))

        assert not same_day.covers(date(2020, 1, 10))


class TestNormalization:
    def test_class_shares_take_the_yfinance_dash(self):
        assert normalize_symbol("BRK.B") == "BRK-B"
        assert normalize_symbol("BF.B") == "BF-B"

    def test_plain_symbols_are_untouched(self):
        assert normalize_symbol("AAPL") == "AAPL"

    def test_whitespace_and_case_are_normalized(self):
        assert normalize_symbol(" brk.b \n") == "BRK-B"

    def test_the_csv_is_normalized_on_load(self, tmp_path):
        spans = load_spans(write_csv(tmp_path, "brk.b,2020-01-02,\n"))

        assert spans[0].symbol == "BRK-B"


class TestRenames:
    """§2.3: a re-ticker is a removal of the old symbol and an addition of the new."""

    def test_the_old_symbol_stops_and_the_new_one_starts_on_the_effective_date(self, tmp_path):
        spans = load_spans(write_csv(tmp_path, "FB,2013-12-23,2022-06-09\nMETA,2022-06-09,\n"))
        membership = Membership(spans=tuple(spans))

        assert membership.members_on(date(2022, 6, 8)) == ("FB",)
        assert membership.members_on(date(2022, 6, 9)) == ("META",)

    def test_a_rename_never_double_counts_the_company(self, tmp_path):
        spans = load_spans(write_csv(tmp_path, "FB,2013-12-23,2022-06-09\nMETA,2022-06-09,\n"))
        membership = Membership(spans=tuple(spans))

        for day in (date(2022, 6, 8), date(2022, 6, 9), date(2023, 1, 3)):
            assert len(membership.members_on(day)) == 1

    def test_a_symbol_can_leave_and_come_back(self, tmp_path):
        spans = load_spans(write_csv(tmp_path, "AAP,2015-07-09,2023-08-25\nAAP,2024-01-02,\n"))
        membership = Membership(spans=tuple(spans))

        assert membership.members_on(date(2016, 1, 4)) == ("AAP",)
        assert membership.members_on(date(2023, 9, 1)) == ()
        assert membership.members_on(date(2024, 2, 1)) == ("AAP",)


class TestValidation:
    def test_a_span_ending_before_it_starts_is_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="removed"):
            load_spans(write_csv(tmp_path, "ABC,2020-01-10,2019-01-01\n"))

    def test_overlapping_spans_for_one_symbol_are_rejected(self, tmp_path):
        # Overlap would count the same member-week twice and inflate §2.4's
        # denominator, so a hand-compiled file is not trusted on this.
        with pytest.raises(ValueError, match="overlapping"):
            load_spans(write_csv(tmp_path, "ABC,2020-01-01,2021-01-01\nABC,2020-06-01,\n"))

    def test_two_open_spans_for_one_symbol_are_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="overlapping"):
            load_spans(write_csv(tmp_path, "ABC,2020-01-01,\nABC,2021-01-01,\n"))

    def test_a_bad_date_names_the_line_the_file_actually_has(self, tmp_path):
        # The fixture opens with one comment line, so the bad row is line 3 —
        # not row 2. The shipped file has 38 header lines, where counting rows
        # after stripping comments would point 38 lines away from the problem.
        with pytest.raises(ValueError, match="line 3"):
            load_spans(write_csv(tmp_path, "ABC,not-a-date,\n"))

    def test_the_reported_line_survives_a_long_comment_header(self, tmp_path):
        path = tmp_path / "membership.csv"
        path.write_text("#\n" * 20 + "symbol,added,removed\nAAA,2020-01-01,\nBBB,nope,\n")

        with pytest.raises(ValueError, match="line 23"):
            load_spans(path)

    def test_a_missing_added_date_is_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="no added date"):
            load_spans(write_csv(tmp_path, "ABC,,\n"))

    def test_touching_spans_are_allowed(self, tmp_path):
        # Removed on the 9th, re-added on the 9th: half-open, so no overlap.
        spans = load_spans(write_csv(tmp_path, "ABC,2020-01-01,2022-06-09\nABC,2022-06-09,\n"))

        assert len(spans) == 2


class TestQueries:
    def membership(self):
        return Membership(
            spans=(
                MembershipSpan("AAA", date(2015, 1, 2), None),
                MembershipSpan("BBB", date(2016, 3, 4), date(2018, 5, 6)),
                MembershipSpan("CCC", date(2019, 1, 1), None),
            )
        )

    def test_members_on_is_sorted(self):
        assert self.membership().members_on(date(2020, 1, 1)) == ("AAA", "CCC")

    def test_members_between_includes_anyone_who_overlapped(self):
        found = self.membership().members_between(date(2017, 1, 1), date(2017, 12, 31))

        assert found == ("AAA", "BBB")

    def test_members_between_excludes_a_span_that_ended_before_the_range(self):
        found = self.membership().members_between(date(2018, 5, 6), date(2018, 12, 31))

        assert found == ("AAA",)

    def test_member_weeks_are_keyed_by_the_fridays_the_symbol_was_in(self):
        fridays = [date(2018, 4, 27), date(2018, 5, 4), date(2018, 5, 11)]

        weeks = self.membership().member_weeks(fridays)

        # BBB leaves on 2018-05-06, so the 11th is not one of its member-weeks.
        assert weeks["BBB"] == (date(2018, 4, 27), date(2018, 5, 4))
        assert weeks["AAA"] == tuple(fridays)


class TestPriceAlias:
    """§2.3a: which cached series prices a span, when the company re-tickered."""

    def load(self, tmp_path, rows):
        return Membership(spans=tuple(load_spans(write_csv(tmp_path, rows, ALIAS_HEADER))))

    def source(self, membership, symbol, day):
        return membership.price_source(membership.span_on(symbol, day))

    def test_a_renamed_span_prices_from_its_successor(self, tmp_path):
        members = self.load(tmp_path, "FB,2013-12-23,2022-06-09,META\nMETA,2022-06-09,,\n")

        assert self.source(members, "FB", date(2020, 1, 3)).symbol == "META"

    def test_a_span_without_an_alias_prices_itself(self, tmp_path):
        members = self.load(tmp_path, "AAPL,1982-11-30,,\n")

        source = self.source(members, "AAPL", date(2020, 1, 3))
        assert source.symbol == "AAPL"
        assert source.until is None

    def test_the_successor_series_is_cut_at_the_re_ticker_date(self, tmp_path):
        # Without the cutoff, FB would also claim META's own member-weeks, and a
        # week no single membership row owns would be counted twice.
        members = self.load(tmp_path, "FB,2013-12-23,2022-06-09,META\nMETA,2022-06-09,,\n")

        assert self.source(members, "FB", date(2020, 1, 3)).until == date(2022, 6, 9)

    def test_a_chain_resolves_to_the_ticker_that_still_trades(self, tmp_path):
        # WLP -> ANTM -> ELV. The middle ticker has no Yahoo data at all, which
        # is exactly why resolution cannot stop at the immediate successor.
        members = self.load(
            tmp_path,
            "WLP,1999-06-09,2014-12-03,ANTM\nANTM,2014-12-03,2022-06-28,ELV\nELV,2022-06-28,,\n",
        )

        assert self.source(members, "WLP", date(2010, 1, 8)).symbol == "ELV"

    def test_a_chain_keeps_the_first_spans_cutoff_not_the_last(self, tmp_path):
        members = self.load(
            tmp_path,
            "WLP,1999-06-09,2014-12-03,ANTM\nANTM,2014-12-03,2022-06-28,ELV\nELV,2022-06-28,,\n",
        )

        assert self.source(members, "WLP", date(2010, 1, 8)).until == date(2014, 12, 3)

    def test_an_alias_is_scoped_to_the_span_not_the_symbol(self, tmp_path):
        # The regression that matters most. IR is Ingersoll-Rand plc until
        # 2020-03-02 and a different Ingersoll Rand Inc after it. A symbol-wide
        # alias would price the second company off the first's successor.
        members = self.load(
            tmp_path,
            "IR,2010-11-17,2020-03-02,TT\nIR,2020-03-02,,\nTT,2020-03-02,,\n",
        )

        assert self.source(members, "IR", date(2015, 1, 2)).symbol == "TT"
        assert self.source(members, "IR", date(2022, 1, 7)).symbol == "IR"

    def test_the_fetch_list_carries_rename_successors(self, tmp_path):
        members = self.load(tmp_path, "FB,2013-12-23,2022-06-09,META\nMETA,2022-06-09,,\n")

        assert members.price_symbols_between(date(2016, 1, 1), date(2020, 1, 1)) == ("META",)

    def test_an_alias_naming_its_own_symbol_is_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="aliases itself"):
            self.load(tmp_path, "FB,2013-12-23,2022-06-09,FB\n")

    def test_an_alias_on_a_still_open_span_is_rejected(self, tmp_path):
        # A company cannot both have re-tickered and never left.
        with pytest.raises(ValueError, match="aliases an open span"):
            self.load(tmp_path, "FB,2013-12-23,,META\nMETA,2022-06-09,,\n")

    def test_an_alias_whose_successor_does_not_start_that_day_is_rejected(self, tmp_path):
        # §2.3 writes a rename as remove+add on one date; anything else means
        # the alias points at a company that was never this one.
        with pytest.raises(ValueError, match="no span beginning"):
            self.load(tmp_path, "FB,2013-12-23,2022-06-09,META\nMETA,2023-01-03,,\n")

    def test_a_cycle_in_the_chain_is_rejected_not_hung(self, tmp_path):
        # Every alias here pairs correctly, so only the walk itself can catch
        # it: AAA hands off to BBB, which hands back to AAA.
        with pytest.raises(ValueError, match="cycles"):
            self.load(
                tmp_path,
                "AAA,2010-01-01,2015-01-01,BBB\nBBB,2015-01-01,2020-01-01,AAA\nAAA,2020-01-01,,\n",
            )

    def test_a_dead_chain_resolves_rather_than_raising(self, tmp_path):
        # COG -> CTRA, where Coterra was itself acquired and purged. Resolution
        # must succeed; §2.4 then reports the span as uncovered, honestly.
        members = self.load(
            tmp_path, "COG,2008-06-23,2021-10-04,CTRA\nCTRA,2021-10-04,2026-05-07,\n"
        )

        assert self.source(members, "COG", date(2018, 1, 5)).symbol == "CTRA"


class TestShippedFile:
    """The committed CSV has to satisfy everything the loader checks."""

    def test_every_row_carries_the_price_symbol_field(self):
        rows = [
            line for line in membership_text().splitlines() if line and not line.startswith("#")
        ]
        assert rows[0] == "symbol,added,removed,price_symbol"
        assert all(line.count(",") == 3 for line in rows[1:])

    def test_the_header_documents_the_price_symbol_column(self):
        comments = [line for line in membership_text().splitlines() if line.startswith("#")]
        assert any("price_symbol" in line for line in comments)

    def test_every_alias_pairs_with_an_addition_on_the_effective_date(self):
        # The §2.3 encoding, checked against the shipped file rather than
        # trusted: an alias that does not pair points at a different company.
        members = Membership.load()
        starts = {(span.symbol, span.added) for span in members.spans}
        for span in members.spans:
            if span.price_symbol is not None:
                assert (span.price_symbol, span.removed) in starts, span

    def test_a_reassigned_symbol_keeps_its_later_span_unaliased(self):
        # IR and Q were both handed to different companies. If a later span ever
        # acquired an alias, hundreds of member-weeks would silently be priced
        # off the wrong security.
        members = Membership.load()
        for symbol in ("IR", "Q"):
            spans = members.spans_for(symbol)
            assert spans, symbol
            assert spans[-1].price_symbol is None, symbol

    def test_the_shipped_aliases_all_resolve(self):
        members = Membership.load()
        aliased = [span for span in members.spans if span.price_symbol is not None]

        assert len(aliased) == 19
        for span in aliased:
            source = members.price_source(span)
            assert source.symbol != span.symbol
            assert source.until == span.removed

    def test_it_loads_and_validates(self):
        spans = load_spans()

        assert len(spans) > 500

    def test_it_records_its_source_and_retrieval_date(self):
        comments = [line for line in membership_text().splitlines() if line.startswith("#")]

        assert any("wikipedia.org" in line for line in comments)
        assert any("Retrieved:" in line for line in comments)

    def test_symbols_are_already_in_yfinance_form(self):
        assert all("." not in span.symbol for span in load_spans())

    def test_the_index_is_roughly_the_right_size_across_the_window(self):
        # Not an exact 500: the index holds ~503 lines for ~500 companies with
        # multiple share classes, and the source table is community-maintained.
        membership = Membership(spans=tuple(load_spans()))

        for day in (date(2016, 8, 12), date(2021, 6, 4), date(2026, 8, 14)):
            assert 470 <= len(membership.members_on(day)) <= 520
