"""Supabase access for the scanner (service key — bypasses RLS)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from leaps_scanner.config import Settings, load_settings

if TYPE_CHECKING:  # pragma: no cover - import cost only paid by type checkers
    from supabase import Client


def service_client(settings: Settings | None = None) -> Client:
    """Build a Supabase client authenticated with the service key.

    Import of `supabase` is deferred so that modules which only need config (and
    tests that never touch the network) do not pay for it.
    """
    from supabase import create_client

    resolved = settings or load_settings()
    return create_client(resolved.supabase_url, resolved.supabase_service_key)
