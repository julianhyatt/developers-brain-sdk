"""CLI-Verhalten: menschenlesbare Ausgabe vs. `--json`, Exit-Codes.

`dbrain.cli.BrainClient` wird durch eine Fabrik ersetzt, die einen Client
mit `httpx.MockTransport` statt echtem HTTP liefert — dieselbe Technik wie
in `tests/conftest.py`, nur von der CLI-Seite aus angestoßen.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from dbrain import cli
from dbrain.client import BrainClient
from tests.conftest import (
    json_response,
    make_hit,
    make_project_payload,
    make_search_payload,
    make_submission_payload,
)


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DBRAIN_URL", "http://test")
    monkeypatch.setenv("DBRAIN_TOKEN", "test-token")


def _patch_transport(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> None:
    echter_init = BrainClient.__init__

    def gepatchter_init(
        self: BrainClient, base_url: str, token: str, **kwargs: object
    ) -> None:
        kwargs["transport"] = httpx.MockTransport(handler)
        echter_init(self, base_url, token, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(BrainClient, "__init__", gepatchter_init)


def test_search_menschenlesbar(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(200, make_search_payload(hits=[make_hit(title="Fund")]))

    _patch_transport(monkeypatch, handler)

    code = cli.main(["search", "migration"])

    assert code == 0
    ausgabe = capsys.readouterr().out
    assert "Fund" in ausgabe
    assert "{" not in ausgabe


def test_search_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(200, make_search_payload(hits=[make_hit(title="Fund")]))

    _patch_transport(monkeypatch, handler)

    code = cli.main(["--json", "search", "migration"])

    assert code == 0
    ausgabe = capsys.readouterr().out
    assert '"title": "Fund"' in ausgabe


def test_store_rejected_gibt_exit_code_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import uuid

    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(
            422, {"detail": make_submission_payload(verdict="rejected", entry_id=None)}
        )

    _patch_transport(monkeypatch, handler)

    # UUID statt Slug: der Slug-Auflösungsschritt (`GET /v1/projects`) ist
    # hier nicht Gegenstand des Tests — derselbe Handler beantwortet jeden
    # Pfad mit der Ablehnung, ein Slug bräuchte einen zweiten Zweig.
    code = cli.main(
        [
            "store",
            "--project",
            str(uuid.uuid4()),
            "--title",
            "t",
            "--content",
            "c",
            "--source",
            "s",
        ]
    )

    assert code == 1
    assert "verdict=rejected" in capsys.readouterr().out


def test_store_tag_kommagetrennt_wird_gesplittet(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Regression für Issue #1: `--tag "a,b"` muss wie `--tag a --tag b`
    wirken, sonst findet die serverseitige `tags @> [...]`-Filterung
    (exakte Array-Elemente) den Eintrag nie wieder."""
    import uuid

    gesehene_tags: list[object] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        gesehene_tags.append(body.get("tags"))
        return json_response(200, make_submission_payload())

    _patch_transport(monkeypatch, handler)

    code = cli.main(
        [
            "store",
            "--project",
            str(uuid.uuid4()),
            "--title",
            "t",
            "--content",
            "c",
            "--source",
            "s",
            "--tag",
            "react,html, formulare ,ticket-17",
            "--tag",
            "react",
        ]
    )

    assert code == 0
    assert gesehene_tags[-1] == ["react", "html", "formulare", "ticket-17"]


def test_search_tag_kommagetrennt_wird_gesplittet(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gesehene_tags: list[object] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        gesehene_tags.append(body.get("tags"))
        return json_response(200, make_search_payload())

    _patch_transport(monkeypatch, handler)

    code = cli.main(["search", "migration", "--tag", "a,b"])

    assert code == 0
    assert gesehene_tags[-1] == ["a", "b"]


def test_fehlende_konfiguration_gibt_exit_code_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("DBRAIN_URL", raising=False)
    monkeypatch.delenv("DBRAIN_TOKEN", raising=False)

    code = cli.main(["--config", "/nicht/vorhanden.toml", "projects"])

    assert code == 1
    assert "Fehler" in capsys.readouterr().err


def test_projects_menschenlesbar(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(
            200, {"projects": [make_project_payload(slug="a", name="A")]}
        )

    _patch_transport(monkeypatch, handler)

    code = cli.main(["projects"])

    assert code == 0
    assert "A" in capsys.readouterr().out


def test_kein_token_flag_im_parser() -> None:
    """Regressionsschutz für Entscheidung 6: kein `--token`-Flag, das ein
    Geheimnis in Shell-History/Prozessliste landen ließe."""
    parser = cli._build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["--token", "irgendwas", "projects"])
