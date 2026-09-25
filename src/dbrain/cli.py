"""`dbrain` — Kommandozeilenwerkzeug über `BrainClient`.

`argparse`, keine neue Laufzeit-Abhängigkeit: Das Projekt hat eine
Handvoll Unterbefehle (`review` mit eigenen Unter-Unterbefehlen seit
#93), und `argparse`-Subparser sind dafür ohne Mehraufwand ausreichend —
kein Grund, für „dünn" Typer/Click als zwei zusätzliche Abhängigkeiten
hereinzuholen.
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
from .models import (
    FeedbackResult,
    Project,
    ReviewEntry,
    SearchResult,
    SubmissionResult,
    SyncEntry,
    SyncResult,
)


def _normalisiere_tags(werte: list[str] | None) -> list[str] | None:
    """Splittet jeden `--tag`-Wert zusätzlich an Kommas.

    `--tag` ist wiederholbar (`action="append"`), aber ein einzelner Aufruf
    wie `--tag "a,b,c"` sah bisher aus wie ein Erfolg und landete klaglos
    als EIN Tag `"a,b,c"` — die serverseitige Filterung (`tags @> [...]`)
    verlangt exakte Array-Elemente und fand ihn danach nie wieder.
    """
    if werte is None:
        return None
    ergebnis: list[str] = []
    for wert in werte:
        for teil in wert.split(","):
            teil = teil.strip()
            if teil and teil not in ergebnis:
                ergebnis.append(teil)
    return ergebnis


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
        print(
            f"{treffer.score:.3f}  {treffer.project_slug}/{treffer.title}"
            f"  ({treffer.entry_id})"
        )
        print(f"    {treffer.snippet}")
    print(f"\n{len(ergebnis.hits)} Treffer, Begriffe: {', '.join(ergebnis.terms)}")


def _hinweis_degradation(ergebnis: SearchResult) -> None:
    """Auf stderr, damit `--json` und Pipes sauber bleiben: Ohne Vektorzweig
    ist eine leere oder dünne Trefferliste kein „gibt es nicht" (#206)."""
    if ergebnis.degraded:
        print(
            "Hinweis: Vektorzweig nicht verfügbar — Treffer nur aus dem "
            "Volltext, Umschreibungen können fehlen.",
            file=sys.stderr,
        )


def _print_store(ergebnis: SubmissionResult) -> None:
    print(f"verdict={ergebnis.verdict}")
    if ergebnis.entry_id is not None:
        print(f"entry_id={ergebnis.entry_id} status={ergebnis.status}")
    if ergebnis.duplicate_of is not None:
        print(f"duplicate_of={ergebnis.duplicate_of}")
    if ergebnis.replaced:
        print("replaced=true")
    for befund in ergebnis.findings:
        print(f"  [{befund.severity}] {befund.gate}/{befund.code}: {befund.hint}")


def _print_remove(ergebnis: dict[str, Any]) -> None:
    print(f"external_key={ergebnis['external_key']} removed=true")


def _print_sync(ergebnis: SyncResult) -> None:
    kopf = f"verdict={ergebnis.verdict} source_revision={ergebnis.source_revision}"
    if ergebnis.counts is not None:
        z = ergebnis.counts
        kopf += (
            f" stored={z.stored} replaced={z.replaced} merged={z.merged} "
            f"archived={z.archived}"
        )
    print(kopf)
    for schluessel in ergebnis.archived:
        print(f"  archiviert: {schluessel}")
    for urteil in ergebnis.results:
        for befund in urteil.findings:
            print(
                f"  {urteil.external_key}: [{befund.severity}] "
                f"{befund.gate}/{befund.code}: {befund.hint}"
            )


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


def _print_review_queue(eintraege: list[ReviewEntry]) -> None:
    if not eintraege:
        print("Keine wartenden Einträge.")
        return
    for eintrag in eintraege:
        ersetzt = (
            f" superseded_by={eintrag.superseded_by}" if eintrag.superseded_by else ""
        )
        print(
            f"{eintrag.entry_id}  {eintrag.title}  (status={eintrag.status}){ersetzt}"
        )


