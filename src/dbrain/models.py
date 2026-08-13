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
        )


@dataclass(frozen=True, slots=True)
class SearchResult:
    """Die Antwort von `POST /v1/search`."""

    hits: tuple[SearchHit, ...]
    terms: tuple[str, ...]
    fusion: str
    vector_candidates: int
    fulltext_candidates: int

    @classmethod
    def _from_json(cls, data: dict[str, Any]) -> SearchResult:
        return cls(
            hits=tuple(SearchHit._from_json(hit) for hit in data["hits"]),
            terms=tuple(data.get("terms", ())),
            fusion=data["fusion"],
            vector_candidates=data["vector_candidates"],
            fulltext_candidates=data["fulltext_candidates"],
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
