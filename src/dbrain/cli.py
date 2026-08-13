"""`dbrain` — Kommandozeilenwerkzeug über `BrainClient`.

`argparse`, keine neue Laufzeit-Abhängigkeit: Das Projekt hat drei
Unterbefehle, und `argparse`-Subparser sind dafür ohne Mehraufwand
ausreichend — kein Grund, für „dünn" Typer/Click als zwei zusätzliche
Abhängigkeiten hereinzuholen.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import json
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import __version__
from .client import BrainClient
from .config import DEFAULT_CONFIG_PATH, ConfigError, resolve
from .exceptions import BrainError
from .models import FeedbackResult, Project, SearchResult, SubmissionResult


def _jsonable(wert: Any) -> Any:
    if dataclasses.is_dataclass(wert) and not isinstance(wert, type):
        wert = dataclasses.asdict(wert)
    if isinstance(wert, dict):
        return {k: _jsonable(v) for k, v in wert.items()}
    if isinstance(wert, list | tuple):
        return [_jsonable(v) for v in wert]
    if isinstance(wert, uuid.UUID):
        return str(wert)
    if isinstance(wert, datetime.datetime):
        return wert.isoformat()
    return wert


def _print_search(ergebnis: SearchResult) -> None:
    if not ergebnis.hits:
        print("Keine Treffer.")
        return
    for treffer in ergebnis.hits:
        print(f"{treffer.score:.3f}  {treffer.project_slug}/{treffer.title}"
              f"  ({treffer.entry_id})")
        print(f"    {treffer.snippet}")
    print(f"\n{len(ergebnis.hits)} Treffer, Begriffe: {', '.join(ergebnis.terms)}")


def _print_store(ergebnis: SubmissionResult) -> None:
    print(f"verdict={ergebnis.verdict}")
    if ergebnis.entry_id is not None:
        print(f"entry_id={ergebnis.entry_id} status={ergebnis.status}")
    if ergebnis.duplicate_of is not None:
        print(f"duplicate_of={ergebnis.duplicate_of}")
    for befund in ergebnis.findings:
        print(f"  [{befund.severity}] {befund.gate}/{befund.code}: {befund.hint}")


def _print_feedback(ergebnis: FeedbackResult) -> None:
    print(
        f"entry_id={ergebnis.entry_id} confidence={ergebnis.confidence} "
        f"status={ergebnis.status} confidence_adjusted={ergebnis.confidence_adjusted}"
    )


def _print_projects(projekte: list[Project]) -> None:
    if not projekte:
        print("Keine Projekte sichtbar.")
        return
    for projekt in projekte:
        archiv = " (archiviert)" if projekt.archived else ""
        print(f"{projekt.slug}\t{projekt.name}\t{projekt.role}{archiv}")


def _output(wert: Any, *, als_json: bool, menschlich: Callable[[Any], None]) -> None:
    if als_json:
        print(json.dumps(_jsonable(wert), indent=2, ensure_ascii=False))
    else:
        menschlich(wert)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dbrain", description="Client für developers-brain"
    )
    parser.add_argument(
        "--url", help="Server-URL (sonst DBRAIN_URL oder Config-Datei)"
    )
    parser.add_argument(
        "--token-stdin",
        action="store_true",
        help="Token von stdin lesen statt DBRAIN_TOKEN/Config-Datei — "
        "niemals --token als Argument (Shell-History, Prozessliste)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help=f"Pfad zur Config-Datei (Vorgabe: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument("--profile", help="Benanntes Profil aus der Config-Datei")
    parser.add_argument(
        "--json", action="store_true", help="Ausgabe als JSON statt menschenlesbar"
    )
    parser.add_argument(
        "--version", action="version", version=f"dbrain {__version__}"
    )

    sub = parser.add_subparsers(dest="command", required=True)

    suche = sub.add_parser("search", help="Wissen suchen")
    suche.add_argument("query")
    suche.add_argument("--limit", type=int)
    suche.add_argument("--category")
    suche.add_argument("--tag", action="append", dest="tags")
    suche.add_argument("--min-confidence", type=float)
    suche.add_argument("--include-content", action="store_true", default=None)
    suche.add_argument(
        "--project",
        action="append",
        dest="projects",
        help="Auf dieses Projekt einschränken (Slug, wiederholbar) — "
        "schließt sich mit --scope aus",
    )
    suche.add_argument("--scope", choices=["all"])
    suche.add_argument("--context-project")

    einreichen = sub.add_parser("store", help="Wissen einreichen")
    einreichen.add_argument(
        "--project", required=True, help="Ziel-Projekt (Slug oder UUID)"
    )
    einreichen.add_argument("--title", required=True)
    einreichen.add_argument(
        "--content", help="Inhalt (Markdown) — ohne diese Option wird stdin gelesen"
    )
    einreichen.add_argument("--source", required=True)
    einreichen.add_argument("--category")
    einreichen.add_argument("--tag", action="append", dest="tags", default=[])
    einreichen.add_argument(
        "--evidence", action="append", dest="evidence", default=[]
    )
    einreichen.add_argument("--confidence", type=float, default=0.5)

    feedback = sub.add_parser("feedback", help="Feedback zu einem Eintrag abgeben")
    feedback.add_argument("entry_id")
    helpful = feedback.add_mutually_exclusive_group(required=True)
    helpful.add_argument("--helpful", action="store_true", dest="helpful")
    helpful.add_argument("--not-helpful", action="store_false", dest="helpful")
    feedback.add_argument("--comment")

    sub.add_parser("projects", help="Effektive Projektmenge anzeigen")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        konfiguration = resolve(
            url=args.url,
            token_stdin=args.token_stdin,
            config_path=args.config,
            profile=args.profile,
        )
    except ConfigError as fehler:
        print(f"Fehler: {fehler}", file=sys.stderr)
        return 1

    with BrainClient(konfiguration.base_url, konfiguration.token) as client:
        try:
            if args.command == "search":
                suchergebnis = client.search(
                    args.query,
                    limit=args.limit,
                    category=args.category,
                    tags=args.tags,
                    min_confidence=args.min_confidence,
                    include_content=args.include_content,
                    projects=args.projects,
                    scope=args.scope,
                    context_project=args.context_project,
                )
                _output(suchergebnis, als_json=args.json, menschlich=_print_search)
                return 0

            if args.command == "store":
                inhalt = args.content if args.content is not None else sys.stdin.read()
                einreichungsergebnis = client.store(
                    project=args.project,
                    title=args.title,
                    content=inhalt,
                    source=args.source,
                    category=args.category,
                    tags=args.tags,
                    evidence=args.evidence,
                    confidence=args.confidence,
                )
                _output(
                    einreichungsergebnis, als_json=args.json, menschlich=_print_store
                )
                return 1 if einreichungsergebnis.verdict == "rejected" else 0

            if args.command == "feedback":
                feedbackergebnis = client.feedback(
                    args.entry_id, helpful=args.helpful, comment=args.comment
                )
                _output(
                    feedbackergebnis, als_json=args.json, menschlich=_print_feedback
                )
                return 0

            if args.command == "projects":
                projekte = client.list_projects()
                _output(projekte, als_json=args.json, menschlich=_print_projects)
                return 0
        except BrainError as fehler:
            print(f"Fehler: {fehler}", file=sys.stderr)
            return 1

    return 1  # unerreichbar bei bekanntem Subcommand — argparse erzwingt einen der vier


if __name__ == "__main__":
    sys.exit(main())
