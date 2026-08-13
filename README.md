# dbrain

Dünner Python-Client + CLI für [developers-brain](https://github.com/julianhyatt/developers-brain)
— für Skripte, Hooks und CI-Jobs, die suchen, einreichen oder Feedback geben
wollen, ohne den Server-Stack (FastAPI, SQLAlchemy, Postgres-Treiber) zu
installieren. Einzige Laufzeit-Abhängigkeit: [httpx](https://www.python-httpx.org/).

## Installation

```bash
pip install "dbrain @ git+https://github.com/julianhyatt/developers-brain-sdk.git@v0.1.1"
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

# Einreichen (Inhalt aus --content oder von stdin)
dbrain store --project backend --title "Titel" --source ci-agent \
  --content "Markdown-Inhalt" --tag postgres --confidence 0.8

cat notiz.md | dbrain store --project backend --title "Titel" --source ci-agent

# Feedback abgeben
dbrain feedback <entry-id> --helpful --comment "hat geholfen"
```

Jeder Unterbefehl unterstützt `--json` für maschinenlesbare Ausgabe;
ohne das Flag ist die Ausgabe menschenlesbarer Text. `store` liefert
Exit-Code `1`, wenn die Einreichung `rejected` wurde — der Grund steht
in den ausgegebenen `findings`, kein separater Fehlerpfad.

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
```

### Fehlerbehandlung

Alle Fehler erben von `dbrain.BrainError`:

| Exception | Bedeutung |
|---|---|
| `BrainAuthError` | 401/403 — kein oder zu schwaches Token |
| `BrainNotFoundError` | 404 — Ressource außerhalb der effektiven Projektmenge oder existiert nicht |
| `BrainValidationError` | 422 — Schema-Fehler oder eine echte Ablehnung (z. B. Secret im Feedback-Kommentar) |
| `BrainAmbiguousError` | 5xx nach einer Antwort auf `store()`/`feedback()` — der Server hat möglicherweise committet, siehe unten |
| `BrainRateLimitError` | 429 nach Ausschöpfen der Retries |
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

`rejected` ist **kein** Fehler: `store()` gibt auch eine Ablehnung als
normales `SubmissionResult` zurück (`verdict == "rejected"`,
`findings` nennt den Grund) — ein Aufrufer, der nur eine Exception fängt,
würde die Begründung sonst nie sehen.

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
