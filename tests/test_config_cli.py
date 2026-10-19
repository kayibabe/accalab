import sys
from pathlib import Path

from accalab import cli
from accalab.config import Settings, load_settings, validate_probe_settings


def test_load_settings_defaults_to_all_leagues_and_reads_local_plan_defaults(
    tmp_path: Path, monkeypatch
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    source = Path(__file__).resolve().parents[1] / "config" / "settings.toml"
    (config_dir / "settings.toml").write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.delenv("API_FOOTBALL_KEY", raising=False)
    monkeypatch.delenv("API_FOOTBALL_LEAGUE_IDS", raising=False)

    settings = load_settings(tmp_path)

    assert settings.league_ids == ()
    assert settings.daily_quota_limit == 7500
    assert settings.requests_per_minute == 300


def test_league_ids_can_restrict_scope_without_a_fixed_count(tmp_path: Path, monkeypatch) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    source = Path(__file__).resolve().parents[1] / "config" / "settings.toml"
    (config_dir / "settings.toml").write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setenv("API_FOOTBALL_KEY", "placeholder")
    monkeypatch.setenv("API_FOOTBALL_LEAGUE_IDS", "39,140,39")

    settings = load_settings(tmp_path)
    validate_probe_settings(settings)

    assert settings.league_ids == (39, 140)


def test_cli_writes_report_when_api_key_is_missing(tmp_path: Path, monkeypatch, capsys) -> None:
    settings = Settings(
        api_key=None,
        league_ids=(),
        base_url="https://example.test",
        timeout_seconds=1,
        retries=0,
        backoff_seconds=0,
        requests_per_minute=10,
        daily_quota_limit=100,
        cache_path=tmp_path / "cache.sqlite3",
        cache_ttl_seconds=60,
        timezone="UTC",
        future_days=5,
        historical_days=(1, 7, 30, 90),
        report_path=tmp_path / "PHASE0_REPORT.md",
    )
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(sys, "argv", ["accalab", "probe-odds"])

    assert cli.main() == 2
    assert "API_FOOTBALL_KEY is missing" in settings.report_path.read_text(encoding="utf-8")
    assert "Report written" in capsys.readouterr().err


def test_daily_ingest_requires_api_key_before_opening_database(monkeypatch, capsys) -> None:
    settings = load_settings()
    settings = Settings(**{**settings.__dict__, "api_key": None})
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(sys, "argv", ["accalab", "ingest-daily"])

    assert cli.main() == 2
    assert "API_FOOTBALL_KEY is missing" in capsys.readouterr().err
