import pytest

from leaps_scanner.config import ConfigError, Settings, load_settings

VALID_ENV = {
    "SUPABASE_URL": "https://example.supabase.co",
    "SUPABASE_SERVICE_KEY": "service-key-value",
}


def test_load_settings_reads_both_variables():
    settings = load_settings(VALID_ENV)

    assert settings == Settings(
        supabase_url="https://example.supabase.co",
        supabase_service_key="service-key-value",
    )


def test_load_settings_names_every_missing_variable():
    with pytest.raises(ConfigError) as excinfo:
        load_settings({})

    message = str(excinfo.value)
    assert "SUPABASE_URL" in message
    assert "SUPABASE_SERVICE_KEY" in message


def test_blank_values_count_as_missing():
    with pytest.raises(ConfigError) as excinfo:
        load_settings({**VALID_ENV, "SUPABASE_SERVICE_KEY": "   "})

    assert "SUPABASE_SERVICE_KEY" in str(excinfo.value)
    assert "SUPABASE_URL" not in str(excinfo.value)


def test_repr_does_not_leak_the_service_key():
    # This repo is public and tracebacks end up in Actions logs.
    assert "service-key-value" not in repr(load_settings(VALID_ENV))
