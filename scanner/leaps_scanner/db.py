"""Supabase access for the scanner (service key — bypasses RLS)."""

from __future__ import annotations

from collections.abc import Sequence
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


def _now() -> str:
    return datetime.now(UTC).isoformat()


def start_scan(client: Client, kind: str, as_of_date: date) -> int:
    """Open a `scans` row and return its id."""
    response = (
        client.table("scans")
        .insert(
            {
                "kind": kind,
                "as_of_date": as_of_date.isoformat(),
                "started_at": _now(),
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
        "finished_at": _now(),
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


def _chunks(rows: Sequence[dict[str, Any]], size: int = WRITE_CHUNK_SIZE):
    for i in range(0, len(rows), size):
        yield list(rows[i : i + size])
