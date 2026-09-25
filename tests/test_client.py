"""Die Client-Logik: Retry-Semantik, Fehler-Mapping, Slug-Auflösung.

Zentrale Zusage aus dem Design-Brief (Entscheidung 5): `search()` und
`list_projects()` sind ohne Nebenwirkung und retryen jeden 5xx/Timeout.
`store()`/`feedback()` retryen nur einen Verbindungsfehler *vor* einer
Antwort — ein 5xx *nach* einer Antwort wird als `BrainAmbiguousError`
durchgereicht, nicht automatisch wiederholt.
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest

from dbrain.client import (
    DEFAULT_TIMEOUT,
    MAX_RETRY_AFTER,
    SYNC_TIMEOUT,
    UPSERT_TIMEOUT,
    BrainClient,
)
from dbrain.exceptions import (
    BrainAmbiguousError,
    BrainAuthError,
    BrainConnectionError,
    BrainHTTPError,
    BrainLockConflictError,
    BrainNotFoundError,
    BrainRateLimitError,
    BrainValidationError,
)
from dbrain.models import SubmissionResult, SyncEntry
from tests.conftest import (
    TOKEN,
    json_response,
    make_client,
    make_hit,
    make_project_payload,
    make_review_entry_payload,
    make_search_payload,
    make_submission_payload,
    make_sync_payload,
    make_sync_rejection,
)


def test_search_gibt_treffer_zurueck() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        return json_response(200, make_search_payload(hits=[make_hit()]))

    with make_client(handler) as client:
        ergebnis = client.search("migration")

    assert len(ergebnis.hits) == 1
    assert ergebnis.hits[0].project_slug == "a"


def test_search_retryt_5xx_und_gelingt_danach() -> None:
    versuche = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal versuche
        versuche += 1
        if versuche < 3:
            return httpx.Response(503)
        return json_response(200, make_search_payload())

    with make_client(handler) as client:
        client.search("migration")

    assert versuche == 3


def test_search_gibt_nach_max_retries_auf() -> None:
    versuche = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal versuche
        versuche += 1
        return httpx.Response(503)

    from dbrain.exceptions import BrainHTTPError

    with make_client(handler, max_retries=2) as client, pytest.raises(BrainHTTPError):
        client.search("migration")

    assert versuche == 3  # erster Versuch + 2 Retries


def test_store_retryt_verbindungsfehler_vor_antwort() -> None:
    versuche = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal versuche
        versuche += 1
        if versuche < 2:
            raise httpx.ConnectError("kaputt")
        return json_response(201, make_submission_payload())

    with make_client(handler) as client:
        ergebnis = client.store(
            project=str(__import__("uuid").uuid4()),
            title="Titel",
            content="Inhalt",
            source="test",
        )

    assert ergebnis.verdict == "stored"
    assert versuche == 2


def test_store_wiederholt_readtimeout_nach_gesendetem_request_nicht() -> None:
    """Ein `ReadTimeout` beweist NICHT, dass der Server den Request nie
    sah — anders als `ConnectError`. Für `store()` darf das deshalb nicht
    automatisch retryt werden."""
    versuche = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal versuche
        versuche += 1
        raise httpx.ReadTimeout("Server hat schon alles gelesen, Antwort kam nie an")

    with make_client(handler) as client, pytest.raises(BrainAmbiguousError):
        client.store(
            project=str(__import__("uuid").uuid4()),
            title="Titel",
            content="Inhalt",
            source="test",
        )

    assert versuche == 1  # kein einziger Retry


def test_search_retryt_readtimeout() -> None:
    """`search()` ist ohne Nebenwirkung — ein `ReadTimeout` darf hier
    weiterhin bedenkenlos retryt werden, anders als bei `store()`."""
    versuche = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal versuche
        versuche += 1
        if versuche < 2:
            raise httpx.ReadTimeout("kurz weg")
        return json_response(200, make_search_payload())

    with make_client(handler) as client:
        client.search("migration")

    assert versuche == 2


def test_store_retryt_connect_timeout_vor_antwort() -> None:
    """`ConnectTimeout`/`PoolTimeout` beweisen wie `ConnectError`, dass der
    Server nichts sah — auch für `store()` sicher retryable."""
    versuche = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal versuche
        versuche += 1
        if versuche < 2:
            raise httpx.ConnectTimeout("Verbindung kam nie zustande")
        return json_response(201, make_submission_payload())

    with make_client(handler) as client:
        ergebnis = client.store(
            project=str(__import__("uuid").uuid4()),
            title="Titel",
            content="Inhalt",
            source="test",
        )

    assert ergebnis.verdict == "stored"
    assert versuche == 2


def test_store_wiederholt_5xx_nach_antwort_nicht() -> None:
    """Der Kern von Entscheidung 5: Ein 500 *nach* Serverantwort ist für
    `store()` nicht automatisch retryable — der Server könnte bereits
    committet haben."""
    versuche = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal versuche
        versuche += 1
        return httpx.Response(500, text="kaputt")

    with make_client(handler) as client, pytest.raises(BrainAmbiguousError):
        client.store(
            project=str(__import__("uuid").uuid4()),
            title="Titel",
            content="Inhalt",
            source="test",
        )

    assert versuche == 1  # kein einziger Retry


def test_store_rejected_ist_kein_fehler() -> None:
    """`rejected` ist ein normales `SubmissionResult`, keine Exception —
    ein Aufrufer soll die `findings` lesen können, statt nur einen Fehler
    zu fangen."""

    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(
            422,
            {
                "detail": make_submission_payload(
                    verdict="rejected",
                    entry_id=None,
                    status=None,
                    findings=[
                        {
                            "gate": "secret-scan",
                            "code": "aws-access-token",
                            "severity": "reject",
                            "field": "content",
                            "hint": "sieht aus wie ein Geheimnis",
                            "reference": None,
                        }
                    ],
                )
            },
        )

    with make_client(handler) as client:
        ergebnis = client.store(
            project=str(__import__("uuid").uuid4()),
            title="Titel",
            content="AKIAIOSFODNN7EXAMPLE",
            source="test",
        )

    assert ergebnis.verdict == "rejected"
    assert ergebnis.entry_id is None
    assert ergebnis.findings[0].code == "aws-access-token"


def test_store_schema_fehler_bleibt_eine_exception() -> None:
    """Eine 422 mit Listenform (`detail` ist eine Liste, FastAPIs
    Schema-Validierung) ist ein echter Fehler — anders als der
    `rejected`-Verdict oben (`detail` ist ein Objekt mit `verdict`)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(
            422, {"detail": [{"loc": ["body", "title"], "msg": "zu kurz", "type": "x"}]}
        )

    with make_client(handler) as client, pytest.raises(BrainValidationError):
        client.store(
            project=str(__import__("uuid").uuid4()),
            title="",
            content="Inhalt",
            source="test",
        )


