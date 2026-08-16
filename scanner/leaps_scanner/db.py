"""Supabase access for the scanner (service key — bypasses RLS)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

from leaps_scanner.config import Settings, load_settings

if TYPE_CHECKING:  # pragma: no cover - import cost only paid by type checkers
    from supabase import Client

# PostgREST payloads are sent in chunks so one scan's worth of rows does not
# arrive as a single oversized request.
WRITE_CHUNK_SIZE = 200

# PostgREST caps responses; reads page through explicit ranges of this size.
READ_PAGE_SIZE = 1000

STATUS_RUNNING = "running"
STATUS_OK = "ok"
STATUS_FAILED = "failed"


def service_client(settings: Settings | None = None) -> Client:
    """Build a Supabase client authenticated with the service key.

    Import of `supabase` is deferred so that modules which only need config (and
    tests that never touch the network) do not pay for it.
    """
    from supabase import create_client

    resolved = settings or load_settings()
    return create_client(resolved.supabase_url, resolved.supabase_service_key)


def now_iso() -> str:
    """Current UTC instant, ISO-8601 — every timestamp this app writes."""
    return datetime.now(UTC).isoformat()


def start_scan(client: Client, kind: str, as_of_date: date) -> int:
    """Open a `scans` row and return its id."""
    response = (
        client.table("scans")
        .insert(
            {
                "kind": kind,
                "as_of_date": as_of_date.isoformat(),
                "started_at": now_iso(),
                "status": STATUS_RUNNING,
            }
        )
        .execute()
    )
    return int(response.data[0]["id"])


def finish_scan(
    client: Client,
    scan_id: int,
    *,
    status: str,
    universe_count: int,
    matches_count: int,
    notes: str | None = None,
    as_of_date: date | None = None,
) -> None:
    """Close out a `scans` row.

    A scan is only ever `ok` when the caller says so; partial data is recorded
    as `failed` (SPEC.md §9) precisely so it cannot be mistaken for a full run.

    `as_of_date` corrects the provisional date the row was opened with, once the
    signals have revealed which week the scan actually speaks for.
    """
    payload = {
        "finished_at": now_iso(),
        "status": status,
        "universe_count": universe_count,
        "matches_count": matches_count,
        "notes": notes,
    }
    if as_of_date is not None:
        payload["as_of_date"] = as_of_date.isoformat()

    client.table("scans").update(payload).eq("id", scan_id).execute()


def upsert_tickers(client: Client, rows: Sequence[dict[str, Any]]) -> None:
    """Refresh the universe table, chunked."""
    for chunk in _chunks(rows):
        client.table("tickers").upsert(chunk, on_conflict="symbol").execute()


def write_scan_results(client: Client, rows: Sequence[dict[str, Any]]) -> None:
    """Write per-symbol results, chunked.

    Upsert rather than insert so a rerun of the same scan id is idempotent.
    """
    for chunk in _chunks(rows):
        client.table("scan_results").upsert(chunk, on_conflict="scan_id,symbol").execute()


def upsert_weekly_bars(client: Client, rows: Sequence[dict[str, Any]]) -> None:
    """Write the weekly bar history the charts read (SPEC.md §8.2), chunked.

    Idempotent per (symbol, week_ending): a rerun rewrites the same bars, and a
    revision to a past week (a late Yahoo correction) overwrites rather than
    duplicating.
    """
    for chunk in _chunks(rows):
        client.table("weekly_bars").upsert(chunk, on_conflict="symbol,week_ending").execute()


def upsert_iv_snapshots(client: Client, rows: Sequence[dict[str, Any]]) -> None:
    """Append daily ATM IV snapshots, chunked; idempotent per (symbol, date)."""
    for chunk in _chunks(rows):
        client.table("iv_snapshots").upsert(chunk, on_conflict="symbol,snap_date").execute()


def fetch_iv_history(
    client: Client, *, since: date, until: date
) -> dict[str, list[tuple[date, float]]]:
    """Read iv_snapshots between two dates, per symbol, oldest first.

    Paged through explicit ranges because PostgREST caps a single response;
    ordering by (symbol, snap_date) keeps the pages stable while reading.
    The loop only stops on an empty page and advances by the rows actually
    received: PostgREST's `max-rows` setting truncates pages *silently* (HTTP
    200), so "shorter than requested" is not proof the data is exhausted.
    """
    history: dict[str, list[tuple[date, float]]] = {}
    offset = 0
    while True:
        response = (
            client.table("iv_snapshots")
            .select("symbol,snap_date,iv30")
            .gte("snap_date", since.isoformat())
            .lte("snap_date", until.isoformat())
            .order("symbol")
            .order("snap_date")
            .range(offset, offset + READ_PAGE_SIZE - 1)
            .execute()
        )
        rows = response.data or []
        if not rows:
            return history
        for row in rows:
            if row.get("iv30") is None:
                continue
            snap_date = date.fromisoformat(row["snap_date"])
            history.setdefault(row["symbol"], []).append((snap_date, float(row["iv30"])))
        offset += len(rows)


def fetch_universe_symbols(client: Client) -> list[str]:
    """Every symbol in `tickers` — the universe the last full scan settled on.

    The daily refresh does not re-derive membership (§3 makes that the full
    scan's step 1); it refreshes what is already there.
    """
    rows = _read_all(lambda: client.table("tickers").select("symbol"), "symbol")
    return [row["symbol"] for row in rows]


def fetch_latest_earnings(client: Client) -> dict[str, date]:
    """Next-earnings dates from the most recent successful full scan.

    §7's earnings heads-up is a daily rule, but the date behind it is a weekly
    fact that the full scan already fetched and stored. Re-pulling
    `yf.Ticker.info` for the whole universe every evening to learn the same
    thing would double the refresh's cost for a field that moves quarterly.
    """
    scan = latest_full_scan(client)
    if scan is None:
        return {}

    rows = _read_all(
        lambda: (
            client.table("scan_results")
            .select("symbol,next_earnings")
            .eq("scan_id", scan["id"])
            .not_.is_("next_earnings", "null")
        ),
        "symbol",
    )
    return {row["symbol"]: date.fromisoformat(row["next_earnings"]) for row in rows}


def latest_full_scan(client: Client) -> dict[str, Any] | None:
    """The newest full scan that finished cleanly.

    `kind = 'full'` matters: refresh runs write `scans` rows too, and a refresh
    has no `scan_results` behind it. Without the filter the newest row would
    often be a refresh, and every consumer of "the latest scan" would find it
    empty.
    """
    response = (
        client.table("scans")
        .select("*")
        .eq("kind", "full")
        .eq("status", STATUS_OK)
        .order("as_of_date", desc=True)
        .order("id", desc=True)
        .limit(1)
        .execute()
    )
    rows = response.data or []
    return rows[0] if rows else None


def fetch_positions(client: Client, *, status: str | None = None) -> list[dict[str, Any]]:
    """Raw `positions` rows, optionally filtered by status."""

    def build():
        query = client.table("positions").select("*")
        return query if status is None else query.eq("status", status)

    return _read_all(build, "id")


def upsert_position_marks(client: Client, rows: Sequence[dict[str, Any]]) -> None:
    """Write today's marks for held contracts; idempotent per (position, date)."""
    for chunk in _chunks(rows):
        client.table("position_marks").upsert(chunk, on_conflict="position_id,mark_date").execute()


def fetch_recent_alerts(client: Client, *, since: date) -> list[dict[str, Any]]:
    """Alerts created on or after `since`, for suppression decisions.

    The window only has to cover the longest suppression rule (the breaker's
    four weeks) plus the earnings run-up; the caller sets it.
    """
    return _read_all(
        lambda: client.table("alerts").select("*").gte("created_at", since.isoformat()),
        "id",
    )


def insert_alerts(client: Client, rows: Sequence[dict[str, Any]]) -> None:
    """Append alerts, chunked. Plain insert: an alert is an event, not a state,
    and two genuine firings on different days are two rows."""
    for chunk in _chunks(rows):
        client.table("alerts").insert(chunk).execute()


def _read_all(build_query: Callable[[], Any], order_column: str) -> list[dict[str, Any]]:
    """Page a PostgREST query to exhaustion.

    Same discipline as `fetch_iv_history`: Supabase's `max-rows` truncates a
    page with a plain HTTP 200, so a short page proves nothing. Stop only on an
    empty one, and advance by the rows actually received.

    `build_query` is a factory, not a query: postgrest-py's builders mutate in
    place and return `self`, so reusing one across pages would stack a second
    `order` clause onto every subsequent request.
    """
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        response = (
            build_query().order(order_column).range(offset, offset + READ_PAGE_SIZE - 1).execute()
        )
        page = response.data or []
        if not page:
            return rows
        rows.extend(page)
        offset += len(page)


def _chunks(rows: Sequence[dict[str, Any]], size: int = WRITE_CHUNK_SIZE):
    for i in range(0, len(rows), size):
        yield list(rows[i : i + size])
