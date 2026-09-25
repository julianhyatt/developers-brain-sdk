"""Antwortmodelle — Klassen-Attribute lesen nur bekannte Felder aus dem
JSON aus, unbekannte Zusatzfelder werden stillschweigend ignoriert statt
einen Fehler zu werfen.

Das ist die Client-seitige Hälfte der Additivstabilität von `/v1`
(ADR-004 Amendment 001): Ein per Tag gepinntes SDK darf nicht brechen,
wenn der Server ein neues optionales Feld ergänzt. Deshalb `dict.get(...)`
statt `**data` und keine Validierungsbibliothek, die unbekannte Felder
standardmäßig ablehnt.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class SearchHit:
    """Ein Treffer, wie ihn `POST /v1/search` liefert."""

    entry_id: uuid.UUID
    project_id: uuid.UUID
    project_slug: str
    title: str
    snippet: str
    content: str | None
    category: str | None
    tags: tuple[str, ...]
    source: str
    confidence: float
    verified: bool
    created_at: datetime
    score: float
    cosine_distance: float | None
    fulltext_score: float | None
    external_key: str | None = None
    """Der Schlüssel, unter dem die Quelle den Eintrag führt — nur bei
    Einträgen aus einem synchronisierten Projekt, sonst `None` (#206). Damit
    ordnet ein Leser einen Treffer dem Abschnitt der Quelle zu."""

    @classmethod
    def _from_json(cls, data: dict[str, Any]) -> SearchHit:
        return cls(
            entry_id=uuid.UUID(data["entry_id"]),
            project_id=uuid.UUID(data["project_id"]),
            project_slug=data["project_slug"],
            title=data["title"],
            snippet=data["snippet"],
            content=data.get("content"),
            category=data.get("category"),
            tags=tuple(data.get("tags", ())),
            source=data["source"],
            confidence=data["confidence"],
            verified=data["verified"],
            created_at=datetime.fromisoformat(data["created_at"]),
            score=data["score"],
            cosine_distance=data.get("cosine_distance"),
            fulltext_score=data.get("fulltext_score"),
            external_key=data.get("external_key"),
        )


@dataclass(frozen=True, slots=True)
class SearchResult:
    """Die Antwort von `POST /v1/search`."""

    hits: tuple[SearchHit, ...]
    terms: tuple[str, ...]
    fusion: str
    vector_candidates: int
    fulltext_candidates: int
    vector_branch: str = "ok"
    """`ok`, oder `unavailable`, wenn der Embedding-Anbieter nicht geantwortet
    hat (#206) — dann stammen die Treffer allein aus dem Volltext: vollständig
    für wörtliche Begriffe, lückenhaft für Umschreibungen. Eine leere Liste in
    diesem Zustand heißt nicht „gibt es nicht". `ambiguous`: Der Zweig lief,
    aber die Kandidatenlage lässt keine Aussage zu. Fehlt das Feld (älterer
    Server), gilt `ok`."""

    @property
    def degraded(self) -> bool:
        """Ob die Antwort ohne Vektorzweig zustande kam."""
        return self.vector_branch == "unavailable"

    @classmethod
    def _from_json(cls, data: dict[str, Any]) -> SearchResult:
        return cls(
            hits=tuple(SearchHit._from_json(hit) for hit in data["hits"]),
            terms=tuple(data.get("terms", ())),
            fusion=data["fusion"],
            vector_candidates=data["vector_candidates"],
            fulltext_candidates=data["fulltext_candidates"],
            vector_branch=data.get("vector_branch", "ok"),
        )


@dataclass(frozen=True, slots=True)
class Finding:
    """Ein Befund der Prüfstrecke, wie ihn `store()` mitliefert — auch bei
    Erfolg (ein Hinweis kann einen angelegten Eintrag begleiten)."""

    gate: str
    code: str
    severity: str
    field: str | None
    hint: str
    reference: uuid.UUID | None

    @classmethod
    def _from_json(cls, data: dict[str, Any]) -> Finding:
        reference = data.get("reference")
        return cls(
            gate=data["gate"],
            code=data["code"],
            severity=data["severity"],
            field=data.get("field"),
            hint=data["hint"],
            reference=uuid.UUID(reference) if reference else None,
        )


@dataclass(frozen=True, slots=True)
class SubmissionResult:
    """Das Urteil über eine Einreichung — `verdict` ist eines von
    `stored`, `pending_review`, `merged`, `rejected`. Ein `rejected`
    ist kein Fehler dieses Clients: `store()` gibt es als normales
    Ergebnis zurück, nicht als Exception (siehe `BrainClient.store`)."""

    verdict: str
    entry_id: uuid.UUID | None
    status: str | None
    duplicate_of: uuid.UUID | None
    confidence: float
    findings: tuple[Finding, ...]
    replaced: bool = False
    """`True`, wenn `upsert()` einen bestehenden Eintrag ersetzt hat — Kennung
    stabil, Inhalt neu, Vektor wird neu berechnet (#206). Bei `merged`
    (unveränderte Wiedereinreichung) `False`."""
    external_key: str | None = None

    @classmethod
    def _from_json(cls, data: dict[str, Any]) -> SubmissionResult:
        entry_id = data.get("entry_id")
        duplicate_of = data.get("duplicate_of")
        return cls(
            verdict=data["verdict"],
            entry_id=uuid.UUID(entry_id) if entry_id else None,
            status=data.get("status"),
            duplicate_of=uuid.UUID(duplicate_of) if duplicate_of else None,
            confidence=data["confidence"],
            findings=tuple(
                Finding._from_json(finding) for finding in data.get("findings", ())
            ),
            replaced=data.get("replaced", False),
            external_key=data.get("external_key"),
        )


@dataclass(frozen=True, slots=True)
class FeedbackResult:
    """Der Zustand des Eintrags nach `feedback()`."""

    entry_id: uuid.UUID
    confidence: float
    status: str
    confidence_adjusted: bool

    @classmethod
    def _from_json(cls, data: dict[str, Any]) -> FeedbackResult:
        return cls(
            entry_id=uuid.UUID(data["entry_id"]),
            confidence=data["confidence"],
            status=data["status"],
            confidence_adjusted=data["confidence_adjusted"],
        )


@dataclass(frozen=True, slots=True)
class ReviewEntry:
    """Ein Eintrag aus Kuratierungssicht — dieselbe Form für einen Eintrag
    aus `list_review_queue()` wie für das Ergebnis von `approve_review()`/
    `reject_review()`/`edit_review()`: Alle vier REST-Routen
    (`GET`/`POST .../approve`/`POST .../reject`/`PATCH`) antworten mit
    demselben `ReviewQueueEntryOut` (`app/api/review.py` im Server-Repo)."""

    entry_id: uuid.UUID
    project_id: uuid.UUID
    title: str
    content: str
    category: str | None
    tags: tuple[str, ...]
    evidence: tuple[str, ...]
    source: str
    confidence: float
    status: str
    superseded_by: uuid.UUID | None
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime
    external_key: str | None = None

    @classmethod
    def _from_json(cls, data: dict[str, Any]) -> ReviewEntry:
        superseded_by = data.get("superseded_by")
        return cls(
            entry_id=uuid.UUID(data["entry_id"]),
            project_id=uuid.UUID(data["project_id"]),
            title=data["title"],
            content=data["content"],
            category=data.get("category"),
            tags=tuple(data.get("tags", ())),
            evidence=tuple(data.get("evidence", ())),
            source=data["source"],
            confidence=data["confidence"],
            status=data["status"],
            superseded_by=uuid.UUID(superseded_by) if superseded_by else None,
            created_by=uuid.UUID(data["created_by"]),
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
            external_key=data.get("external_key"),
        )


@dataclass(frozen=True, slots=True)
class Project:
    """Ein Projekt, in dem dieses Token arbeiten darf (`GET /v1/projects`)."""

    project_id: uuid.UUID
    slug: str
    name: str
    role: str
    archived: bool

    @classmethod
    def _from_json(cls, data: dict[str, Any]) -> Project:
        return cls(
            project_id=uuid.UUID(data["project_id"]),
            slug=data["slug"],
            name=data["name"],
            role=data["role"],
            archived=data["archived"],
        )


@dataclass(frozen=True, slots=True)
class SyncEntry:
    """Ein Eintrag des Manifests für `BrainClient.sync()` — dieselben Felder
    wie `store()`, dazu der Schlüssel, unter dem die Quelle ihn führt.

    `external_key`: Kleinbuchstaben, Ziffern und die Trenner `.` `_` `~` `-`,
    nie am Rand, nie doppelt (Server: `^[a-z0-9]+([._~-][a-z0-9]+)*$`, höchstens
    200 Zeichen). **Kein `#`** — der Schlüssel steht im URL-Pfad, und `#` ist
    dort der Fragment-Trenner; wer eine Quelle mit `artikel#abschnitt`
    hat, bildet den Trenner auf `~` ab. Die Prüfung macht der Server, das SDK
    dupliziert sie nicht."""

    external_key: str
    title: str
    content: str
    source: str
    category: str | None = None
    tags: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    confidence: float = 0.5

    def _to_json(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "external_key": self.external_key,
            "title": self.title,
            "content": self.content,
            "source": self.source,
            "confidence": self.confidence,
        }
        if self.category is not None:
            body["category"] = self.category
        if self.tags:
            body["tags"] = list(self.tags)
        if self.evidence:
            body["evidence"] = list(self.evidence)
        return body


@dataclass(frozen=True, slots=True)
class SyncEntryResult:
    """Das Urteil über einen Schlüssel des Manifests — dieselbe Form wie
    `SubmissionResult`, um den Schlüssel ergänzt."""

    external_key: str
    verdict: str
    entry_id: uuid.UUID | None
    status: str | None
    replaced: bool
    duplicate_of: uuid.UUID | None
    findings: tuple[Finding, ...]

    @classmethod
    def _from_json(cls, data: dict[str, Any]) -> SyncEntryResult:
        entry_id = data.get("entry_id")
        duplicate_of = data.get("duplicate_of")
        return cls(
            external_key=data["external_key"],
            verdict=data["verdict"],
            entry_id=uuid.UUID(entry_id) if entry_id else None,
            status=data.get("status"),
            replaced=data.get("replaced", False),
            duplicate_of=uuid.UUID(duplicate_of) if duplicate_of else None,
            findings=tuple(
                Finding._from_json(finding) for finding in data.get("findings", ())
            ),
        )


@dataclass(frozen=True, slots=True)
class SyncCounts:
    stored: int
    """Neu angelegt."""
    replaced: int
    """Bestehend, Inhalt ersetzt."""
    merged: int
    """Bestehend, unverändert — kein Schreibzugriff."""
    archived: int
    """Im Projekt vorhanden, nicht im Manifest — archiviert."""

    @classmethod
    def _from_json(cls, data: dict[str, Any]) -> SyncCounts:
        return cls(
            stored=data["stored"],
            replaced=data["replaced"],
            merged=data["merged"],
            archived=data["archived"],
        )


@dataclass(frozen=True, slots=True)
class SyncResult:
    """Der Ausgang eines Manifest-Syncs — `verdict` ist `applied` oder
    `rejected`. Wie bei `store()` ist eine Ablehnung **kein** Fehler dieses
    Clients: `sync()` gibt sie als normales Ergebnis zurück, unterscheidbar
    über `verdict`/`ok`. Ein Aufrufer, der nur Exceptions fängt, würde die
    Begründung sonst nie sehen; eine CI prüft `ok`.

    `rejected`: Mindestens ein Eintrag wurde von der Prüfstrecke abgelehnt
    (Geheimnis, Sperrliste, Länge) — **nichts wurde geschrieben**, der
    Bestand steht auf der letzten konsistenten Revision. `results` nennt
    **alle** Urteile, nicht nur das erste; `archived` ist leer, `counts`
    `None`."""

    verdict: str
    source_revision: str
    results: tuple[SyncEntryResult, ...]
    archived: tuple[str, ...]
    counts: SyncCounts | None

    @property
    def ok(self) -> bool:
        return self.verdict == "applied"

    @classmethod
    def _from_json(cls, data: dict[str, Any]) -> SyncResult:
        return cls(
            verdict="applied",
            source_revision=data["source_revision"],
            results=tuple(SyncEntryResult._from_json(r) for r in data["results"]),
            archived=tuple(data.get("archived", ())),
            counts=SyncCounts._from_json(data["counts"]),
        )

    @classmethod
    def _from_rejection(cls, data: dict[str, Any]) -> SyncResult:
        return cls(
            verdict="rejected",
            source_revision=data["source_revision"],
            results=tuple(SyncEntryResult._from_json(r) for r in data["results"]),
            archived=(),
            counts=None,
        )
