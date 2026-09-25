"""Gemeinsame Test-Bausteine.

Kein echter Server: `httpx.MockTransport` simuliert die API, dadurch
prüfen diese Tests ausschließlich die Client-Logik (Retry, Fehler-Mapping,
Slug-Auflösung) — kein Ersatz für einen Contract-Test gegen die echte
`/v1`-Fassade, aber unabhängig von einer laufenden Datenbank.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

import httpx

from dbrain.client import BrainClient

TOKEN = "dbrain_test12345_geheimnis"


def make_client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    max_retries: int = 3,
    backoff_base: float = 0.001,
) -> BrainClient:
    """Ein `BrainClient` gegen `handler` statt gegen echtes HTTP.

    `backoff_base` winzig, sonst dauert jeder Retry-Test spürbar lange —
    die tatsächliche Backoff-*Formel* wird hier nicht geprüft, nur, dass
    überhaupt (und wie oft) wiederholt wird.
    """
    return BrainClient(
        "http://test",
        TOKEN,
        max_retries=max_retries,
        backoff_base=backoff_base,
        transport=httpx.MockTransport(handler),
    )


def json_response(status_code: int, payload: object) -> httpx.Response:
    return httpx.Response(
        status_code,
        content=json.dumps(payload),
        headers={"content-type": "application/json"},
    )


def make_search_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "hits": [],
        "terms": ["migration"],
        "fusion": "rrf",
        "vector_candidates": 0,
        "fulltext_candidates": 0,
    }
    payload.update(overrides)
    return payload


def make_hit(**overrides: object) -> dict[str, object]:
    hit: dict[str, object] = {
        "entry_id": str(uuid.uuid4()),
        "project_id": str(uuid.uuid4()),
        "project_slug": "a",
        "title": "Titel",
        "snippet": "…Ausschnitt…",
        "content": None,
        "category": None,
        "tags": [],
        "source": "test",
        "confidence": 0.5,
        "verified": False,
        "created_at": datetime.now(UTC).isoformat(),
        "score": 0.9,
        "cosine_distance": None,
        "fulltext_score": 1.2,
    }
    hit.update(overrides)
    return hit


def make_submission_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "verdict": "stored",
        "entry_id": str(uuid.uuid4()),
        "status": "active",
        "duplicate_of": None,
        "confidence": 0.9,
        "findings": [],
    }
    payload.update(overrides)
    return payload


def make_project_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "project_id": str(uuid.uuid4()),
        "slug": "a",
        "name": "A",
        "role": "reader",
        "archived": False,
    }
    payload.update(overrides)
    return payload


def make_review_entry_payload(**overrides: object) -> dict[str, object]:
    """Die Form von `ReviewQueueEntryOut` (`app/api/review.py` im
    Server-Repo, #93) — identisch für einen Eintrag aus `GET
    .../review-queue`, `POST .../approve`, `POST .../reject` und `PATCH
    .../review-queue/{id}`."""
    payload: dict[str, object] = {
        "entry_id": str(uuid.uuid4()),
        "project_id": str(uuid.uuid4()),
        "title": "Titel",
        "content": "Inhalt",
        "category": None,
        "tags": [],
        "evidence": [],
        "source": "test",
        "confidence": 0.5,
        "status": "pending_review",
        "superseded_by": None,
        "created_by": str(uuid.uuid4()),
        "created_at": datetime.now(UTC).isoformat(),
        "updated_at": datetime.now(UTC).isoformat(),
    }
    payload.update(overrides)
    return payload


def make_sync_entry_result(**overrides: object) -> dict[str, object]:
    """Ein Urteil je Schlüssel, wie `SyncEntryResult` (`app/api/synced_entries.py`
    im Server-Repo, #206) es liefert."""
    urteil: dict[str, object] = {
        "external_key": "core.a",
        "verdict": "stored",
        "entry_id": str(uuid.uuid4()),
        "status": "active",
        "replaced": False,
        "duplicate_of": None,
        "findings": [],
    }
    urteil.update(overrides)
    return urteil


def make_sync_payload(**overrides: object) -> dict[str, object]:
    """Die Antwort auf einen erfolgreichen `PUT .../sync`."""
    payload: dict[str, object] = {
        "source_revision": "abc123",
        "results": [make_sync_entry_result()],
        "archived": [],
        "counts": {"stored": 1, "replaced": 0, "merged": 0, "archived": 0},
    }
    payload.update(overrides)
    return payload


def make_sync_rejection(**overrides: object) -> dict[str, object]:
    """Der 422-Körper (`detail`), wenn mindestens ein Eintrag abgelehnt wurde —
    alle Urteile, nichts geschrieben."""
    befund = {
        "gate": "secret-scan",
        "code": "aws-access-token",
        "severity": "reject",
        "field": "content",
        "hint": "sieht aus wie ein Geheimnis",
        "reference": None,
    }
    detail: dict[str, object] = {
        "verdict": "rejected",
        "source_revision": "abc123",
        "results": [
            make_sync_entry_result(external_key="core.gut"),
            make_sync_entry_result(
                external_key="core.geheim",
                verdict="rejected",
                entry_id=None,
                status=None,
                findings=[befund],
            ),
        ],
    }
    detail.update(overrides)
    return detail
