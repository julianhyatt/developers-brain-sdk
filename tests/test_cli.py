"""CLI-Verhalten: menschenlesbare Ausgabe vs. `--json`, Exit-Codes.

`dbrain.cli.BrainClient` wird durch eine Fabrik ersetzt, die einen Client
mit `httpx.MockTransport` statt echtem HTTP liefert — dieselbe Technik wie
in `tests/conftest.py`, nur von der CLI-Seite aus angestoßen.
"""

from __future__ import annotations

import io
import json
import uuid
from collections.abc import Callable
from pathlib import Path

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
    make_sync_payload,
    make_sync_rejection,
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


# --- #206: Suche mit Schwelle, Degradation ---------------------------------------


def test_search_reicht_die_schwelle_durch(monkeypatch: pytest.MonkeyPatch) -> None:
    gesehen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        gesehen.update(json.loads(request.content))
        return json_response(200, make_search_payload())

    _patch_transport(monkeypatch, handler)

    assert cli.main(["search", "x", "--max-cosine-distance", "0.4"]) == 0
    assert gesehen["max_cosine_distance"] == 0.4


def test_search_warnt_auf_stderr_wenn_der_vektorzweig_fehlt(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Auf stderr, damit `--json` und Pipes sauber bleiben — aber sichtbar:
    Eine leere Liste ohne Vektorzweig heißt nicht „gibt es nicht"."""

    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(200, make_search_payload(vector_branch="unavailable"))

    _patch_transport(monkeypatch, handler)

    assert cli.main(["--json", "search", "x"]) == 0

    ausgabe = capsys.readouterr()
    assert "Vektorzweig nicht verfügbar" in ausgabe.err
    assert json.loads(ausgabe.out)["vector_branch"] == "unavailable"


def test_search_schweigt_wenn_der_vektorzweig_lief(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(200, make_search_payload())

    _patch_transport(monkeypatch, handler)

    cli.main(["search", "x"])

    assert capsys.readouterr().err == ""


# --- #206: store --external-key, remove ------------------------------------------


def test_store_mit_external_key_ersetzt_per_put(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    projekt = str(uuid.uuid4())
    gesehen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        gesehen["methode"] = request.method
        gesehen["pfad"] = request.url.path
        return json_response(200, make_submission_payload(replaced=True))

    _patch_transport(monkeypatch, handler)

    code = cli.main(
        [
            "store",
            "--project",
            projekt,
            "--external-key",
            "core.a~b",
            "--title",
            "t",
            "--content",
            "c",
            "--source",
            "s",
        ]
    )

    assert code == 0
    assert gesehen == {
        "methode": "PUT",
        "pfad": f"/v1/projects/{projekt}/entries/by-key/core.a~b",
    }
    assert "replaced=true" in capsys.readouterr().out


def test_store_ohne_external_key_bleibt_ein_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    methoden: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methoden.append(request.method)
        return json_response(201, make_submission_payload())

    _patch_transport(monkeypatch, handler)

    cli.main(
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

    assert methoden == ["POST"]


def test_remove_archiviert_und_meldet_es(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    methoden: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methoden.append(request.method)
        return httpx.Response(204)

    _patch_transport(monkeypatch, handler)

    code = cli.main(
        ["remove", "--project", str(uuid.uuid4()), "--external-key", "core.a"]
    )

    assert code == 0
    assert methoden == ["DELETE"]
    assert "external_key=core.a removed=true" in capsys.readouterr().out


def test_remove_unbekannter_schluessel_gibt_exit_code_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(404, {"detail": "Eintrag nicht gefunden"})

    _patch_transport(monkeypatch, handler)

    code = cli.main(
        ["remove", "--project", str(uuid.uuid4()), "--external-key", "core.weg"]
    )

    assert code == 1
    assert "Fehler" in capsys.readouterr().err


# --- #206: sync ------------------------------------------------------------------


def _manifest(tmp_path: Path, inhalt: object) -> Path:
    datei = tmp_path / "manifest.json"
    datei.write_text(json.dumps(inhalt), encoding="utf-8")
    return datei


def _eintrag(**abweichungen: object) -> dict[str, object]:
    eintrag: dict[str, object] = {
        "external_key": "core.a",
        "title": "Titel",
        "content": "Inhalt",
        "source": "ci",
    }
    eintrag.update(abweichungen)
    return eintrag


def _sync_argumente(datei: Path, *zusatz: str) -> list[str]:
    return ["sync", "--project", str(uuid.uuid4()), "--manifest", str(datei), *zusatz]


def _kein_netz(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """Ein Handler, der jede Anfrage mitschreibt — für die Tests, die beweisen
    sollen, dass ein formal kaputtes Manifest **nie** einen Sync anstößt."""
    gesehen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        gesehen.append(request)
        return json_response(200, make_sync_payload())

    _patch_transport(monkeypatch, handler)
    return gesehen


def test_sync_wendet_das_manifest_an_und_meldet_die_zaehler(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    gesehen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        gesehen.update(json.loads(request.content))
        return json_response(
            200,
            make_sync_payload(
                archived=["core.alt"],
                counts={"stored": 1, "replaced": 2, "merged": 3, "archived": 1},
            ),
        )

    _patch_transport(monkeypatch, handler)
    datei = _manifest(
        tmp_path,
        {
            "source_revision": "abc123",
            "entries": [_eintrag(tags=["x"], confidence=0.9)],
        },
    )

    code = cli.main(_sync_argumente(datei))

    assert code == 0
    assert gesehen["source_revision"] == "abc123"
    assert gesehen["entries"] == [
        {
            "external_key": "core.a",
            "title": "Titel",
            "content": "Inhalt",
            "source": "ci",
            "confidence": 0.9,
            "tags": ["x"],
        }
    ]
    ausgabe = capsys.readouterr().out
    assert "verdict=applied" in ausgabe
    assert "stored=1 replaced=2 merged=3 archived=1" in ausgabe
    assert "archiviert: core.alt" in ausgabe


def test_sync_ablehnung_gibt_exit_code_1_und_nennt_jeden_befund(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(422, {"detail": make_sync_rejection()})

    _patch_transport(monkeypatch, handler)
    datei = _manifest(tmp_path, {"source_revision": "abc123", "entries": [_eintrag()]})

    code = cli.main(_sync_argumente(datei))

    assert code == 1
    ausgabe = capsys.readouterr().out
    assert "verdict=rejected" in ausgabe
    assert "core.geheim: [reject] secret-scan/aws-access-token" in ausgabe


def test_sync_source_revision_flag_ueberschreibt_das_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gesehen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        gesehen.update(json.loads(request.content))
        return json_response(200, make_sync_payload())

    _patch_transport(monkeypatch, handler)
    datei = _manifest(tmp_path, {"source_revision": "alt", "entries": []})

    assert cli.main(_sync_argumente(datei, "--source-revision", "neu")) == 0
    assert gesehen["source_revision"] == "neu"


def test_sync_json_gibt_das_ergebnis_maschinenlesbar_aus(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    _patch_transport(
        monkeypatch, lambda request: json_response(200, make_sync_payload())
    )
    datei = _manifest(tmp_path, {"source_revision": "r", "entries": []})

    assert cli.main(["--json", *_sync_argumente(datei)]) == 0

    daten = json.loads(capsys.readouterr().out)
    assert daten["verdict"] == "applied"
    assert daten["counts"]["stored"] == 1


@pytest.mark.parametrize(
    ("manifest", "fragment"),
    [
        ({"entries": []}, "source_revision fehlt"),
        ({"source_revision": "r", "entries": "kaputt"}, "'entries' muss eine Liste"),
        ({"source_revision": "r", "eintraege": []}, "Unbekannte Schlüssel"),
        (
            {"source_revision": "r", "entries": [_eintrag(tag=["x"])]},
            "entries[0]: unbekannte Schlüssel: tag",
        ),
        (
            {"source_revision": "r", "entries": [{"external_key": "core.a"}]},
            "'title' fehlt",
        ),
        (
            {"source_revision": "r", "entries": [_eintrag(tags="x")]},
            "'tags' muss eine Liste",
        ),
        (
            {"source_revision": "r", "entries": [_eintrag(confidence="hoch")]},
            "'confidence' muss eine Zahl",
        ),
        ([], "muss ein JSON-Objekt"),
    ],
)
def test_sync_mit_formal_kaputtem_manifest_stoesst_nie_einen_sync_an(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    manifest: object,
    fragment: str,
) -> None:
    """Ein Tippfehler wie `tag` statt `tags` würde bei einem Sync, der
    Fehlendes archiviert, still Daten verlieren — deshalb strikt, und vor
    jedem Netzwerkzugriff."""
    gesehen = _kein_netz(monkeypatch)
    datei = _manifest(tmp_path, manifest)

    code = cli.main(_sync_argumente(datei))

    assert code == 1
    assert fragment in capsys.readouterr().err
    assert gesehen == []


def test_sync_mit_ungueltigem_json_gibt_exit_code_1(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    gesehen = _kein_netz(monkeypatch)
    datei = tmp_path / "manifest.json"
    datei.write_text("{nicht json", encoding="utf-8")

    assert cli.main(_sync_argumente(datei)) == 1
    assert "kein gültiges JSON" in capsys.readouterr().err
    assert gesehen == []


def test_sync_ohne_die_manifestdatei_gibt_exit_code_1(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    gesehen = _kein_netz(monkeypatch)

    assert cli.main(_sync_argumente(tmp_path / "gibt-es-nicht.json")) == 1
    assert "nicht lesbar" in capsys.readouterr().err
    assert gesehen == []


def test_sync_liest_das_manifest_von_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gesehen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        gesehen.update(json.loads(request.content))
        return json_response(200, make_sync_payload())

    _patch_transport(monkeypatch, handler)
    monkeypatch.setattr(
        "sys.stdin", io.StringIO(json.dumps({"source_revision": "r", "entries": []}))
    )

    code = cli.main(["sync", "--project", str(uuid.uuid4()), "--manifest", "-"])

    assert code == 0
    assert gesehen["source_revision"] == "r"


def test_sync_manifest_von_stdin_und_token_stdin_schliessen_sich_aus(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gesehen = _kein_netz(monkeypatch)

    code = cli.main(
        [
            "--token-stdin",
            "sync",
            "--project",
            str(uuid.uuid4()),
            "--manifest",
            "-",
        ]
    )

    assert code == 1
    assert "beide von stdin" in capsys.readouterr().err
    assert gesehen == []
