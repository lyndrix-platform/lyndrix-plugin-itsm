# IT Service Management (ITSM) — Dokumentation

## Überblick

Das ITSM-Plugin bringt Incident-, Change-, Problem-, Request- und Task-Management nach Lyndrix —
als **eigene Suite** (native mode) und, über Connector-Adapter, als **Gateway** zu bestehenden
Unternehmenssystemen (BMC Helix ITSM, ServiceNow). Ziel ist, in eine vorhandene Werkzeuglandschaft
hineinzupassen statt ein weiteres Tool zu sein: In Gateway-Betrieb bleibt das externe System die
„Wahrheit“, Lyndrix füttert und spiegelt.

Phase 1 (dieser Stand) liefert die **Grundfunktionen** — bewusst so modular, dass spätere
Erweiterungen (Event-Management, Change-Freigaben, Connectoren, SLAs) andocken, ohne das Modell
anzufassen.

**Leitprinzip: Prozess ist Konfiguration.** Was ein Incident oder Change *ist* — Status,
Übergänge, Pflichtfelder, Untertypen, Kategorien, Prioritätsmatrix — steht in YAML
(`catalog/record_types/*.yml`), nicht im Code. Ein neuer Datensatztyp oder ein zusätzliches Feld
braucht keine Codeänderung.

## Architektur

```
catalog/record_types/*.yml   # Prozessdefinitionen (incident, change, problem, request, task)
app/model/models.py          # Record / JournalEntry / CiLink / RecordLink / Attachment / Counter
app/model/database.py        # Session-Helfer + Tabellen-Bootstrap (handgeschriebene Migration)
app/logic/catalog.py         # validierter Katalog-Loader (pydantic, Querverweis-Prüfung)
app/logic/service.py         # CRUD, Übergänge, Journal, Verknüpfungen, Anhänge, Events
app/logic/attachments.py     # Dateiablage (UUID-Dateinamen, kein Path-Traversal)
app/logic/connectors/        # ConnectorAdapter-Schnittstelle (Gateway-Betrieb, spätere Phase)
app/api/__init__.py          # REST-Router — beide UIs nutzen nur diesen
app/ui/react/                # React-UI (Bundle → app/ui/static/ui_bundle.js)
```

### Datenmodell

| Tabelle | Inhalt |
|---|---|
| `itsm_records` | ein Datensatz je Zeile: `uuid` (PK), `number` (`INC0000042`, eindeutig), `record_type`, `subtype`, `category`, `title`, `description`, `state`, `impact`, `urgency`, `priority` (abgeleitet), `assignment_group`, `assignee`, `requester`, `created_by`, `custom_fields` (JSON), `tags` (JSON), `opened_at`/`resolved_at`/`closed_at`/`due_at`, `version` (optimistisches Sperren), Gateway-Felder `source`/`external_id`/`external_url`/`sync_state`/`sync_error` |
| `itsm_journal` | append-only Verlauf: `comment`, `work_note`, `state_change`, `field_change`, `link`, `attachment`, `system` — jede Änderung hinterlässt einen Eintrag (Audit-Trail) |
| `itsm_ci_links` | Datensatz ↔ Configuration Item (UUID aus der Entity Registry) mit Rolle `affected` / `caused_by` / `changed` / `related`; Anzeigename/Typ sind gecacht, damit die UI auch ohne Registry funktioniert |
| `itsm_record_links` | Datensatz ↔ Datensatz (`parent_of`, `caused_by`, `implements`, `duplicate_of`, `related_to` + Inverse) |
| `itsm_attachments` | Metadaten; die Bytes liegen unter `<attachment_dir>/<record uuid>/<attachment uuid>` |
| `itsm_counters` | Nummernkreis je Typ, inkrementiert unter Zeilensperre in derselben Transaktion wie das Anlegen |

### Regeln, die der Service erzwingt

- **Übergänge** nur entlang `transitions` des Katalogs → sonst HTTP 409.
- **Pflichtfelder** eines Zielstatus (`requires`) müssen vorhanden sein — entweder bereits am
  Datensatz oder im `fields`-Teil des Übergangs → sonst HTTP 400 mit Liste der fehlenden Felder.
