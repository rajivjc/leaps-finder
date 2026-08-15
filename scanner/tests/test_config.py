import pytest

from leaps_scanner.config import ConfigError, Settings, load_env_file, load_settings

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


def test_env_file_populates_the_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("SUPABASE_URL=https://from-file.supabase.co\nSUPABASE_SERVICE_KEY=k\n")

    assert load_env_file(env_file) is True
    assert load_settings().supabase_url == "https://from-file.supabase.co"


def test_env_file_does_not_override_the_real_environment(tmp_path, monkeypatch):
    # Actions secrets must win over a stale local file.
    monkeypatch.setenv("SUPABASE_URL", "https://from-actions.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "actions-key")
    env_file = tmp_path / ".env"
    env_file.write_text("SUPABASE_URL=https://from-file.supabase.co\nSUPABASE_SERVICE_KEY=k\n")

    load_env_file(env_file)

    assert load_settings().supabase_url == "https://from-actions.supabase.co"


def test_missing_env_file_is_not_an_error(tmp_path):
    # The file never exists in CI; that path has to stay quiet.
    assert load_env_file(tmp_path / "absent.env") is False
