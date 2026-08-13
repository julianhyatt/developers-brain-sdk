"""Die Client-Logik: Retry-Semantik, Fehler-Mapping, Slug-Auflösung.

Zentrale Zusage aus dem Design-Brief (Entscheidung 5): `search()` und
`list_projects()` sind ohne Nebenwirkung und retryen jeden 5xx/Timeout.
`store()`/`feedback()` retryen nur einen Verbindungsfehler *vor* einer
Antwort — ein 5xx *nach* einer Antwort wird als `BrainAmbiguousError`
durchgereicht, nicht automatisch wiederholt.
"""

from __future__ import annotations

import httpx
import pytest

from dbrain.exceptions import (
    BrainAmbiguousError,
    BrainAuthError,
    BrainConnectionError,
    BrainNotFoundError,
    BrainRateLimitError,
    BrainValidationError,
)
from tests.conftest import (
    TOKEN,
    json_response,
    make_client,
    make_hit,
    make_project_payload,
    make_search_payload,
    make_submission_payload,
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

    with make_client(handler, max_retries=1) as client, pytest.raises(
        BrainRateLimitError
    ):
        client.search("migration")


def test_verbindungsfehler_gibt_nach_max_retries_auf() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("kaputt")

    with make_client(handler, max_retries=1) as client, pytest.raises(
        BrainConnectionError
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