- **Priorität** wird aus `impact × urgency` über die Matrix abgeleitet und bei Änderung
  neu berechnet; nie direkt setzbar.
- **Optimistisches Sperren**: `PATCH` und Übergänge dürfen `version` mitsenden; stimmt sie nicht
  → HTTP 409. Jede Änderung erhöht `version`.
- **Status nur per Übergang** — `PATCH` mit `state` wird abgelehnt.
- Zeitstempel folgen der Status-`kind`: `resolved` setzt `resolved_at`, `closed` setzt `closed_at`,
  ein Wechsel zurück in einen `open`-Status löscht beide.
- Custom Fields werden typgeprüft (`text` / `select` / `int` / `bool` / `datetime`), unbekannte
  Felder abgelehnt.
- Jeder neue Datensatz wird als Entität `itsm_record` bei der Entity Registry angemeldet
  (`entity_registry:register`, best effort).

## Katalog-Schema (`catalog/record_types/<typ>.yml`)

```yaml
id: incident                 # Typ-ID (snake_case)
label: Incident
prefix: INC                  # Nummernpräfix, eindeutig über alle Typen
number_width: 7              # INC0000042
icon: report_problem         # Material Icon
mode: native                 # native | gateway:<connector id>
description: ...
subtypes: [{id: outage, label: Outage}, ...]
categories:                  # beliebig verschachtelt
  - {id: network, label: Network, children: [{id: dns, label: DNS}]}
priority:
  impact:  [{id: 1, label: High}, {id: 2, label: Medium}, {id: 3, label: Low}]
  urgency: [{id: 1, label: High}, {id: 2, label: Medium}, {id: 3, label: Low}]
  defaults: {impact: 3, urgency: 3}
  matrix: {"1-1": P1, "1-2": P2, ...}          # "<impact>-<urgency>" → Level
  levels: [{id: P1, label: Critical, color: down}, ...]   # color = Theme-State-Token
states:
  - {id: new, label: New, initial: true, kind: open}      # genau ein initial
  - {id: resolved, label: Resolved, kind: resolved, requires: [resolution_code, resolution_notes]}
  - {id: closed, label: Closed, kind: closed, final: true}
transitions:
  new: [in_progress, resolved, cancelled]
  resolved: [closed, in_progress]
fields:                      # Custom Fields → custom_fields JSON
  - {id: resolution_code, label: Resolution code, kind: select, options: [{id: fixed, label: Fixed}]}
  - {id: resolution_notes, label: Resolution notes, kind: text, multiline: true}
assignment_groups:
  - {id: infra, label: Infrastructure, core_group: INT_ADMIN}
```

Der Loader prüft Querverweise (Übergänge → Status, `requires` → Felder oder Kernfelder
`assignee`/`assignment_group`/`category`/`subtype`/`description`, Matrix → Level) und
Eindeutigkeit von IDs und Präfixen. Ein defekter Katalog verhindert den Plugin-Start bzw. lässt
bei `reload` den alten Stand aktiv (HTTP 400).

Mitgeliefert: `incident`, `change` (standard/normal/emergency, Risiko, Plan-/Rollback-Felder,
Status `approval` als Platzhalter für die spätere Freigabe-Logik), `problem` (Known Error),
`request`, `task`.

## REST-API

Basis: `/api/plugins/lyndrix.plugin.itsm/`. Rechte: `plugin:lyndrix.plugin.itsm:api:read`,
`api:write`, `itsm:admin`. `{ref}` ist UUID **oder** Nummer (`INC0000042`).

