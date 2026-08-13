"""Fehlerhierarchie des Clients.

Eine Klasse je Statuscode-Klasse, keine einzelne `BrainHTTPError` für
alles — ein Aufrufer, der zwischen „nicht gefunden" (404, kann vorkommen)
und „Scope reicht nicht" (401/403, Konfigurationsfehler) unterscheiden
will, soll das über den Exception-Typ tun können, nicht über
`err.status_code == 404`.
"""

from __future__ import annotations


class BrainError(Exception):
    """Basisklasse für alle Fehler dieses Clients."""


class BrainConnectionError(BrainError):
    """Verbindung fehlgeschlagen, bevor eine Antwort ankam — nach
    Ausschöpfen der Retries."""


class BrainHTTPError(BrainError):
    """Der Server hat geantwortet, aber mit einem Fehlerstatus."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"HTTP {status_code}: {detail}")


class BrainAuthError(BrainHTTPError):
    """401 oder 403 — kein oder ein zu schwaches Token."""


class BrainNotFoundError(BrainHTTPError):
    """404 — Ressource liegt außerhalb der effektiven Projektmenge oder
    existiert nicht. Beide Fälle sind serverseitig ununterscheidbar
    (Invariante 1), das gilt hier genauso."""


class BrainValidationError(BrainHTTPError):
    """422 — Schema-Fehler oder eine echte Ablehnung (z. B. Secret-Scan
    bei `feedback()`), nicht der `rejected`-Verdict von `store()`: Der
    ist ein normales `SubmissionResult`, keine Exception (siehe
    `BrainClient.store`)."""


class BrainAmbiguousError(BrainHTTPError):
    """Der Ausgang einer nicht-idempotenten Anfrage (`store()`/`feedback()`)
    ist unklar — der Server hat möglicherweise committet, bevor der Client
    das erfuhr. Der Client kann das ohne Idempotency-Key nicht
    unterscheiden und wiederholt deshalb nicht automatisch. Vor einem
    manuellen erneuten Versuch prüfen, ob der Eintrag/das Feedback schon
    angekommen ist.

    Zwei Ursachen, dieselbe Unsicherheit: ein 5xx **nach** einer Antwort
    (`status_code` trägt den echten HTTP-Status) oder ein Verbindungsabbruch
    (`ReadTimeout`/`WriteTimeout`/…), der den Request möglicherweise bereits
    beim Server ankommen ließ, bevor die Verbindung riss — dafür ist
    `status_code` `0`, weil es keine echte HTTP-Antwort gab.
    """


class BrainRateLimitError(BrainHTTPError):
    """429, das auch nach den konfigurierten Retries nicht durchkam."""
