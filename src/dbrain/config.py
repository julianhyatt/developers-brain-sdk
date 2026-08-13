"""Konfigurationsauflösung: CLI-Flag > Env > Config-Datei > Default.

**Kein `--token`-Flag.** Ein Geheimnis als Kommandozeilenargument landet
unweigerlich in der Shell-History und ist über `ps`/`/proc/<pid>/cmdline`
für jeden anderen Prozess auf derselben Maschine sichtbar — auf einem
geteilten CI-Runner oder Mehrbenutzer-Host ein Leak an alle, die den
Prozess sehen können, bevor überhaupt geloggt wird. Das Token kommt
deshalb ausschließlich aus `DBRAIN_TOKEN`, `--token-stdin` oder der
Config-Datei — nie aus einem Argument.
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "dbrain" / "config.toml"


class ConfigError(Exception):
    """Fehlende oder widersprüchliche Konfiguration."""


@dataclass(frozen=True, slots=True)
class Config:
    base_url: str
    token: str


def _read_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open("rb") as datei:
        return tomllib.load(datei)


def resolve(
    *,
    url: str | None = None,
    token_stdin: bool = False,
    config_path: Path | None = None,
    profile: str | None = None,
) -> Config:
    """Löst Server-URL und Token aus allen Quellen auf.

    `profile` wählt einen `[profiles.<name>]`-Abschnitt der Config-Datei,
    der `base_url`/`token` des Top-Levels überschreibt — für mehrere
    Umgebungen (z. B. `staging`, `prod`) in derselben Datei, ohne
    `DBRAIN_URL`/`DBRAIN_TOKEN` bei jedem Aufruf neu zu setzen.
    """
    pfad = config_path or DEFAULT_CONFIG_PATH
    datei = _read_file(pfad)

    if profile is not None:
        profile_daten = datei.get("profiles", {}).get(profile)
        if profile_daten is None:
            raise ConfigError(f"Profil {profile!r} nicht in {pfad} gefunden")
        datei = {**datei, **profile_daten}

    resolved_url = url or os.environ.get("DBRAIN_URL") or datei.get("base_url")
    if not resolved_url:
        raise ConfigError(
            "Keine Server-URL — setze DBRAIN_URL, --url oder base_url in "
            f"{pfad}"
        )

    if token_stdin:
        resolved_token = sys.stdin.readline().strip()
        if not resolved_token:
            raise ConfigError("--token-stdin gesetzt, aber stdin war leer")
    else:
        resolved_token = os.environ.get("DBRAIN_TOKEN") or datei.get("token")

    if not resolved_token:
        raise ConfigError(
            "Kein Token — setze DBRAIN_TOKEN, --token-stdin oder token in "
            f"{pfad}"
        )

    return Config(base_url=str(resolved_url).rstrip("/"), token=str(resolved_token))