| Methode | Pfad | Zweck |
|---|---|---|
| `GET` | `/records?type=&state=&priority=&assignee=&group=&tag=&ci=&q=&include_closed=&limit=&offset=` | Liste (geschlossene standardmäßig ausgeblendet) |
| `POST` | `/records` | anlegen → `201 {record}` |
| `GET` | `/records/{ref}` | Detail inkl. `allowed_transitions`, `journal`, `ci_links`, `links`, `attachments` |
| `PATCH` | `/records/{ref}` | Felder ändern; optional `version` |
| `POST` | `/records/{ref}/transition` | `{to_state, fields?, note?, version?}` |
| `POST` | `/records/{ref}/journal` | `{kind: comment\|work_note, body}` |
| `POST` / `DELETE` | `/records/{ref}/ci-links[/{role}/{entity_uuid}]` | CI-Verknüpfungen. Beim Anlegen `entity_uuid` **oder** `ci` (jede Referenz, die die Entity Registry auflöst: UUID, `iac.host:docker-hydra`, reiner Identifier-Wert `docker-hydra`, Anzeigename) plus `role` (`affected`, `caused_by`, `changed`, `related`) |
| `POST` / `DELETE` | `/records/{ref}/links[/{relation}/{to}]` | Datensatz-Verknüpfungen (`to` = UUID oder Nummer) |
| `POST` | `/records/{ref}/attachments` | Multipart-Upload (`file`), Größen-/MIME-Limit |
| `GET` / `DELETE` | `/records/{ref}/attachments/{uuid}` | Download / Löschen |
| `GET` | `/catalog` | alle Typdefinitionen + Beziehungs-/Rollenlisten |
| `POST` | `/catalog/reload` | Katalog neu laden (`itsm:admin`) |
| `GET` | `/stats` | offene Datensätze je Typ und Priorität |

Fehlercodes: 400 ungültige Eingabe / fehlende Pflichtfelder, 404 unbekannt, 409 unerlaubter
Übergang oder veraltete `version`, 503 Datenbank noch nicht bereit.

`source`/`external_id` beim Anlegen (Gateway-Schlüssel eines externen Systems) dürfen nur
Administratoren bzw. Connectoren mit `itsm:admin` setzen — ein Agent kann sich so nicht als BMC
ausgeben (sonst 403).

**CI-Auflösung.** Jede CI-Verknüpfung — in `ci_links` beim Anlegen, nachträglich über
`/ci-links` oder per Bus — geht durch die Entity Registry
(`ctx.request("entity_registry:lookup", …)`): ein Name wie `docker-hydra` wird zur UUID,
Anzeigename und Typ werden aus der Registry gecacht, eine zusammengeführte UUID zeigt auf die
überlebende Entität, und unbekannte oder mehrdeutige Referenzen werden mit 400 abgelehnt (die
Kandidaten stehen in der Fehlermeldung). Ist die Registry nicht erreichbar, wird eine rohe UUID
weiterhin akzeptiert (mit dem mitgegebenen Namen), ein Name allein nicht. Die Liste filtert mit
`?ci=<uuid>` auf alle Datensätze zu einem CI.

## Event-Bus

### Konsumiert

| Topic | Payload | Antwort |
|---|---|---|
| `itsm:record_create` | Datensatzfelder + `author`, optional `request_id` | `itsm:record_create_result {request_id, ok, record\|error}` |
| `itsm:record_update` | `{ref, changes, author, version?, request_id?}` | `itsm:record_update_result` |
| `itsm:transition` | `{ref, to_state, fields?, note?, author, version?, request_id?}` | `itsm:transition_result` |
| `itsm:journal_add` | `{ref, kind, body, author}` | — |
| `itsm:ci_link` | `{ref, ci \| entity_uuid, role?, author, request_id?}` — `ci` ist jede Referenz, die die Registry auflöst (`docker-hydra`, UUID, `schema:wert`) | `itsm:ci_link_result {request_id, ok, record, ci}` |

Mit `ctx.request(...)` (Core-API ≥ 1.4) werden daraus Round-Trips; Fehler ohne `request_id`
landen auf `itsm:ingest_failed`. Darüber werden später das Event-Management (Alarm → Incident,
Alarm-Hostname → `itsm:ci_link`) und die IaC-Change-Brücke (Prod-Deploy → Change-Freigabe)
angebunden.

### Emittiert

`itsm:record_created {record}`, `itsm:record_updated {record, changes}`,
`itsm:state_changed {record, from_state, to_state}`, `itsm:journal_added {record_uuid, number,
entry}`, `itsm:ci_link_result` (Antwort), `entity_registry:register` sowie
`entity_registry:lookup` (Request zur CI-Auflösung, Antwort auf `entity_registry:lookup_result`).

### Benachrichtigungen

