"""Universe tests (SPEC.md §3.1). No network: the cap fetcher is injected."""

import pytest

from leaps_scanner import universe
from leaps_scanner.prices import Throttle
from leaps_scanner.universe import SeedEntry

SEED = [
    SeedEntry("AAPL", "Apple Inc.", "Information Technology", "Hardware"),
    SeedEntry("SMALL", "Small Co", "Industrials", "Widgets"),
    SeedEntry("MSFT", "Microsoft", "Information Technology", "Software"),
    SeedEntry("NOQUOTE", "Delisted Co", "Utilities", "Electric"),
]

CAPS = {"AAPL": 4.4e12, "SMALL": 12e9, "MSFT": 3.1e12}


def no_throttle() -> Throttle:
    return Throttle(rate=1e9, sleeper=lambda _: None, clock=lambda: 0.0)


class TestSeedList:
    def test_bundled_seed_loads(self):
        seed = universe.load_seed()

        assert len(seed) > 400
        assert all(entry.symbol and entry.name for entry in seed)

    def test_symbols_use_yahoos_dash_convention(self):
        symbols = {entry.symbol for entry in universe.load_seed()}

        # Yahoo wants BRK-B; the source list writes BRK.B.
        assert "BRK-B" in symbols
        assert not any("." in symbol for symbol in symbols)

    def test_seed_has_no_duplicate_symbols(self):
        symbols = [entry.symbol for entry in universe.load_seed()]

        assert len(symbols) == len(set(symbols))


class TestSelectUniverse:
    def test_keeps_only_names_at_or_above_the_floor(self):
        selected = universe.select_universe(SEED, CAPS)

        assert [entry.symbol for entry in selected] == ["AAPL", "MSFT"]

    def test_the_floor_is_inclusive(self):
        selected = universe.select_universe(SEED, {"SMALL": universe.MIN_MARKET_CAP})

        assert [entry.symbol for entry in selected] == ["SMALL"]

    def test_symbol_without_a_cap_is_excluded(self):
        # An unpriceable name is not screened rather than assumed large.
        assert "NOQUOTE" not in {e.symbol for e in universe.select_universe(SEED, CAPS)}

    def test_sorted_largest_first(self):
        caps = [entry.market_cap for entry in universe.select_universe(SEED, CAPS)]

        assert caps == sorted(caps, reverse=True)

    def test_seed_metadata_is_carried_through(self):
        selected = universe.select_universe(SEED, CAPS)

        assert selected[0].name == "Apple Inc."
        assert selected[0].sector == "Information Technology"
        assert selected[0].market_cap == pytest.approx(4.4e12)

    def test_the_floor_is_fifty_billion(self):
        assert universe.MIN_MARKET_CAP == 50e9


class TestFetchMarketCaps:
    def test_collects_a_cap_per_symbol(self):
        caps = universe.fetch_market_caps(
            ["AAPL", "MSFT"], fetcher=CAPS.get, throttle=no_throttle()
        )

        assert caps == {"AAPL": 4.4e12, "MSFT": 3.1e12}

    def test_a_failing_symbol_is_skipped_not_fatal(self):
        def fetcher(symbol):
            if symbol == "BAD":
                raise ConnectionError("no quote")
            return CAPS[symbol]

        caps = universe.fetch_market_caps(
            ["AAPL", "BAD", "MSFT"], fetcher=fetcher, throttle=no_throttle()
        )

        assert sorted(caps) == ["AAPL", "MSFT"]

    def test_missing_and_zero_values_are_dropped(self):
        caps = universe.fetch_market_caps(
            ["A", "B"], fetcher={"A": None, "B": 0.0}.get, throttle=no_throttle()
        )

        assert caps == {}

    def test_requests_are_throttled(self):
        now = [0.0]
        slept = []

        def sleeper(seconds):
            slept.append(seconds)
            now[0] += seconds

        universe.fetch_market_caps(
            ["AAPL", "MSFT", "SMALL"],
            fetcher=CAPS.get,
            throttle=Throttle(rate=5.0, sleeper=sleeper, clock=lambda: now[0]),
        )

        assert slept == [pytest.approx(0.2), pytest.approx(0.2)]
