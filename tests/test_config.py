"""Konfigurationsauflösung: CLI-Flag > Env > Config-Datei > Default."""

from __future__ import annotations

from pathlib import Path

import pytest

from dbrain.config import ConfigError, resolve


def test_url_und_token_aus_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DBRAIN_URL", "https://env.example")
    monkeypatch.setenv("DBRAIN_TOKEN", "env-token")

    konfiguration = resolve(config_path=tmp_path / "fehlt.toml")

    assert konfiguration.base_url == "https://env.example"
    assert konfiguration.token == "env-token"


def test_url_flag_gewinnt_gegen_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DBRAIN_URL", "https://env.example")
    monkeypatch.setenv("DBRAIN_TOKEN", "env-token")

    konfiguration = resolve(url="https://flag.example", config_path=tmp_path / "x.toml")

    assert konfiguration.base_url == "https://flag.example"


def test_config_datei_als_unterste_schicht(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("DBRAIN_URL", raising=False)
    monkeypatch.delenv("DBRAIN_TOKEN", raising=False)
    datei = tmp_path / "config.toml"
    datei.write_text('base_url = "https://datei.example"\ntoken = "datei-token"\n')

    konfiguration = resolve(config_path=datei)

    assert konfiguration.base_url == "https://datei.example"
    assert konfiguration.token == "datei-token"


def test_env_gewinnt_gegen_config_datei(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DBRAIN_URL", "https://env.example")
    monkeypatch.delenv("DBRAIN_TOKEN", raising=False)
    datei = tmp_path / "config.toml"
    datei.write_text('base_url = "https://datei.example"\ntoken = "datei-token"\n')

    konfiguration = resolve(config_path=datei)

    assert konfiguration.base_url == "https://env.example"
    assert konfiguration.token == "datei-token"


def test_ohne_url_wirft_config_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("DBRAIN_URL", raising=False)

    with pytest.raises(ConfigError):
        resolve(config_path=tmp_path / "fehlt.toml")


def test_ohne_token_wirft_config_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DBRAIN_URL", "https://env.example")
    monkeypatch.delenv("DBRAIN_TOKEN", raising=False)

    with pytest.raises(ConfigError):
        resolve(config_path=tmp_path / "fehlt.toml")


def test_profil_ueberschreibt_top_level(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("DBRAIN_URL", raising=False)
    monkeypatch.delenv("DBRAIN_TOKEN", raising=False)
    datei = tmp_path / "config.toml"
    datei.write_text(
        'base_url = "https://prod.example"\n'
        'token = "prod-token"\n'
        "\n"
        "[profiles.staging]\n"
        'base_url = "https://staging.example"\n'
        'token = "staging-token"\n'
    )

    konfiguration = resolve(config_path=datei, profile="staging")

    assert konfiguration.base_url == "https://staging.example"
    assert konfiguration.token == "staging-token"


def test_unbekanntes_profil_wirft_config_error(tmp_path: Path) -> None:
    datei = tmp_path / "config.toml"
    datei.write_text('base_url = "https://prod.example"\ntoken = "x"\n')

    with pytest.raises(ConfigError):
        resolve(config_path=datei, profile="unbekannt")