def test_401_wird_zu_brain_auth_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(401, {"detail": "kein Token"})

    with make_client(handler) as client, pytest.raises(BrainAuthError):
        client.search("migration")


def test_404_wird_zu_brain_not_found_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(404, {"detail": "Eintrag nicht gefunden"})

    with make_client(handler) as client, pytest.raises(BrainNotFoundError):
        client.feedback(str(__import__("uuid").uuid4()), helpful=True)


def test_429_wartet_retry_after_und_wiederholt() -> None:
    versuche = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal versuche
        versuche += 1
        if versuche == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return json_response(200, make_search_payload())

    with make_client(handler) as client:
        client.search("migration")

    assert versuche == 2


def test_429_gilt_auch_fuer_nicht_idempotente_aufrufe() -> None:
    """Ausnahme von der Idempotenz-Regel: Das Rate-Limit greift vor jeder
    Schreiblogik, ein 429 heißt also „nie verarbeitet" — unabhängig von
    der Methode."""
    versuche = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal versuche
        versuche += 1
        if versuche == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return json_response(201, make_submission_payload())

    with make_client(handler) as client:
        ergebnis = client.store(
            project=str(__import__("uuid").uuid4()),
            title="Titel",
            content="Inhalt",
            source="test",
        )

    assert ergebnis.verdict == "stored"
    assert versuche == 2


def test_429_gibt_nach_max_retries_brain_rate_limit_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "0"})

    with (
        make_client(handler, max_retries=1) as client,
        pytest.raises(BrainRateLimitError),
    ):
        client.search("migration")


