"""`BrainClient` — dünner HTTP-Client gegen die developers-brain-API
(`/v1/*`), keine Server-Abhängigkeit außer `httpx`.

## Retry+Backoff: transient UND idempotent, nicht nur transient

`search()` und `list_projects()` sind ohne Nebenwirkung — jeder
Verbindungsfehler, Timeout oder 5xx wird bedenkenlos wiederholt.

`store()`, `feedback()` und die drei schreibenden Kuratierungsaufrufe
(`approve_review()`/`reject_review()`/`edit_review()`, #93) schreiben, und
die Unterscheidung, die zählt, ist nicht *welcher* Fehler auftrat, sondern
*ob beweisbar ist*, dass der Request den Server nie erreicht hat:

- `ConnectError`/`ConnectTimeout`/`PoolTimeout` — die Verbindung kam nie
  zustande, der Server hat vom Request nichts gesehen. Retryable, für jede
  Methode, ob lesend oder schreibend.
- `ReadTimeout`/`WriteTimeout`/sonstige `TransportError` **nach**
  aufgebauter Verbindung — der Request könnte bereits vollständig
  gesendet und verarbeitet worden sein, das SDK kann es nicht
  unterscheiden. Für die lesenden Methoden (`search()`, `list_projects()`,
  `list_review_queue()`) unbedenklich (kein Nebeneffekt); für die
  schreibenden **nicht automatisch retryable** — dieselbe Ambiguität wie
  unten, als `BrainAmbiguousError`.
- Ein 5xx-Statuscode, also eine Antwort kam an, nur eine schlechte, heißt
  für eine schreibende Methode: unklar, ob committet wurde, bevor der
  Fehler zurückkam. **Nicht automatisch retryable** — das wird als
  `BrainAmbiguousError` durchgereicht statt automatisch wiederholt.

429 ist die eine Ausnahme von der Idempotenz-Regel: Das Rate-Limit-Budget
ist die erste Dependency in der Server-Kette (vor Scope- und
Rollenprüfung, vor jeder Schreiblogik) — eine 429-Antwort bedeutet, der
Request wurde nie verarbeitet, unabhängig von der Methode. Das SDK wartet
dabei die vom Server genannte `Retry-After`-Zeit, nicht die eigene
Backoff-Stufe — sonst unterbietet der Client absichtlich das Limit, das
der Server gerade gesetzt hat.

**409 mit `Retry-After` ist die zweite Ausnahme (#206)**, aus demselben
Grund: Der Server antwortet so, wenn die Anfrage an einer Zeilensperre
abgebrochen wurde (ein laufender Manifest-Sync im selben Projekt, ein
Deadlock) — nichts wurde geschrieben, unabhängig von der Methode. Das
`Retry-After` ist das Unterscheidungsmerkmal: Ein 409 **ohne** den Header
ist ein fachlicher Konflikt (`approve_review()` auf einen Eintrag, der
nicht zur Prüfung ansteht) und wird nie wiederholt.

## `upsert()`, `remove()` und `sync()` sind idempotent

Sie adressieren einen Zustand über einen Schlüssel, keine Aktion: Dieselbe
Anfrage zweimal ausgeführt ergibt denselben Bestand (eine unveränderte
Wiedereinreichung ist am Server ein No-Op, `merged`). Deshalb gilt für
sie dieselbe Retry-Regel wie für `search()` — auch ein `ReadTimeout` oder
5xx nach einer Antwort wird wiederholt. Preis: Nach einem Retry kann das
Urteil `merged` lauten, obwohl schon der erste Versuch geschrieben hat.
"""

from __future__ import annotations

import random
import time
import uuid
from collections.abc import Iterable
from types import TracebackType
from typing import Any, Self
from urllib.parse import quote

import httpx

from . import exceptions as exc
from .models import (
    FeedbackResult,
    Project,
    ReviewEntry,
    SearchResult,
    SubmissionResult,
    SyncEntry,
    SyncResult,
)