def _print_review_entry(eintrag: ReviewEntry) -> None:
    print(f"entry_id={eintrag.entry_id} status={eintrag.status}")
    if eintrag.superseded_by is not None:
        print(f"superseded_by={eintrag.superseded_by}")


class ManifestError(Exception):
    """Das Manifest ist nicht lesbar oder hat die falsche Form — Abbruch vor
    jedem Netzwerkzugriff."""


_MANIFEST_SCHLUESSEL = frozenset({"source_revision", "entries"})
_EINTRAG_PFLICHT = ("external_key", "title", "content", "source")
_EINTRAG_ERLAUBT = frozenset(
    {*_EINTRAG_PFLICHT, "category", "tags", "evidence", "confidence"}
)


def _lade_manifest(
    pfad: str, source_revision: str | None, *, token_stdin: bool
) -> tuple[str, list[SyncEntry]]:
    """Liest das Manifest (`-` = stdin) und prüft seine **Form**, nicht seinen
    Inhalt — die Inhaltsprüfung (Schlüsselformat, Geheimnisse, Längen) macht
    der Server. Strikt bei unbekannten Schlüsseln: Ein Tippfehler wie `tag`
    statt `tags` würde sonst stillschweigend Tags verlieren, und bei einem
    Sync, der Fehlendes archiviert, sind stille Verluste die teure Sorte."""
    if pfad == "-":
        if token_stdin:
            raise ManifestError(
                "--manifest - und --token-stdin lesen beide von stdin — "
                "das Manifest aus einer Datei oder das Token aus der Umgebung nehmen"
            )
        text = sys.stdin.read()
    else:
        try:
            # `utf-8-sig`: Ein Editor, der ein BOM schreibt, soll nicht als
            # „kein gültiges JSON" enden.
            text = Path(pfad).read_text(encoding="utf-8-sig")
        except OSError as fehler:
            raise ManifestError(f"Manifest nicht lesbar: {fehler}") from fehler
    try:
        daten = json.loads(text)
    except ValueError as fehler:
        raise ManifestError(f"Manifest ist kein gültiges JSON: {fehler}") from fehler
    if not isinstance(daten, dict):
        raise ManifestError("Manifest muss ein JSON-Objekt sein")
    unbekannt = sorted(set(daten) - _MANIFEST_SCHLUESSEL)
    if unbekannt:
        raise ManifestError(f"Unbekannte Schlüssel im Manifest: {', '.join(unbekannt)}")
    revision = (
        source_revision if source_revision is not None else daten.get("source_revision")
    )
    if not isinstance(revision, str) or not revision:
        raise ManifestError(
            "source_revision fehlt — im Manifest oder mit --source-revision angeben"
        )
    roh = daten.get("entries")
    if not isinstance(roh, list):
        raise ManifestError("'entries' muss eine Liste sein")

    eintraege: list[SyncEntry] = []
    for i, eintrag in enumerate(roh):
        stelle = f"entries[{i}]"
        if not isinstance(eintrag, dict):
            raise ManifestError(f"{stelle} muss ein Objekt sein")
        unbekannt = sorted(set(eintrag) - _EINTRAG_ERLAUBT)
        if unbekannt:
            raise ManifestError(
                f"{stelle}: unbekannte Schlüssel: {', '.join(unbekannt)}"
            )
        for pflicht in _EINTRAG_PFLICHT:
            if not isinstance(eintrag.get(pflicht), str):
                raise ManifestError(f"{stelle}: '{pflicht}' fehlt oder ist kein Text")
        for liste in ("tags", "evidence"):
            wert = eintrag.get(liste, [])
            if not isinstance(wert, list) or not all(isinstance(x, str) for x in wert):
                raise ManifestError(
                    f"{stelle}: '{liste}' muss eine Liste von Texten sein"
                )
        confidence = eintrag.get("confidence", 0.5)
        if isinstance(confidence, bool) or not isinstance(confidence, int | float):
            raise ManifestError(f"{stelle}: 'confidence' muss eine Zahl sein")
        kategorie = eintrag.get("category")
        if kategorie is not None and not isinstance(kategorie, str):
            raise ManifestError(f"{stelle}: 'category' muss Text sein")
        eintraege.append(
            SyncEntry(
                external_key=eintrag["external_key"],
                title=eintrag["title"],
                content=eintrag["content"],
                source=eintrag["source"],
                category=kategorie,
                tags=tuple(eintrag.get("tags", ())),
                evidence=tuple(eintrag.get("evidence", ())),
                confidence=float(confidence),
            )
        )
    return revision, eintraege