def test_verbindungsfehler_gibt_nach_max_retries_auf() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("kaputt")

    with (
        make_client(handler, max_retries=1) as client,
        pytest.raises(BrainConnectionError),
    ):
        client.search("migration")


def test_store_loest_slug_ueber_list_projects_auf() -> None:
    aufgerufene_pfade = []

    def handler(request: httpx.Request) -> httpx.Response:
        aufgerufene_pfade.append(request.url.path)
        if request.url.path == "/v1/projects":
            return json_response(
                200, {"projects": [make_project_payload(slug="mein-projekt")]}
            )
        return json_response(201, make_submission_payload())

    with make_client(handler) as client:
        client.store(
            project="mein-projekt", title="Titel", content="Inhalt", source="test"
        )

    assert aufgerufene_pfade[0] == "/v1/projects"
    assert aufgerufene_pfade[1].startswith("/v1/projects/")


def test_store_mit_uuid_ueberspringt_die_aufloesung() -> None:
    import uuid

    projekt_id = uuid.uuid4()
    aufgerufene_pfade = []

    def handler(request: httpx.Request) -> httpx.Response:
        aufgerufene_pfade.append(request.url.path)
        return json_response(201, make_submission_payload())

    with make_client(handler) as client:
        client.store(
            project=str(projekt_id), title="Titel", content="Inhalt", source="test"
        )

    assert aufgerufene_pfade == [f"/v1/projects/{projekt_id}/entries"]


def test_store_mit_unbekanntem_slug_wirft_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(200, {"projects": []})

    with make_client(handler) as client, pytest.raises(BrainNotFoundError):
        client.store(
            project="unbekannt", title="Titel", content="Inhalt", source="test"
        )


def test_list_review_queue_gibt_wartende_eintraege_zurueck() -> None:
    import uuid

    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(200, [make_review_entry_payload(title="Wartend")])

    with make_client(handler) as client:
        eintraege = client.list_review_queue(str(uuid.uuid4()))

    assert len(eintraege) == 1
    assert eintraege[0].title == "Wartend"
    assert eintraege[0].status == "pending_review"


def test_list_review_queue_traegt_limit_offset_als_query_parameter() -> None:
    import uuid

    aufgezeichnete_query: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        aufgezeichnete_query.update(dict(request.url.params))
        return json_response(200, [])

    with make_client(handler) as client:
        client.list_review_queue(str(uuid.uuid4()), limit=5, offset=10)

    assert aufgezeichnete_query == {"limit": "5", "offset": "10"}


def test_list_review_queue_retryt_5xx_wie_search() -> None:
    """Ohne Nebenwirkung wie `search()`/`list_projects()` — ein 5xx wird
    bedenkenlos retryt."""
    import uuid

    versuche = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal versuche
        versuche += 1
        if versuche < 2:
            return httpx.Response(503)
        return json_response(200, [])

    with make_client(handler) as client:
        client.list_review_queue(str(uuid.uuid4()))

    assert versuche == 2


def test_approve_review_gibt_aktualisierten_eintrag_zurueck() -> None:
    import uuid

    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(200, make_review_entry_payload(status="active"))

    with make_client(handler) as client:
        ergebnis = client.approve_review(str(uuid.uuid4()), uuid.uuid4())

    assert ergebnis.status == "active"


def test_reject_review_schreibt_kein_zweites_mal_bei_5xx_nach_antwort() -> None:
    """Dieselbe Ambiguitäts-Regel wie `store()`/`feedback()`: Ein 500
    *nach* einer Antwort ist für eine schreibende Kuratierungsaktion nicht
    automatisch retryable."""
    import uuid

    versuche = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal versuche
        versuche += 1
        return httpx.Response(500, text="kaputt")

    with make_client(handler) as client, pytest.raises(BrainAmbiguousError):
        client.reject_review(str(uuid.uuid4()), uuid.uuid4())

    assert versuche == 1


def test_edit_review_sendet_nur_gesetzte_felder() -> None:
    import uuid

    ersetzt_durch = uuid.uuid4()
    aufgezeichnete_koerper: list[object] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        aufgezeichnete_koerper.append(_json.loads(request.content))
        return json_response(
            200, make_review_entry_payload(superseded_by=str(ersetzt_durch))
        )

    with make_client(handler) as client:
        ergebnis = client.edit_review(
            str(uuid.uuid4()), uuid.uuid4(), superseded_by=ersetzt_durch
        )

    assert ergebnis.superseded_by == ersetzt_durch
    assert aufgezeichnete_koerper == [{"superseded_by": str(ersetzt_durch)}]


