"""CLI-Verhalten: menschenlesbare Ausgabe vs. `--json`, Exit-Codes.

`dbrain.cli.BrainClient` wird durch eine Fabrik ersetzt, die einen Client
mit `httpx.MockTransport` statt echtem HTTP liefert — dieselbe Technik wie
in `tests/conftest.py`, nur von der CLI-Seite aus angestoßen.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from dbrain import cli
from dbrain.client import BrainClient
from tests.conftest import (
    json_response,
    make_hit,
    make_project_payload,
    make_review_entry_payload,
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


def test_review_list_menschenlesbar(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import uuid

    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(200, [make_review_entry_payload(title="Wartend")])

    _patch_transport(monkeypatch, handler)

    code = cli.main(["review", "list", "--project", str(uuid.uuid4())])

    assert code == 0
    ausgabe = capsys.readouterr().out
    assert "Wartend" in ausgabe
    assert "{" not in ausgabe


def test_review_approve_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import uuid

    entry_id = uuid.uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(
            200, make_review_entry_payload(entry_id=str(entry_id), status="active")
        )

    _patch_transport(monkeypatch, handler)

    code = cli.main(
        [
            "--json",
            "review",
            "approve",
            "--project",
            str(uuid.uuid4()),
            "--entry",
            str(entry_id),
        ]
    )

    assert code == 0
    assert '"status": "active"' in capsys.readouterr().out


def test_review_reject_gibt_exit_code_0(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Anders als ein abgelehntes `store` ist ein Review-`reject` eine
    **erfolgreiche** Kuratierungsentscheidung — Exit 0, nicht 1."""
    import uuid

    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(200, make_review_entry_payload(status="archived"))

    _patch_transport(monkeypatch, handler)

    code = cli.main(
        [
            "review",
            "reject",
            "--project",
            str(uuid.uuid4()),
            "--entry",
            str(uuid.uuid4()),
        ]
    )

    assert code == 0
    assert "status=archived" in capsys.readouterr().out


def test_review_edit_superseded_by(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import uuid

    ersetzt_durch = uuid.uuid4()
    aufgezeichnete_koerper: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PATCH":
            import json as _json

            aufgezeichnete_koerper.append(_json.loads(request.content))
        return json_response(
            200, make_review_entry_payload(superseded_by=str(ersetzt_durch))
        )

    _patch_transport(monkeypatch, handler)

    code = cli.main(
        [
            "review",
            "edit",
            "--project",
            str(uuid.uuid4()),
            "--entry",
            str(uuid.uuid4()),
            "--superseded-by",
            str(ersetzt_durch),
        ]
    )

    assert code == 0
    assert f"superseded_by={ersetzt_durch}" in capsys.readouterr().out
    assert aufgezeichnete_koerper == [{"superseded_by": str(ersetzt_durch)}]


def test_review_erfordert_unterbefehl(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main(["review"])


def test_kein_token_flag_im_parser() -> None:
    """Regressionsschutz für Entscheidung 6: kein `--token`-Flag, das ein
    Geheimnis in Shell-History/Prozessliste landen ließe."""
    parser = cli._build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["--token", "irgendwas", "projects"])
