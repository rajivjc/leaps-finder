"""Environment configuration for the scanner.

The scanner writes with the Supabase *service* key, which bypasses RLS. That key
lives only in GitHub Actions secrets (or a local, git-ignored .env) — never in
code. This repo is public.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

SUPABASE_URL_VAR = "SUPABASE_URL"
SUPABASE_SERVICE_KEY_VAR = "SUPABASE_SERVICE_KEY"


class ConfigError(RuntimeError):
    """Raised when required environment configuration is missing."""


@dataclass(frozen=True)
class Settings:
    """Everything the scanner needs from the environment."""

    supabase_url: str
    supabase_service_key: str

    def __repr__(self) -> str:  # pragma: no cover - trivial
        # Never let the service key reach a log line or a traceback.
        return f"Settings(supabase_url={self.supabase_url!r}, supabase_service_key='***')"


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Read settings from `env` (defaults to the process environment).

    Raises ConfigError naming every missing variable at once, so a misconfigured
    Actions run fails with one useful message instead of three round trips.
    """
    source = os.environ if env is None else env

    values = {}
    missing = []
    for var in (SUPABASE_URL_VAR, SUPABASE_SERVICE_KEY_VAR):
        value = (source.get(var) or "").strip()
        if not value:
            missing.append(var)
        values[var] = value

    if missing:
        raise ConfigError(
            "Missing required environment variable(s): "
            + ", ".join(missing)
            + ". Set them in GitHub Actions secrets, or in scanner/.env for local runs."
        )

    return Settings(
        supabase_url=values[SUPABASE_URL_VAR],
        supabase_service_key=values[SUPABASE_SERVICE_KEY_VAR],
    )