def test_list_projects_toleriert_unbekannte_felder() -> None:
    """Client-seitige Hälfte der Additivstabilität von `/v1`
    (ADR-004-Amendment-001): ein zusätzliches, unbekanntes Feld in der
    Server-Antwort darf den Client nicht brechen."""

    def handler(request: httpx.Request) -> httpx.Response:
        payload = make_project_payload()
        payload["ein_kuenftiges_feld"] = "sollte ignoriert werden"
        return json_response(200, {"projects": [payload]})

    with make_client(handler) as client:
        projekte = client.list_projects()

    assert projekte[0].slug == "a"


# --- #206: Suche mit Schwelle, Degradation und Schlüssel -------------------------


def test_search_traegt_max_cosine_distance_und_liest_vector_branch() -> None:
    gesehen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        gesehen.update(json.loads(request.content))
        return json_response(
            200,
            make_search_payload(
                vector_branch="unavailable",
                hits=[make_hit(external_key="core.haushalt~name")],
            ),
        )

    with make_client(handler) as client:
        ergebnis = client.search("haushalt", max_cosine_distance=0.4)

    assert gesehen["max_cosine_distance"] == 0.4
    assert ergebnis.vector_branch == "unavailable"
    assert ergebnis.degraded is True
    assert ergebnis.hits[0].external_key == "core.haushalt~name"


