"""`BrainClient` — dünner HTTP-Client gegen die developers-brain-API
(`/v1/*`), keine Server-Abhängigkeit außer `httpx`.

## Retry+Backoff: transient UND idempotent, nicht nur transient

`search()` und `list_projects()` sind ohne Nebenwirkung — jeder
Verbindungsfehler, Timeout oder 5xx wird bedenkenlos wiederholt.

`store()` und `feedback()` schreiben, und die Unterscheidung, die zählt,
ist nicht *welcher* Fehler auftrat, sondern *wann*:

- Ein Verbindungsfehler oder Timeout, **bevor** irgendeine Antwort beim
  Client ankam, heißt: der Request hat den Server nachweislich nie
  verarbeitet. Retryable, für alle vier Methoden.
- Ein 5xx-Statuscode, also eine Antwort kam an, nur eine schlechte, heißt
  für `store()`/`feedback()`: unklar, ob committet wurde, bevor der
  Fehler zurückkam. **Nicht automatisch retryable** — das wird als
  `BrainAmbiguousError` durchgereicht statt automatisch wiederholt.

429 ist die eine Ausnahme von der Idempotenz-Regel: Das Rate-Limit-Budget
ist die erste Dependency in der Server-Kette (vor Scope- und
Rollenprüfung, vor jeder Schreiblogik) — eine 429-Antwort bedeutet, der
Request wurde nie verarbeitet, unabhängig von der Methode. Das SDK wartet
dabei die vom Server genannte `Retry-After`-Zeit, nicht die eigene
Backoff-Stufe — sonst unterbietet der Client absichtlich das Limit, das
der Server gerade gesetzt hat.
"""

from __future__ import annotations

import random
import time
import uuid
from types import TracebackType
from typing import Any, Self

import httpx

from . import exceptions as exc
from .models import FeedbackResult, Project, SearchResult, SubmissionResult

DEFAULT_TIMEOUT = 10.0
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
    ) -> SearchResult:
        """`POST /v1/search`. Feldnamen und Ausschlussregeln (`projects`
        vs. `scope`) bildet dieser Aufruf 1:1 auf `SearchRequest`
        (`app/api/search.py` im Server-Repo) ab — die Validierung liegt
        dort, das SDK dupliziert sie nicht."""
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

        if response.status_code in (200, 201):
            return SubmissionResult._from_json(response.json())

        if response.status_code == 422:
            daten = response.json()
            detail = daten.get("detail")
            if isinstance(detail, dict) and "verdict" in detail:
                # Die Ablehnung der Prüfstrecke — ein Ergebnis, kein
                # Fehler dieses Clients (siehe Docstring oben).
                return SubmissionResult._from_json(detail)
            raise exc.BrainValidationError(422, str(detail if detail else daten))

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
        self, method: str, path: str, *, json: dict[str, Any] | None = None,
        idempotent: bool,
    ) -> httpx.Response:
        """Transport-Retry — 429 und Verbindungsfehler vor jeder Antwort
        immer, 5xx nur wenn `idempotent`. Gibt jede andere Antwort
        unverändert zurück; die Statuscode-Interpretation (401/403/404/422)
        bleibt bei den aufrufenden Methoden, weil `store()` einen 422
        anders behandelt als alle anderen."""
        versuch = 0
        while True:
            versuch += 1
            try:
                response = self._client.request(method, path, json=json)
            except (httpx.TimeoutException, httpx.TransportError) as fehler:
                if versuch > self._max_retries:
                    raise exc.BrainConnectionError(str(fehler)) from fehler
                self._warten(versuch)
                continue

            if response.status_code == 429:
                if versuch > self._max_retries:
                    raise exc.BrainRateLimitError(429, _detail(response))
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