Endpunkte `record_assigned`, `record_state_changed`, `record_commented` werden über den
Core-Notification-Router ausgeliefert (Benutzer steuern Toast/Verlauf/externe Kanäle in den
Einstellungen).

## Einstellungen (Plugin-Settings-Dialog, in Vault gespeichert)

| Schlüssel | Standard | Bedeutung |
|---|---|---|
| `attachment_dir` | `/data/storage/itsm/attachments` | Ablageort für Anhänge |
| `max_attachment_mb` | `25` | Größenlimit je Datei |
| `catalog_dir` | leer (= mitgelieferter Katalog) | eigenes Verzeichnis mit `record_types/*.yml` — **ersetzt** den mitgelieferten Satz |

Die Einstellungen werden nach `vault:ready_for_data` angewendet.

## Rollen

| Rolle | Rechte | Auto-Mapping |
|---|---|---|
| `admin` | `api:read`, `api:write`, `itsm:admin` | Gruppe `INT_ADMIN` |
| `agent` | `api:read`, `api:write` | — |
| `viewer` | `api:read` | — |

## React-UI

Route `/itsm` (im Shell `/apps/lyndrix-plugin-itsm/itsm`). Deep-Links: `?record=<uuid|nummer>`
öffnet einen Datensatz, `?new=incident&ci=<uuid>` öffnet das Neu-Formular mit dem CI als
*betroffen* vorbelegt (so verlinken Server Manager und Entity Registry „Incident melden“),
`?ci=<uuid>` listet alle Datensätze zu einem CI.

- **Liste**: Suche, Filter Typ/Status/Priorität, „geschlossene einblenden“, CI-Filter-Chip.
- **Neu**: Formular wird aus dem Katalog erzeugt (Untertyp, Kategoriebaum, Impact/Urgency mit
  Prioritätsvorschau, Gruppe, Custom Fields, CI-Picker).
- **CI-Picker** (Neu und Detail): Name oder Identifier tippen (z. B. `docker-hydra`) oder eine
  UUID einfügen — die Entity Registry löst auf und der Treffer (Typ, Name, Identifier) wird
  **vor** dem Verknüpfen angezeigt; Enter übernimmt den ersten Treffer, bereits verknüpfte CIs
  sind markiert. Eine unbekannte UUID wird abgewiesen; nur wenn die Registry nicht erreichbar
  ist, lässt sich eine rohe UUID verknüpfen.
- **Detail**: Kopf mit **nur den erlaubten Übergängen** als Buttons; der Übergangsdialog fragt die
  Pflichtfelder des Zielstatus ab; Bearbeiten mit `version`; Verlauf mit Kommentar/Arbeitsnotiz;
  CIs (Link in die Registry, „alle Datensätze zu diesem CI“), verknüpfte Datensätze (per Nummer),
  Anhänge (Upload/Download).
- **Prozesskatalog**: Lesesicht auf alle Typen, Status und Übergänge; „neu laden“ für Admins.

## Gateway-Betrieb (Ausblick)

`app/logic/connectors/base.py` definiert `ConnectorAdapter` (`create`, `update`, `add_journal`,
`fetch`, `handle_webhook`) und eine Registry. Ein Typ mit `mode: gateway:bmc_helix` würde Anlegen
und Änderungen an den Connector delegieren, die lokale Kopie trägt `source`/`external_id`/
`sync_state` — dasselbe Muster wie das Write-back des Server-Managers. Der erste Connector (BMC
Helix: AR-JWT, `HPD:IncidentInterface_Create`, `CHG:ChangeInterface_Create`) folgt in Phase 3.

## Entwicklung & Tests

```bash
npm install && npm run build          # React-Bundle
ruff check entrypoint.py app tests

docker cp . lyndrix-core-dev:/tmp/itsm
docker exec -w /tmp/itsm -e PYTHONPATH=/app:/tmp/pytest-sm lyndrix-core-dev python -m pytest -q tests
```

Die Service-Tests (SQLite im Speicher) decken Nummernvergabe, Prioritätsmatrix, Katalogvalidierung,
unerlaubte Übergänge, Pflichtfelder, Wiedereröffnen, optimistisches Sperren, Journal,
Verknüpfungen in beide Richtungen, Anhänge mit Limits und den Ingest-Schlüssel ab.