DEFAULT_TIMEOUT = 10.0
# Ein Manifest-Sync mit vielen Einträgen läuft am Server Sekunden bis
# Minuten (eine Transaktion, Sperren bis zum Commit) — der Vorgabe-Timeout
# eines Suchaufrufs wäre hier ein Fehler, wo keiner ist. Passt zum
# Proxy-Startwert der Server-Doku für den Sync-Pfad (300 s).
SYNC_TIMEOUT = 300.0
# Ein Upsert kann auf eine Zeile warten, die ein Sync oder der Embedding-
# Worker gerade hält — der Server wartet höchstens seine Sperrfrist (10 s) und
# antwortet dann mit 409 und `Retry-After`. Der Vorgabe-Timeout von 10 s würde
# genau dort abreißen; 30 s sind Sperrfrist plus Reserve für die Bearbeitung.
UPSERT_TIMEOUT = 30.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_BASE = 0.5
BACKOFF_FACTOR = 2.0
JITTER_FRACTION = 0.2


class BrainClient:
    """Ein Client je Token — `Authorization` steht fest bei der Erzeugung.

    Als Context-Manager verwendbar (`with BrainClient(...) as client:`),
    das schließt den zugrundeliegenden `httpx.Client` zuverlässig.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base: float = DEFAULT_BACKOFF_BASE,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    # -- öffentliche API ---------------------------------------------------

    def search(
        self,
        query: str,
        *,
        limit: int | None = None,
        category: str | None = None,
        tags: list[str] | None = None,
        min_confidence: float | None = None,
        include_content: bool | None = None,
        projects: list[str] | None = None,
        scope: str | None = None,
        context_project: str | None = None,
        max_cosine_distance: float | None = None,
    ) -> SearchResult:
        """`POST /v1/search`. Feldnamen und Ausschlussregeln (`projects`
        vs. `scope`) bildet dieser Aufruf 1:1 auf `SearchRequest`
        (`app/api/search.py` im Server-Repo) ab — die Validierung liegt
        dort, das SDK dupliziert sie nicht.

        `max_cosine_distance` (#206) verengt die Relevanz-Schwelle des
        Vektorzweigs für diese Anfrage — nur enger als die Grenze des
        Servers, nie weiter (sonst 422). Sie hat nur eine Bedeutung, wenn
        der Server mit einem echten Embedding-Modell läuft; gegen den
        Platzhalter-Anbieter bleibt sie wirkungslos. Ob der Vektorzweig
        überhaupt gelaufen ist, steht in `SearchResult.vector_branch`."""
        body: dict[str, Any] = {"query": query}
        for feld, wert in (
            ("limit", limit),
            ("category", category),
            ("tags", tags),
            ("min_confidence", min_confidence),
            ("include_content", include_content),
            ("projects", projects),
            ("scope", scope),
            ("context_project", context_project),
            ("max_cosine_distance", max_cosine_distance),
        ):
            if wert is not None:
                body[feld] = wert

        response = self._send("POST", "/v1/search", json=body, idempotent=True)
        if response.status_code >= 400:
            raise _fehler_aus_antwort(response)
        return SearchResult._from_json(response.json())

    def store(
        self,
        *,
        project: str,
        title: str,
        content: str,
        source: str,
        category: str | None = None,
        tags: list[str] | None = None,
        evidence: list[str] | None = None,
        confidence: float = 0.5,
    ) -> SubmissionResult:
        """`POST /v1/projects/{project_id}/entries`.

        `project` ist ein Slug **oder** eine UUID — ein Slug wird über
        `list_projects()` aufgelöst (derselbe `GET /v1/projects`, den
        auch `dbrain projects` nutzt), bevor der eigentliche Request
        rausgeht.

        Der `rejected`-Verdict ist **kein** Fehler: `stored`,
        `pending_review`, `merged` und `rejected` kommen alle als
        normales `SubmissionResult` zurück, unterscheidbar über
        `result.verdict` — ein Aufrufer, der nur `rejected` erfährt,
        könnte sonst denselben Text erneut versuchen, statt die
        `findings` zu lesen.
        """
        project_id = self._resolve_project_id(project)
        body: dict[str, Any] = {
            "title": title,
            "content": content,
            "source": source,
            "confidence": confidence,
        }
        if category is not None:
            body["category"] = category
        if tags is not None:
            body["tags"] = tags
        if evidence is not None:
            body["evidence"] = evidence

        response = self._send(
            "POST",
            f"/v1/projects/{project_id}/entries",
            json=body,
            idempotent=False,
        )

        return _einreichungsergebnis(response)

    def upsert(
        self,
        *,
        project: str,
        external_key: str,
        title: str,
        content: str,
        source: str,
        category: str | None = None,
        tags: list[str] | None = None,
        evidence: list[str] | None = None,
        confidence: float = 0.5,
    ) -> SubmissionResult:
        """`PUT /v1/projects/{project_id}/entries/by-key/{external_key}`
        (#206) — legt den Eintrag unter dem Schlüssel an oder **ersetzt** ihn.

        Für synchronisierte Quellen: Die Kennung des Eintrags bleibt über
        Änderungen stabil (Feedback, Nutzungszähler und Zitate hängen daran),
        Inhalt und Vektor sind neu. Eine unveränderte Wiedereinreichung ist
        ein No-Op (`verdict == "merged"`). `result.replaced` sagt, ob ein
        bestehender Eintrag ersetzt wurde. Nötig ist mindestens die Rolle
        `maintainer`; in einem synchronisierten Projekt ist der Schlüssel
        Pflicht für jeden Schreibzugriff.

        Wie `store()`: Eine Ablehnung der Prüfstrecke (`rejected`) kommt als
        normales Ergebnis zurück, nicht als Exception. Idempotent, siehe
        Modul-Docstring — ein Retry kann `merged` melden, obwohl der erste
        Versuch geschrieben hat.
        """
        project_id = self._resolve_project_id(project)
        body: dict[str, Any] = {
            "title": title,
            "content": content,
            "source": source,
            "confidence": confidence,
        }
        if category is not None:
            body["category"] = category
        if tags is not None:
            body["tags"] = tags
        if evidence is not None:
            body["evidence"] = evidence

        response = self._send(
            "PUT",
            f"/v1/projects/{project_id}/entries/by-key/{_pfadsegment(external_key)}",
            json=body,
            idempotent=True,
            timeout=UPSERT_TIMEOUT,
        )
        return _einreichungsergebnis(response)

    def remove(self, project: str, external_key: str) -> None:
        """`DELETE /v1/projects/{project_id}/entries/by-key/{external_key}`
        (#206) — archiviert den Eintrag: Er verlässt die Suche, bleibt aber
        erhalten (kein Hard-Delete). Idempotent: Ein bereits archivierter
        Eintrag ist ebenfalls `204`. `BrainNotFoundError` nur für einen
        unbekannten Schlüssel — am Server ununterscheidbar von einem Projekt
        außerhalb der effektiven Projektmenge (Invariante 1)."""
        project_id = self._resolve_project_id(project)
        response = self._send(
            "DELETE",
            f"/v1/projects/{project_id}/entries/by-key/{_pfadsegment(external_key)}",
            idempotent=True,
        )
        if response.status_code >= 400:
            raise _fehler_aus_antwort(response)

    def sync(
        self,
        project: str,
        *,
        source_revision: str,
        entries: Iterable[SyncEntry],
        timeout: float = SYNC_TIMEOUT,
    ) -> SyncResult:
        """`PUT /v1/projects/{project_id}/sync` (#206) — bringt den Bestand
        eines synchronisierten Projekts auf den Stand des Manifests.

        **Eine Transaktion, alles oder nichts:** Jeder Eintrag des Manifests
        wird angelegt, ersetzt oder als unverändert erkannt; jeder Schlüssel
        des Projekts, der **nicht** im Manifest steht, wird archiviert. Ein
        leeres Manifest archiviert den ganzen Bestand. Wird ein Eintrag von
        der Prüfstrecke abgelehnt (Geheimnis, Sperrliste, Länge), passiert
        nichts, und die Antwort nennt **alle** Urteile auf einmal
        (`result.verdict == "rejected"`) — ein normales Ergebnis, keine
        Exception, wie bei `store()`.

        `source_revision` (etwa ein Commit-Hash) landet im Audit-Log des
        Servers, mit den Zählern — keine Inhalte. Höchstens 1000 Einträge und
        8 MiB je Aufruf; mehr sind ein 422/413 vom Server. Nur für Projekte im
        Modus `synced` (sonst 422), nötig ist mindestens `maintainer`.

        Idempotent (Modul-Docstring): Ein Retry nach einem abgelaufenen Timeout
        läuft am Server gegen einen bereits geschriebenen Stand und meldet
        `merged`. `timeout` ersetzt für diesen Aufruf den Client-Vorgabewert
        (Vorgabe `SYNC_TIMEOUT`, 300 s): Ein Sync mit vielen Einträgen läuft am
        Server länger als eine Suche.
        """
        project_id = self._resolve_project_id(project)
        body: dict[str, Any] = {
            "source_revision": source_revision,
            "entries": [eintrag._to_json() for eintrag in entries],
        }
        response = self._send(
            "PUT",
            f"/v1/projects/{project_id}/sync",
            json=body,
            idempotent=True,
            timeout=timeout,
        )
        if response.status_code == 200:
            return SyncResult._from_json(response.json())
        if response.status_code == 422:
            detail = response.json().get("detail")
            if isinstance(detail, dict) and detail.get("verdict") == "rejected":
                return SyncResult._from_rejection(detail)
        raise _fehler_aus_antwort(response)

    def feedback(
        self, entry_id: uuid.UUID | str, *, helpful: bool, comment: str | None = None
    ) -> FeedbackResult:
        """`POST /v1/entries/{entry_id}/feedback`."""
        body: dict[str, Any] = {"helpful": helpful}
        if comment is not None:
            body["comment"] = comment

        response = self._send(
            "POST", f"/v1/entries/{entry_id}/feedback", json=body, idempotent=False
        )
        if response.status_code >= 400:
            raise _fehler_aus_antwort(response)
        return FeedbackResult._from_json(response.json())

    def list_projects(self) -> list[Project]:
        """`GET /v1/projects` — die effektive Projektmenge mit lesbaren
        Namen. Ohne Aufruf rätst du Slugs, und ein geratener Slug ist
        kein leeres Ergebnis, sondern ein Fehler (`store()`, `search()`
        mit `projects=`)."""
        response = self._send("GET", "/v1/projects", idempotent=True)
        if response.status_code >= 400:
            raise _fehler_aus_antwort(response)
        return [Project._from_json(p) for p in response.json()["projects"]]

    # -- Kuration (#93) — `maintainer`-Rolle im Zielprojekt vorausgesetzt --

    def list_review_queue(
        self, project: str, *, limit: int | None = None, offset: int | None = None
    ) -> list[ReviewEntry]:
        """`GET /v1/projects/{project_id}/review-queue` — wartende
        Einträge (`pending_review`), älteste zuletzt. `project` wie bei
        `store()`: Slug oder UUID."""
        project_id = self._resolve_project_id(project)
        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset

        response = self._send(
            "GET",
            f"/v1/projects/{project_id}/review-queue",
            params=params or None,
            idempotent=True,
        )
        if response.status_code >= 400:
            raise _fehler_aus_antwort(response)
        return [ReviewEntry._from_json(eintrag) for eintrag in response.json()]

    def approve_review(self, project: str, entry_id: uuid.UUID | str) -> ReviewEntry:
        """`POST .../review-queue/{entry_id}/approve` —
        `pending_review` → `active`. Wirkt nur aus `pending_review`
        heraus; alles andere ist eine `BrainHTTPError` (409 am Server —
        der Eintrag steht nicht zur Prüfung)."""
        project_id = self._resolve_project_id(project)
        response = self._send(
            "POST",
            f"/v1/projects/{project_id}/review-queue/{entry_id}/approve",
            idempotent=False,
        )
        if response.status_code >= 400:
            raise _fehler_aus_antwort(response)
        return ReviewEntry._from_json(response.json())

    def reject_review(self, project: str, entry_id: uuid.UUID | str) -> ReviewEntry:
        """`POST .../review-queue/{entry_id}/reject` —
        `pending_review` → `archived`. Anders als ein abgelehntes
        `store()` ist das hier eine **erfolgreiche** Kuratierungsentscheidung,
        kein gescheiterter Versuch — siehe `dbrain review reject`s
        Exit-Code."""
        project_id = self._resolve_project_id(project)
        response = self._send(
            "POST",
            f"/v1/projects/{project_id}/review-queue/{entry_id}/reject",
            idempotent=False,
        )
        if response.status_code >= 400:
            raise _fehler_aus_antwort(response)
        return ReviewEntry._from_json(response.json())

    def edit_review(
        self,
        project: str,
        entry_id: uuid.UUID | str,
        *,
        title: str | None = None,
        content: str | None = None,
        category: str | None = None,
        tags: list[str] | None = None,
        confidence: float | None = None,
        superseded_by: uuid.UUID | str | None = None,
    ) -> ReviewEntry:
        """`PATCH .../review-queue/{entry_id}` — ändert den Status nicht,
        auch nicht für einen längst `active` Eintrag. Nur gesetzte
        Argumente ändern sich; `None` heißt „unverändert lassen" — es gibt
        keinen Weg, `superseded_by` über diesen Aufruf explizit wieder zu
        löschen, dieselbe Grenze wie am Server (`app/api/review.py`,
        `ReviewEdit`)."""
        project_id = self._resolve_project_id(project)
        body: dict[str, Any] = {}
        for feld, wert in (
            ("title", title),
            ("content", content),
            ("category", category),
            ("tags", tags),
            ("confidence", confidence),
            (
                "superseded_by",
                str(superseded_by) if superseded_by is not None else None,
            ),
        ):
            if wert is not None:
                body[feld] = wert

        response = self._send(
            "PATCH",
            f"/v1/projects/{project_id}/review-queue/{entry_id}",
            json=body,
            idempotent=False,
        )
        if response.status_code >= 400:
            raise _fehler_aus_antwort(response)
        return ReviewEntry._from_json(response.json())

    # -- intern --------------------------------------------------------

    def _resolve_project_id(self, project: str) -> str:
        try:
            return str(uuid.UUID(project))
        except ValueError:
            pass
        for eintrag in self.list_projects():
            if eintrag.slug == project:
                return str(eintrag.project_id)
        raise exc.BrainNotFoundError(
            404, f"Projekt-Slug {project!r} nicht gefunden oder kein Zugriff"
        )

    def _send(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        idempotent: bool,
        timeout: float | None = None,
    ) -> httpx.Response:
        """Transport-Retry — 429 und Verbindungsfehler vor jeder Antwort
        immer, 5xx nur wenn `idempotent`. Gibt jede andere Antwort
        unverändert zurück; die Statuscode-Interpretation (401/403/404/422)
        bleibt bei den aufrufenden Methoden, weil `store()` einen 422
        anders behandelt als alle anderen.

        **`ConnectTimeout`/`PoolTimeout` vs. `ReadTimeout`/`WriteTimeout` —
        nicht dieselbe Garantie.** Nur die ersten beiden beweisen, dass der
        Request den Server nie erreicht hat (die Verbindung kam gar nicht
        zustande). Ein `ReadTimeout` heißt: der Request wurde vollständig
        gesendet, der Server hat ihn möglicherweise verarbeitet — dieselbe
        Unsicherheit wie ein 5xx *nach* einer Antwort. Für `idempotent=False`
        gilt deshalb dieselbe Grenze wie bei einem 5xx: sicher retryable ist
        nur, was beweisbar vor jeder Verarbeitung scheiterte.
        """
        # Nur setzen, wenn der Aufruf es verlangt — sonst gilt der Vorgabewert
        # des Clients (`httpx.USE_CLIENT_DEFAULT`, nicht `None`, wäre „kein
        # Timeout").
        optionen: dict[str, Any] = {} if timeout is None else {"timeout": timeout}
        versuch = 0
        while True:
            versuch += 1
            try:
                response = self._client.request(
                    method, path, json=json, params=params, **optionen
                )
            except (
                httpx.ConnectError,
                httpx.ConnectTimeout,
                httpx.PoolTimeout,
            ) as fehler:
                # Verbindung kam nie zustande — für jede Methode sicher
                # retryable, der Server hat nichts davon gesehen.
                if versuch > self._max_retries:
                    raise exc.BrainConnectionError(str(fehler)) from fehler
                self._warten(versuch)
                continue
            except (httpx.TimeoutException, httpx.TransportError) as fehler:
                # ReadTimeout/WriteTimeout/ReadError/… — der Request könnte
                # den Server bereits erreicht (und bei store()/feedback()
                # bereits etwas ausgelöst) haben. Für idempotente Aufrufe
                # so unbedenklich wie oben; für nicht-idempotente dieselbe
                # Ambiguität wie ein 5xx nach Antwort.
                if idempotent:
                    if versuch > self._max_retries:
                        raise exc.BrainConnectionError(str(fehler)) from fehler
                    self._warten(versuch)
                    continue
                raise exc.BrainAmbiguousError(
                    0,
                    "Status unklar — Verbindung brach ab, nachdem der "
                    "Request möglicherweise bereits gesendet wurde; Server "
                    f"hat ihn eventuell schon verarbeitet ({fehler})",
                ) from fehler

            if response.status_code == 429:
                if versuch > self._max_retries:
                    raise exc.BrainRateLimitError(429, _detail(response))
                self._warten_auf_retry_after(response)
                continue

            if response.status_code == 409 and "retry-after" in response.headers:
                # Zeilensperre am Server (#206): nichts geschrieben, für jede
                # Methode sicher retryable — das `Retry-After` unterscheidet
                # es von einem fachlichen 409 (Modul-Docstring).
                if versuch > self._max_retries:
                    raise exc.BrainLockConflictError(409, _detail(response))
                self._warten_auf_retry_after(response)
                continue

            ist_5xx = response.status_code >= 500
            if ist_5xx and idempotent and versuch <= self._max_retries:
                self._warten(versuch)
                continue

            if ist_5xx and not idempotent:
                raise exc.BrainAmbiguousError(
                    response.status_code,
                    "Status unklar — Server hat möglicherweise committet, "
                    f"vor erneutem Versuch prüfen ({_detail(response)})",
                )

            return response

    def _warten(self, versuch: int) -> None:
        basis = self._backoff_base * (BACKOFF_FACTOR ** (versuch - 1))
        jitter = basis * JITTER_FRACTION * (2 * random.random() - 1)  # noqa: S311
        time.sleep(max(0.0, basis + jitter))

    def _warten_auf_retry_after(self, response: httpx.Response) -> None:
        try:
            sekunden = float(response.headers.get("Retry-After", "1"))
        except ValueError:
            sekunden = 1.0
        time.sleep(max(0.0, sekunden))


def _pfadsegment(external_key: str) -> str:
    """Der Schlüssel als Pfadsegment — **immer** prozent-kodiert, auch wenn
    er harmlos aussieht. Ein `#` im Schlüssel schnitte `httpx` sonst als
    URL-Fragment ab, und der Server sähe stillschweigend einen kürzeren
    Schlüssel (aus `core.haushalt#name` würde `core.haushalt`) — ein Upsert
    träfe den falschen Eintrag. Kodiert kommt der volle Schlüssel an und
    scheitert am Formatprüfer des Servers (422), wo der Fehler hingehört."""
    return quote(external_key, safe="")


def _einreichungsergebnis(response: httpx.Response) -> SubmissionResult:
    """`store()` und `upsert()` beantworten Erfolg und Ablehnung gleich."""
    if response.status_code in (200, 201):
        return SubmissionResult._from_json(response.json())

    if response.status_code == 422:
        daten = response.json()
        detail = daten.get("detail")
        if isinstance(detail, dict) and "verdict" in detail:
            # Die Ablehnung der Prüfstrecke — ein Ergebnis, kein
            # Fehler dieses Clients (siehe Docstring von `store()`).
            return SubmissionResult._from_json(detail)
        raise exc.BrainValidationError(422, str(detail if detail else daten))

    raise _fehler_aus_antwort(response)


def _detail(response: httpx.Response) -> str:
    try:
        daten = response.json()
    except ValueError:
        return response.text
    if isinstance(daten, dict) and "detail" in daten:
        detail = daten["detail"]
        return detail if isinstance(detail, str) else str(detail)
    return str(daten)


def _fehler_aus_antwort(response: httpx.Response) -> exc.BrainError:
    status = response.status_code
    detail = _detail(response)
    if status in (401, 403):
        return exc.BrainAuthError(status, detail)
    if status == 404:
        return exc.BrainNotFoundError(status, detail)
    if status == 422:
        return exc.BrainValidationError(status, detail)
    return exc.BrainHTTPError(status, detail)
