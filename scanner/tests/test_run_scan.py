import pytest

from leaps_scanner import run_scan

VALID_ENV = {
    "SUPABASE_URL": "https://example.supabase.co",
    "SUPABASE_SERVICE_KEY": "service-key-value",
}


@pytest.mark.parametrize("command", ["full", "refresh"])
def test_parser_accepts_both_commands(command):
    assert run_scan._build_parser().parse_args([command]).command == command


def test_parser_rejects_unknown_command():
    with pytest.raises(SystemExit) as excinfo:
        run_scan._build_parser().parse_args(["backtest"])

    assert excinfo.value.code == 2


def test_main_exits_nonzero_when_config_is_missing(monkeypatch, capsys):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)

    assert run_scan.main(["full"]) == 2
    assert "SUPABASE_URL" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["full", "refresh"])
def test_main_reaches_the_pipeline_once_configured(monkeypatch, command):
    for key, value in VALID_ENV.items():
        monkeypatch.setenv(key, value)

    # The pipeline itself is M2/M3/M5 work; what M1 guarantees is that the CLI
    # gets there rather than dying on config.
    with pytest.raises(NotImplementedError):
        run_scan.main([command])