def test_search_ohne_neue_felder_gilt_der_vektorzweig_als_ok() -> None:
    """Additiv-tolerant: Ein älterer Server kennt `vector_branch` und
    `external_key` nicht — das ist kein Fehler und keine Degradation."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert "max_cosine_distance" not in json.loads(request.content)
        return json_response(200, make_search_payload(hits=[make_hit()]))

    with make_client(handler) as client:
        ergebnis = client.search("haushalt")

    assert ergebnis.vector_branch == "ok"
    assert ergebnis.degraded is False
    assert ergebnis.hits[0].external_key is None


# --- #206: upsert ----------------------------------------------------------------


def _upsert(
    client: BrainClient, projekt: str, schluessel: str = "core.a~b"
) -> SubmissionResult:
    return client.upsert(
        project=projekt,
        external_key=schluessel,
        title="Titel",
        content="Inhalt",
        source="ci",
    )


def test_upsert_sendet_put_an_den_schluesselpfad_und_liest_das_urteil() -> None:
    projekt = str(uuid.uuid4())
    gesehen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        gesehen["methode"] = request.method
        gesehen["pfad"] = request.url.path
        gesehen["body"] = json.loads(request.content)
        return json_response(
            201, make_submission_payload(replaced=False, external_key="core.a~b")
        )

    with make_client(handler) as client:
        ergebnis = client.upsert(
            project=projekt,
            external_key="core.a~b",
            title="Titel",
            content="Inhalt",
            source="ci",
            tags=["help"],
        )

    assert gesehen["methode"] == "PUT"
    assert gesehen["pfad"] == f"/v1/projects/{projekt}/entries/by-key/core.a~b"
    assert gesehen["body"] == {
        "title": "Titel",
        "content": "Inhalt",
        "source": "ci",
        "confidence": 0.5,
        "tags": ["help"],
    }
    assert ergebnis.verdict == "stored"
    assert ergebnis.replaced is False
    assert ergebnis.external_key == "core.a~b"


def test_upsert_meldet_ersetzt_und_merged() -> None:
    antworten = iter(
        [
            json_response(200, make_submission_payload(replaced=True)),
            json_response(
                200,
                make_submission_payload(
                    verdict="merged", entry_id=str(uuid.uuid4()), replaced=False
                ),
            ),
        ]
    )

    with make_client(lambda request: next(antworten)) as client:
        ersetzt = _upsert(client, str(uuid.uuid4()))
        unveraendert = _upsert(client, str(uuid.uuid4()))

    assert ersetzt.replaced is True
    assert unveraendert.verdict == "merged"


def test_upsert_rejected_ist_kein_fehler() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(
            422, {"detail": make_submission_payload(verdict="rejected", entry_id=None)}
        )

    with make_client(handler) as client:
        ergebnis = _upsert(client, str(uuid.uuid4()))

    assert ergebnis.verdict == "rejected"


def test_upsert_schemafehler_bleibt_eine_exception() -> None:
    """Ein ungültiger Schlüssel ist ein 422 mit Liste — kein Urteil."""

    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(422, {"detail": [{"loc": ["path", "external_key"]}]})

    with make_client(handler) as client, pytest.raises(BrainValidationError):
        _upsert(client, str(uuid.uuid4()))


def test_upsert_kodiert_eine_raute_statt_den_schluessel_zu_kuerzen() -> None:
    """Der Fehler, der auf dem Server gefunden wurde: `httpx` schnitte `#`
    als URL-Fragment ab, und der Server sähe still `core.haushalt` statt
    `core.haushalt#name` — ein Upsert träfe den falschen Eintrag. Kodiert
    kommt der volle Schlüssel an und scheitert am Formatprüfer (422)."""
    gesehen: dict[str, bytes] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        gesehen["raw"] = request.url.raw_path
        return json_response(422, {"detail": [{"msg": "Schlüsselformat"}]})

    with make_client(handler) as client, pytest.raises(BrainValidationError):
        _upsert(client, str(uuid.uuid4()), schluessel="core.haushalt#name")

    assert gesehen["raw"].endswith(b"/entries/by-key/core.haushalt%23name")


def test_upsert_retryt_einen_readtimeout_weil_idempotent() -> None:
    versuche = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal versuche
        versuche += 1
        if versuche == 1:
            raise httpx.ReadTimeout("zu langsam", request=request)
        return json_response(200, make_submission_payload(verdict="merged"))

    with make_client(handler) as client:
        ergebnis = _upsert(client, str(uuid.uuid4()))

    assert versuche == 2
    assert ergebnis.verdict == "merged"


def test_upsert_hat_einen_eigenen_timeout() -> None:
    gesehen: dict[str, float] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        gesehen["read"] = request.extensions["timeout"]["read"]
        return json_response(201, make_submission_payload())

    with make_client(handler) as client:
        _upsert(client, str(uuid.uuid4()))

    assert gesehen["read"] == UPSERT_TIMEOUT


# --- #206: remove ----------------------------------------------------------------


def test_remove_archiviert_ueber_den_schluessel() -> None:
    projekt = str(uuid.uuid4())
    gesehen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        gesehen["methode"] = request.method
        gesehen["pfad"] = request.url.path
        return httpx.Response(204)

    with make_client(handler) as client:
        client.remove(projekt, "core.a~b")  # kein Rückgabewert, kein Fehler

    assert gesehen == {
        "methode": "DELETE",
        "pfad": f"/v1/projects/{projekt}/entries/by-key/core.a~b",
    }


def test_remove_unbekannter_schluessel_ist_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(404, {"detail": "Eintrag nicht gefunden"})

    with make_client(handler) as client, pytest.raises(BrainNotFoundError):
        client.remove(str(uuid.uuid4()), "core.weg")


def test_remove_retryt_5xx_weil_idempotent() -> None:
    versuche = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal versuche
        versuche += 1
        return httpx.Response(503 if versuche == 1 else 204)

    with make_client(handler) as client:
        client.remove(str(uuid.uuid4()), "core.a")

    assert versuche == 2


# --- #206: sync ------------------------------------------------------------------


def test_sync_sendet_das_manifest_und_liest_das_ergebnis() -> None:
    projekt = str(uuid.uuid4())
    gesehen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        gesehen["methode"] = request.method
        gesehen["pfad"] = request.url.path
        gesehen["body"] = json.loads(request.content)
        gesehen["read"] = request.extensions["timeout"]["read"]
        return json_response(
            200,
            make_sync_payload(
                archived=["core.alt"],
                counts={"stored": 1, "replaced": 0, "merged": 0, "archived": 1},
            ),
        )

    with make_client(handler) as client:
        ergebnis = client.sync(
            projekt,
            source_revision="abc123",
            entries=[
                SyncEntry("core.a", "Titel", "Inhalt", "ci"),
                SyncEntry("core.b", "T", "I", "ci", category="help", tags=("x", "y")),
            ],
        )

    assert gesehen["methode"] == "PUT"
    assert gesehen["pfad"] == f"/v1/projects/{projekt}/sync"
    assert gesehen["read"] == SYNC_TIMEOUT
    assert gesehen["body"] == {
        "source_revision": "abc123",
        "entries": [
            {
                "external_key": "core.a",
                "title": "Titel",
                "content": "Inhalt",
                "source": "ci",
                "confidence": 0.5,
            },
            {
                "external_key": "core.b",
                "title": "T",
                "content": "I",
                "source": "ci",
                "confidence": 0.5,
                "category": "help",
                "tags": ["x", "y"],
            },
        ],
    }
    assert ergebnis.ok is True
    assert ergebnis.verdict == "applied"
    assert ergebnis.archived == ("core.alt",)
    assert ergebnis.counts is not None and ergebnis.counts.archived == 1
    assert ergebnis.results[0].external_key == "core.a"


def test_sync_timeout_ist_ueberschreibbar() -> None:
    gesehen: dict[str, float] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        gesehen["read"] = request.extensions["timeout"]["read"]
        return json_response(200, make_sync_payload())

    with make_client(handler) as client:
        client.sync(str(uuid.uuid4()), source_revision="r", entries=[], timeout=900.0)

    assert gesehen["read"] == 900.0


def test_sync_ohne_eintraege_sendet_eine_leere_liste() -> None:
    """Ein leeres Manifest archiviert den ganzen Bestand — der Server
    entscheidet, das SDK sendet es unverändert."""
    gesehen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        gesehen.update(json.loads(request.content))
        return json_response(200, make_sync_payload(results=[], archived=["core.a"]))

    with make_client(handler) as client:
        ergebnis = client.sync(str(uuid.uuid4()), source_revision="r", entries=[])

    assert gesehen["entries"] == []
    assert ergebnis.archived == ("core.a",)


def test_sync_ablehnung_ist_ein_ergebnis_keine_exception() -> None:
    """Wie `store()`s `rejected`: Ein Aufrufer, der nur Exceptions fängt,
    sähe die Begründung sonst nie. Nichts wurde geschrieben, `results`
    nennt alle Urteile."""

    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(422, {"detail": make_sync_rejection()})

    with make_client(handler) as client:
        ergebnis = client.sync(
            str(uuid.uuid4()),
            source_revision="abc123",
            entries=[SyncEntry("core.geheim", "T", "I", "ci")],
        )

    assert ergebnis.ok is False
    assert ergebnis.verdict == "rejected"
    assert ergebnis.counts is None and ergebnis.archived == ()
    assert [r.external_key for r in ergebnis.results] == ["core.gut", "core.geheim"]
    assert ergebnis.results[1].findings[0].code == "aws-access-token"


@pytest.mark.parametrize(
    "detail",
    [
        [{"loc": ["body", "entries"], "msg": "doppelte Schlüssel"}],
        "Manifest-Sync ist nur für synchronisierte Projekte zulässig",
    ],
)
def test_sync_schemafehler_und_kuratiertes_projekt_sind_exceptions(
    detail: object,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(422, {"detail": detail})

    with make_client(handler) as client, pytest.raises(BrainValidationError):
        client.sync(str(uuid.uuid4()), source_revision="r", entries=[])


def test_sync_zu_grosses_manifest_ist_ein_http_fehler() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(413, {"detail": "Manifest ist zu groß"})

    with make_client(handler) as client, pytest.raises(BrainHTTPError) as gefangen:
        client.sync(str(uuid.uuid4()), source_revision="r", entries=[])

    assert gefangen.value.status_code == 413


# --- #206: 409 mit Retry-After = Zeilensperre, nichts geschrieben -----------------


def test_409_mit_retry_after_wird_wiederholt_auch_bei_schreibenden_aufrufen() -> None:
    """Der Server antwortet so, wenn die Anfrage an einer Zeilensperre
    abgebrochen wurde (laufender Sync, Deadlock) — nichts geschrieben, für
    jede Methode sicher retryable, auch für `store()`."""
    versuche = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal versuche
        versuche += 1
        if versuche == 1:
            return httpx.Response(
                409, headers={"Retry-After": "0"}, json={"detail": "kollidiert"}
            )
        return json_response(201, make_submission_payload())

    with make_client(handler) as client:
        ergebnis = client.store(
            project=str(uuid.uuid4()), title="T", content="I", source="s"
        )

    assert ergebnis.verdict == "stored"
    assert versuche == 2


def test_409_ohne_retry_after_ist_ein_fachlicher_konflikt_und_wird_nie_wiederholt() -> (
    None
):
    """`approve_review()` auf einen Eintrag, der nicht zur Prüfung ansteht,
    ist ebenfalls ein 409 — aber ein dauerhafter. Das `Retry-After` ist das
    Unterscheidungsmerkmal."""
    versuche = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal versuche
        versuche += 1
        return json_response(409, {"detail": "Eintrag steht nicht zur Prüfung"})

    with make_client(handler) as client, pytest.raises(BrainHTTPError) as gefangen:
        client.approve_review(str(uuid.uuid4()), str(uuid.uuid4()))

    assert versuche == 1
    assert gefangen.value.status_code == 409
    assert not isinstance(gefangen.value, BrainLockConflictError)


def test_409_mit_retry_after_gibt_nach_max_retries_lock_conflict_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, headers={"Retry-After": "0"}, json={"detail": "x"})

    with (
        make_client(handler, max_retries=1) as client,
        pytest.raises(BrainLockConflictError),
    ):
        client.remove(str(uuid.uuid4()), "core.a")


def test_antworten_ohne_die_neuen_felder_bleiben_lesbar() -> None:
    """Additiv-tolerant: `replaced` und `external_key` fehlen bei einem
    älteren Server."""

    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(201, make_submission_payload())

    with make_client(handler) as client:
        ergebnis = client.store(
            project=str(uuid.uuid4()), title="T", content="I", source="s"
        )

    assert ergebnis.replaced is False
    assert ergebnis.external_key is None


# --- Härtung: Retry-After begrenzt, connect-Timeout bleibt, upsert-Timeout -------


@pytest.mark.parametrize(
    ("header", "erwartet"),
    [
        ("1e9", MAX_RETRY_AFTER),  # feindlich oder fehlkonfiguriert: gedeckelt
        ("inf", 1.0),  # nicht endlich → wie ein fehlender Header
        ("nan", 1.0),
        ("-5", 0.0),  # keine Wartezeit
        ("Wed, 21 Oct 2026 07:28:00 GMT", 1.0),  # HTTP-Datum: nicht unterstützt
        ("3", 3.0),
    ],
)
def test_retry_after_wird_begrenzt_und_unlesbares_faellt_auf_eine_sekunde(
    monkeypatch: pytest.MonkeyPatch, header: str, erwartet: float
) -> None:
    geschlafen: list[float] = []
    monkeypatch.setattr("dbrain.client.time.sleep", geschlafen.append)
    versuche = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal versuche
        versuche += 1
        if versuche == 1:
            return httpx.Response(429, headers={"Retry-After": header})
        return json_response(200, make_search_payload())

    with make_client(handler) as client:
        client.search("x")

    assert geschlafen == [erwartet]


def test_ein_aufruf_timeout_laesst_den_connect_timeout_des_clients_stehen() -> None:
    """Ein float gälte für alle Phasen: Ein 300-s-Sync ließe einen nicht
    erreichbaren Server 300 s auf die Verbindung warten."""
    gesehen: dict[str, float] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        gesehen.update(request.extensions["timeout"])
        return json_response(200, make_sync_payload())

    with make_client(handler) as client:
        client.sync(str(uuid.uuid4()), source_revision="r", entries=[])

    assert gesehen["read"] == SYNC_TIMEOUT
    assert gesehen["connect"] == DEFAULT_TIMEOUT


def test_upsert_timeout_ist_ueberschreibbar() -> None:
    gesehen: dict[str, float] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        gesehen["read"] = request.extensions["timeout"]["read"]
        return json_response(201, make_submission_payload())

    with make_client(handler) as client:
        client.upsert(
            project=str(uuid.uuid4()),
            external_key="core.a",
            title="Titel",
            content="Inhalt",
            source="ci",
            timeout=120.0,
        )

    assert gesehen["read"] == 120.0
