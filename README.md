# dbrain

Dünner Python-Client + CLI für [developers-brain](https://github.com/julianhyatt/developers-brain)
— für Skripte, Hooks und CI-Jobs, die suchen, einreichen oder Feedback geben
wollen, ohne den Server-Stack (FastAPI, SQLAlchemy, Postgres-Treiber) zu
installieren. Einzige Laufzeit-Abhängigkeit: [httpx](https://www.python-httpx.org/).

## Installation

```bash
pip install "dbrain @ git+https://github.com/julianhyatt/developers-brain-sdk.git@v0.2.0"
```

Immer gegen einen Tag installieren, nie gegen `@main` — sonst installieren
zwei CI-Läufe desselben Commits zu unterschiedlichen Zeitpunkten
unterschiedliche SDK-Versionen.

## Konfiguration

Server-URL und Token werden in dieser Reihenfolge aufgelöst:
**CLI-Flag > Umgebungsvariable > Config-Datei > Fehler.**

```bash
export DBRAIN_URL="https://brain.example.internal"
export DBRAIN_TOKEN="dbrain_…"
```

Oder über eine Config-Datei unter `~/.config/dbrain/config.toml`:

```toml
base_url = "https://brain.example.internal"

# Optional: benannte Profile, ausgewählt über --profile <name>
[profiles.staging]
base_url = "https://brain-staging.example.internal"
token = "dbrain_…"
```

**Kein `--token`-Flag.** Ein Geheimnis als Kommandozeilenargument landet
unweigerlich in der Shell-History und ist über `ps`/`/proc/<pid>/cmdline`
für andere Prozesse auf derselben Maschine sichtbar — auf einem geteilten
CI-Runner oder Mehrbenutzer-Host ein Leak. Für ein Token, das nicht in der
Umgebung stehen soll, gibt es `--token-stdin`:

```bash
echo -n "$TOKEN" | dbrain --token-stdin projects
```

## CLI

```bash
# Effektive Projektmenge anzeigen — der erste Aufruf, bevor du Slugs rätst
dbrain projects

# Suchen
dbrain search "wie läuft eine Datenbankmigration ohne Ausfallzeit"
dbrain search "migration" --project backend --limit 5 --json
dbrain search "migration" --max-cosine-distance 0.4   # Relevanz-Schwelle (#206)

# Einreichen (Inhalt aus --content oder von stdin)
dbrain store --project backend --title "Titel" --source ci-agent \
  --content "Markdown-Inhalt" --tag postgres --confidence 0.8

cat notiz.md | dbrain store --project backend --title "Titel" --source ci-agent

# Feedback abgeben
dbrain feedback <entry-id> --helpful --comment "hat geholfen"

# Kuration (#93, braucht mindestens maintainer im Zielprojekt)
dbrain review list --project backend
dbrain review approve --project backend --entry <entry-id>
dbrain review reject --project backend --entry <entry-id>
dbrain review edit --project backend --entry <entry-id> \
  --superseded-by <ersetzender-entry-id>

# Synchronisierte Quellen (#206) — braucht maintainer und ein Projekt im Modus
# `synced`, siehe den Abschnitt unten
dbrain store --project docs --external-key core.haushalt~name \
  --title "Haushalt umbenennen" --source ci --content "…"   # legt an oder ersetzt
dbrain remove --project docs --external-key core.haushalt~name  # archiviert
dbrain sync --project docs --manifest manifest.json --source-revision "$COMMIT_SHA"
```

Jeder Unterbefehl unterstützt `--json` für maschinenlesbare Ausgabe;
ohne das Flag ist die Ausgabe menschenlesbarer Text. `store` liefert
Exit-Code `1`, wenn die Einreichung `rejected` wurde — der Grund steht
in den ausgegebenen `findings`, kein separater Fehlerpfad. `review
reject` ist davon bewusst verschieden: Anders als eine abgelehnte
Einreichung ist ein Review-`reject` eine **erfolgreiche**
Kuratierungsentscheidung und liefert Exit-Code `0`; Exit `1` bedeutet bei
`review` ausschließlich einen echten Fehler (Auth, Netz, HTTP). `sync`
liefert wie `store` Exit-Code `1`, wenn die Prüfstrecke abgelehnt hat — die
Begründung je Schlüssel steht in der Ausgabe, eine CI schlägt damit fehl.

Ist der Vektorzweig der Suche ausgefallen (`vector_branch: unavailable`),
kommen die Treffer allein aus dem Volltext. `dbrain search` sagt das auf
**stderr** — `--json` und Pipes bleiben sauber —, denn eine leere Liste in
diesem Zustand heißt nicht „gibt es nicht".

## Synchronisierte Quellen (#206)

Für Dokumentation, die in einem Repository liegt und per CI in ein Projekt
gespiegelt wird: Jeder Eintrag trägt den **Schlüssel**, unter dem die Quelle
ihn führt, und ein Manifest bringt das Projekt in einem Zug auf den Stand
der Quelle. Voraussetzung: ein Projekt im Modus `synced` (setzt ein Admin)
und mindestens die Rolle `maintainer`.

```json
{
  "source_revision": "3f9c2ab",
  "entries": [
    {
      "external_key": "core.haushalt~name",
      "title": "Haushalt umbenennen",
      "content": "1. Öffne die Einstellungen …",
      "source": "docs-ci",
      "category": "help-article",
      "tags": ["help", "module:core"]
    }
  ]
}
```

- **Alles oder nichts.** Ein Sync ist eine Transaktion. Jeder Eintrag wird
  angelegt, ersetzt oder als unverändert erkannt; jeder Schlüssel des
  Projekts, der **nicht** im Manifest steht, wird archiviert. Ein leeres
  Manifest archiviert den ganzen Bestand. Lehnt die Prüfstrecke einen
  Eintrag ab (Geheimnis, Sperrliste, Länge), wird **nichts** geschrieben —
  die Ausgabe nennt dann alle Befunde auf einmal.
- **Der Schlüssel** besteht aus Kleinbuchstaben, Ziffern und den Trennern
  `.` `_` `~` `-` (nie am Rand, nie doppelt, höchstens 200 Zeichen). **Kein
  `#`:** Der Schlüssel steht im URL-Pfad, und `#` ist dort der
  Fragment-Trenner — wer eine Quelle mit `artikel#abschnitt` hat, bildet den
  Trenner auf `~` ab. Das SDK kodiert den Schlüssel immer; ein ungültiger
  scheitert deshalb mit 422, statt still gekürzt zu werden.
- **Streng bei der Form.** Das Manifest wird vor jedem Netzwerkzugriff
  geprüft, und unbekannte Schlüssel sind ein Fehler: Ein Tippfehler wie
  `tag` statt `tags` würde bei einem Sync, der Fehlendes archiviert, still
  Daten verlieren. Es kann aus einer Datei oder von stdin (`--manifest -`)
  kommen — nicht zusammen mit `--token-stdin`.
- **Kennungen bleiben stabil.** Ein geänderter Abschnitt wird an Ort und
  Stelle ersetzt (Feedback und Nutzungszähler hängen an der Kennung), Inhalt
  und Vektor sind neu. Wird ein Schlüssel in der Quelle umbenannt, ist das
  ein neuer Eintrag und ein archivierter alter.
- **Grenzen des Servers:** höchstens 1000 Einträge und 8 MiB je Sync. Der
  Sync-Aufruf hat einen Timeout von 300 s (`timeout=` überschreibt ihn), der
  einzelne Upsert 30 s.
- **Frische der Suche.** Der Server puffert Trefferlisten kurz (Vorgabe
  30 s) und leert den Puffer nicht bei einem Sync: Dieselbe Suche, die kurz
  vor dem Sync lief, kann bis zum Ablauf noch den alten Stand liefern. Eine
  CI, die direkt nach dem Sync prüft, wartet diese Spanne ab oder fragt
  anders (etwa mit anderem `limit`).

## SDK

```python
from dbrain import BrainClient

with BrainClient("https://brain.example.internal", token) as client:
    ergebnis = client.search("datenbankmigration", limit=5)
    for treffer in ergebnis.hits:
        print(treffer.project_slug, treffer.title, treffer.score)

    urteil = client.store(
        project="backend",  # Slug oder UUID — ein Slug wird über
        # GET /v1/projects aufgelöst
        title="Alembic-Downgrade scheitert bei nativen Enums",
        content="…",
        source="ci-agent",
        tags=["alembic", "postgres"],
    )
    if urteil.verdict == "rejected":
        for befund in urteil.findings:
            print(befund.severity, befund.hint)
    elif urteil.entry_id is not None:  # nicht bei "merged" — dort None
        client.feedback(urteil.entry_id, helpful=True)

    # Kuration (#93) — dieselbe maintainer-Rolle wie bei `dbrain review`
    for wartend in client.list_review_queue("backend"):
        client.approve_review("backend", wartend.entry_id)

    # Synchronisierte Quellen (#206)
    ergebnis = client.sync(
        "docs",
        source_revision="3f9c2ab",
        entries=[
            SyncEntry("core.haushalt~name", "Haushalt umbenennen", "…", "docs-ci"),
        ],
    )
    if not ergebnis.ok:  # kein Fehler, sondern ein Ergebnis — nichts wurde geschrieben
        for urteil in ergebnis.results:
            for befund in urteil.findings:
                print(urteil.external_key, befund.severity, befund.hint)
    else:
        print(ergebnis.counts, ergebnis.archived)

    client.upsert(  # einzelner Schlüssel: legt an oder ersetzt
        project="docs",
        external_key="core.haushalt~name",
        title="Haushalt umbenennen",
        content="…",
        source="docs-ci",
    )
    client.remove("docs", "core.haushalt~name")  # archiviert

    suche = client.search("haushalt", max_cosine_distance=0.4)
    if suche.degraded:  # Vektorzweig ausgefallen — nur Volltext-Treffer
        print("Hinweis: Umschreibungen können fehlen")
```

`SyncEntry` gehört zu `from dbrain import BrainClient, SyncEntry`.

### Fehlerbehandlung

Alle Fehler erben von `dbrain.BrainError`:

| Exception | Bedeutung |
|---|---|
| `BrainAuthError` | 401/403 — kein oder zu schwaches Token |
| `BrainNotFoundError` | 404 — Ressource außerhalb der effektiven Projektmenge oder existiert nicht |
| `BrainValidationError` | 422 — Schema-Fehler oder eine echte Ablehnung (z. B. Secret im Feedback-Kommentar) |
| `BrainAmbiguousError` | 5xx nach einer Antwort auf `store()`/`feedback()` — der Server hat möglicherweise committet, siehe unten |
| `BrainRateLimitError` | 429 nach Ausschöpfen der Retries |
| `BrainLockConflictError` | 409 **mit** `Retry-After` nach Ausschöpfen der Retries — der Server hat abgebrochen, weil dieselben Zeilen gerade geändert werden (etwa ein laufender Sync im selben Projekt); nichts wurde geschrieben, ein späterer Versuch ist zulässig |
| `BrainConnectionError` | Verbindungsfehler nach Ausschöpfen der Retries |

**`store()`/`feedback()` werfen `BrainAmbiguousError` statt automatisch zu
wiederholen, wenn ein 5xx *nach* einer Serverantwort kommt** — anders als
`search()`/`list_projects()`, die ohne Nebenwirkung sind und jeden
5xx/Timeout retryen. Der Grund: Ohne Idempotency-Key lässt sich
clientseitig nicht unterscheiden, ob der Server bereits committet hat,
bevor er den Fehler zurückgab — ein automatischer Retry könnte einen
zweiten Eintrag anlegen (`store()`) oder ein zweites Feedback zählen
(`feedback()`, wirkt sich auf den Confidence-Streak aus). Ein
Verbindungsfehler *vor* jeder Antwort wird dagegen für alle Methoden
retryt — der Request hat den Server nachweislich nie erreicht. 429 ist
eine Ausnahme von dieser Regel und wird für jede Methode retryt (unter
Beachtung des `Retry-After`-Headers): Das Rate-Limit-Budget ist die erste
Prüfung in der Server-Kette, vor jeder Schreiblogik — eine 429-Antwort
bedeutet immer „nie verarbeitet".

**409 mit `Retry-After` wird für jede Methode wiederholt** (#206): So
antwortet der Server, wenn die Anfrage an einer Zeilensperre abgebrochen
wurde — nichts geschrieben, also wie bei 429 sicher retryable. Ein 409
**ohne** `Retry-After` ist ein fachlicher Konflikt (etwa `approve_review()`
auf einen Eintrag, der nicht zur Prüfung ansteht) und kommt unverändert als
`BrainHTTPError` an.

**`upsert()`, `remove()` und `sync()` sind idempotent** — sie adressieren
einen Zustand über einen Schlüssel, keine Aktion. Deshalb werden sie wie
`search()` auch nach einem Timeout oder 5xx wiederholt. Preis: Nach einem
Retry kann das Urteil `merged` lauten, obwohl schon der erste Versuch
geschrieben hat.

`rejected` ist **kein** Fehler: `store()` gibt auch eine Ablehnung als
normales `SubmissionResult` zurück (`verdict == "rejected"`,
`findings` nennt den Grund) — ein Aufrufer, der nur eine Exception fängt,
würde die Begründung sonst nie sehen. Dasselbe gilt für `upsert()` und für
`sync()` (`result.ok` ist dann `False`, `result.results` nennt alle Urteile).

### Antwortmodelle sind additiv-tolerant

Neue, unbekannte Felder in einer Server-Antwort führen nicht zu einem
Fehler — sie werden beim Parsen ignoriert. Das ist die Client-seitige
Hälfte der Additivstabilität von `/v1` (siehe `ADR-004 Amendment 001` im
Hauptrepo): Der Server darf `/v1` um neue optionale Felder erweitern,
ohne ein per Tag gepinntes SDK zu brechen.

## Entwicklung

```bash
uv sync
uv run ruff check .
uv run mypy .
uv run pytest
```

Tests laufen gegen `httpx.MockTransport` (kein echter Server nötig) und
prüfen die Client-Logik — Retry-Verhalten, Fehler-Zuordnung,
Slug-Auflösung. Sie sind kein Ersatz für einen Contract-Test gegen die
echte `/v1`-Fassade des Hauptrepos; das ist ein offener Folgepunkt.