def _output(wert: Any, *, als_json: bool, menschlich: Callable[[Any], None]) -> None:
    if als_json:
        print(json.dumps(_jsonable(wert), indent=2, ensure_ascii=False))
    else:
        menschlich(wert)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dbrain", description="Client für developers-brain"
    )
    parser.add_argument("--url", help="Server-URL (sonst DBRAIN_URL oder Config-Datei)")
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
    parser.add_argument("--version", action="version", version=f"dbrain {__version__}")

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
    suche.add_argument(
        "--max-cosine-distance",
        type=float,
        dest="max_cosine_distance",
        help="Relevanz-Schwelle des Vektorzweigs für diese Anfrage — nur enger "
        "als die Grenze des Servers (#206)",
    )

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
    einreichen.add_argument("--evidence", action="append", dest="evidence", default=[])
    einreichen.add_argument("--confidence", type=float, default=0.5)
    einreichen.add_argument(
        "--external-key",
        dest="external_key",
        help="Schlüssel der Quelle: legt an oder ersetzt (PUT) statt "
        "einzureichen (POST) — für synchronisierte Projekte (#206)",
    )

    feedback = sub.add_parser("feedback", help="Feedback zu einem Eintrag abgeben")
    feedback.add_argument("entry_id")
    helpful = feedback.add_mutually_exclusive_group(required=True)
    helpful.add_argument("--helpful", action="store_true", dest="helpful")
    helpful.add_argument("--not-helpful", action="store_false", dest="helpful")
    feedback.add_argument("--comment")

    sub.add_parser("projects", help="Effektive Projektmenge anzeigen")

    entfernen = sub.add_parser(
        "remove",
        help="Eintrag über seinen Schlüssel archivieren (#206, braucht maintainer)",
    )
    entfernen.add_argument("--project", required=True, help="Projekt (Slug oder UUID)")
    entfernen.add_argument("--external-key", required=True, dest="external_key")

    sync = sub.add_parser(
        "sync",
        help="Projektbestand auf ein Manifest bringen — alles oder nichts, "
        "Fehlendes wird archiviert (#206, braucht maintainer)",
    )
    sync.add_argument("--project", required=True, help="Projekt (Slug oder UUID)")
    sync.add_argument(
        "--manifest",
        required=True,
        help='JSON-Datei mit {"source_revision": …, "entries": [{external_key, '
        "title, content, source, category?, tags?, evidence?, confidence?}]} — "
        "`-` liest von stdin",
    )
    sync.add_argument(
        "--source-revision",
        dest="source_revision",
        help="Quell-Revision (etwa Commit-Hash); überschreibt die im Manifest",
    )

    review = sub.add_parser(
        "review", help="Kuration der Review-Queue (#93, braucht maintainer)"
    )
    review_sub = review.add_subparsers(dest="review_command", required=True)

    review_list = review_sub.add_parser("list", help="Wartende Einträge anzeigen")
    review_list.add_argument(
        "--project", required=True, help="Projekt (Slug oder UUID)"
    )
    review_list.add_argument("--limit", type=int)
    review_list.add_argument("--offset", type=int)

    review_approve = review_sub.add_parser(
        "approve", help="Eintrag freigeben — pending_review → active"
    )
    review_approve.add_argument(
        "--project", required=True, help="Projekt (Slug oder UUID)"
    )
    review_approve.add_argument("--entry", required=True, dest="entry_id")

    review_reject = review_sub.add_parser(
        "reject", help="Eintrag zurückweisen — pending_review → archived"
    )
    review_reject.add_argument(
        "--project", required=True, help="Projekt (Slug oder UUID)"
    )
    review_reject.add_argument("--entry", required=True, dest="entry_id")

    review_edit = review_sub.add_parser(
        "edit", help="Eintrag redigieren — ändert den Status nicht"
    )
    review_edit.add_argument(
        "--project", required=True, help="Projekt (Slug oder UUID)"
    )
    review_edit.add_argument("--entry", required=True, dest="entry_id")
    review_edit.add_argument("--title")
    review_edit.add_argument("--content")
    review_edit.add_argument("--category")
    review_edit.add_argument("--tag", action="append", dest="tags")
    review_edit.add_argument("--confidence", type=float)
    review_edit.add_argument(
        "--superseded-by",
        dest="superseded_by",
        help="Kennung des Eintrags, der diesen ersetzt (#93)",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command in ("search", "store"):
        args.tags = _normalisiere_tags(args.tags)

    manifest: tuple[str, list[SyncEntry]] | None = None
    if args.command == "sync":
        # Vor jedem Netzwerkzugriff: Ein formal kaputtes Manifest soll nie
        # einen Sync anstoßen, der Fehlendes archiviert.
        try:
            manifest = _lade_manifest(
                args.manifest, args.source_revision, token_stdin=args.token_stdin
            )
        except ManifestError as fehler:
            print(f"Fehler: {fehler}", file=sys.stderr)
            return 1

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
                    max_cosine_distance=args.max_cosine_distance,
                )
                _output(suchergebnis, als_json=args.json, menschlich=_print_search)
                _hinweis_degradation(suchergebnis)
                return 0

            if args.command == "store":
                inhalt = args.content if args.content is not None else sys.stdin.read()
                if args.external_key is not None:
                    einreichungsergebnis = client.upsert(
                        project=args.project,
                        external_key=args.external_key,
                        title=args.title,
                        content=inhalt,
                        source=args.source,
                        category=args.category,
                        tags=args.tags,
                        evidence=args.evidence,
                        confidence=args.confidence,
                    )
                else:
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

            if args.command == "remove":
                client.remove(args.project, args.external_key)
                _output(
                    {"external_key": args.external_key, "removed": True},
                    als_json=args.json,
                    menschlich=_print_remove,
                )
                return 0

            if args.command == "sync":
                if manifest is None:  # unerreichbar: oben für diesen Befehl geladen
                    return 1
                revision, eintraege_sync = manifest
                sync_ergebnis = client.sync(
                    args.project, source_revision=revision, entries=eintraege_sync
                )
                _output(sync_ergebnis, als_json=args.json, menschlich=_print_sync)
                # Wie `store`s `rejected`: Der Grund steht in der Ausgabe,
                # der Exit-Code lässt eine CI fehlschlagen.
                return 0 if sync_ergebnis.ok else 1

            if args.command == "projects":
                projekte = client.list_projects()
                _output(projekte, als_json=args.json, menschlich=_print_projects)
                return 0

            if args.command == "review":
                if args.review_command == "list":
                    eintraege = client.list_review_queue(
                        args.project, limit=args.limit, offset=args.offset
                    )
                    _output(
                        eintraege, als_json=args.json, menschlich=_print_review_queue
                    )
                    return 0

                if args.review_command == "approve":
                    ergebnis = client.approve_review(args.project, args.entry_id)
                    _output(
                        ergebnis, als_json=args.json, menschlich=_print_review_entry
                    )
                    return 0

                if args.review_command == "reject":
                    # Anders als `store`s `rejected`-Verdict ist das hier
                    # eine erfolgreiche Kuratierungsentscheidung, kein
                    # gescheiterter Versuch — Exit 0 bei Erfolg, dieselbe
                    # normale Fehlerbehandlung wie jeder andere Aufruf.
                    ergebnis = client.reject_review(args.project, args.entry_id)
                    _output(
                        ergebnis, als_json=args.json, menschlich=_print_review_entry
                    )
                    return 0

                if args.review_command == "edit":
                    ergebnis = client.edit_review(
                        args.project,
                        args.entry_id,
                        title=args.title,
                        content=args.content,
                        category=args.category,
                        tags=args.tags,
                        confidence=args.confidence,
                        superseded_by=args.superseded_by,
                    )
                    _output(
                        ergebnis, als_json=args.json, menschlich=_print_review_entry
                    )
                    return 0
        except BrainError as fehler:
            print(f"Fehler: {fehler}", file=sys.stderr)
            return 1

    return 1  # unerreichbar bei bekanntem Subcommand — argparse erzwingt einen gültigen


if __name__ == "__main__":
    sys.exit(main())
