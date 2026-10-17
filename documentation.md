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
app/logic/ingest.py          # Basis der Bus-Event-Handler (Dedupe, CI-Links, Race-Behandlung, ingest_failed)
app/logic/monitoring_incidents.py  # Monitoring-Ausfälle → Incidents / Auto-Resolve
app/logic/maintenance_tasks.py     # fehlendes Wartungsfenster → Task
app/logic/texts.py           # generierte Texte aus locales/itsm.<locale>.json (Abschnitt auto)
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

Felder, die das Plugin selbst befüllt (Abschnitte *Automatische Incidents* und *Tasks aus
Wartungs-Ereignissen*): `incident` hat den Lösungscode `auto_recovered` und die Custom Fields
`monitor_rule` (Regel als JSON), `outage_id`, `ci_kind`; `task` hat die Kategorie `maintenance`
und das Custom Field `source_ref`. Ein eigener Katalog (`catalog_dir`) sollte sie behalten —
fehlt `auto_recovered`, hinterlässt das Auto-Resolve nur einen Kommentar.

**Hinweis zu Kategorien:** eine leere `categories`-Liste erlaubt jede Kategorie; sobald die
Liste Einträge hat (seit 0.3.0 bei `task`), sind nur noch diese IDs gültig — ein Datensatz ohne
Kategorie bleibt erlaubt.

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
landen auf `itsm:ingest_failed`. Darüber wird später die IaC-Change-Brücke (Prod-Deploy →
Change-Freigabe) angebunden.

Ereignisse anderer Plugins aus dem Wartungsfenster-Vertrag (Core `docs/plugins.md` →
*Maintenance windows*), die das Plugin **ohne** Request/Reply in Datensätze verwandelt:

| Topic | Emitter | Wirkung |
|---|---|---|
| `monitoring:outage_persisted` | Monitoring, einmal je Ausfall | Incident anlegen (siehe *Automatische Incidents*) |
| `monitoring:outage_recovered` | Monitoring | Incident automatisch lösen oder kommentieren |
| `maintenance:window_missing` | IaC-Orchestrator | Task „Wartungsfenster definieren“ (siehe *Tasks aus Wartungs-Ereignissen*) |

Ungültige Payloads (fehlende Pflichtfelder, `ci_id` ohne 36 Zeichen) werden auf
`itsm:ingest_failed {topic, error}` gemeldet, nie als Exception. Kommt ein Ereignis während des
Boots an, bevor die Tabellen bereit sind, wartet der Handler bis zu 30 s.

### Emittiert

`itsm:record_created {record}`, `itsm:record_updated {record, changes}`,
`itsm:state_changed {record, from_state, to_state}`, `itsm:journal_added {record_uuid, number,
entry}`, `itsm:ingest_failed {topic, error}`, `itsm:ci_link_result` (Antwort),
`entity_registry:register` sowie `entity_registry:lookup` (Request zur CI-Auflösung, Antwort auf
`entity_registry:lookup_result`).

### Benachrichtigungen

Endpunkte `record_created` (nur automatisch angelegte P1/P2-Incidents, Payload mit `link` auf den
Datensatz), `record_assigned`, `record_state_changed`, `record_commented` werden über den
Core-Notification-Router ausgeliefert (Benutzer steuern Toast/Verlauf/externe Kanäle in den
Einstellungen). Manuell angelegte Datensätze erzeugen beim Anlegen keine Meldung.

## Automatische Incidents aus dem Monitoring (ab 0.3.0)

Das Monitoring-Plugin meldet einen Ausfall **einmal**, wenn ein Configuration Item außerhalb
eines Wartungsfensters länger als die `offline_minutes` seiner Incident-Regel nicht erreichbar
ist (`monitoring:outage_persisted`), und einmal, wenn der Monitor wieder antwortet
(`monitoring:outage_recovered`). Das ITSM-Plugin braucht dafür keine Konfiguration — die Regel
(`offline_minutes`, `severity`, `auto_resolve`) kommt aus `service.yml` bzw. den
`incident_defaults` des iac-controllers und reist im Ereignis mit.

**Payload `outage_persisted`:** `{outage_id, ci_id, kind, name, host_ci_id?, monitor_id, since,
persisted_at, duration_s, rule: {offline_minutes, severity, auto_resolve}, error?}`;
`outage_id = "<ci_id>:<since>"` ist deterministisch und wird als `external_id` gespeichert.

So entsteht der Incident:

| Feld | Wert |
|---|---|
| `record_type` / `subtype` / `category` | `incident` / `outage` / `monitoring` |
| `source` / `external_id` | `monitoring` / `outage_id` |
| `impact` × `urgency` → Priorität | `critical → 1/1 (P1)`, `high → 1/2 (P2)`, `medium → 2/2 (P3)`, `low → 3/2 (P4)`; unbekannte Severity zählt als `high` |
| `title` | „`<name>`: nicht erreichbar seit `<since>`“ |
| `description` | Monitor, CI, Host, Regel, Dauer, letzter Fehler |
| `tags` | `auto`, `monitoring`, `<kind>` (Entity-Registry-Kind, z. B. `service`, `host`) |
| `custom_fields` | `monitor_rule` (Regel als JSON), `outage_id`, `ci_kind` |
| CI-Links | `ci_id` als `affected`, `host_ci_id` als `related` — Namen/Typen aus der Entity Registry; ohne Registry (oder wenn sie das CI noch nicht kennt) rohe UUID-Links |
| `created_by` / `requester` | `monitoring` |

**Dedupe.** (1) Je Ausfall: existiert `(monitoring, outage_id)` schon, passiert nichts — auch
wenn eine andere Cluster-Instanz den Datensatz zwischen Prüfung und Einfügen angelegt hat
(`UNIQUE(source, external_id)`, der Datensatz des Gewinners gilt). (2) Je CI: hat das CI bereits
einen **offenen** Monitoring-Incident (Status mit Kind `open`: `new`, `in_progress`, `on_hold`),
bekommt dieser eine Arbeitsnotiz „Erneuter Ausfall gemeldet …“ statt eines zweiten Datensatzes.
`resolved` zählt dabei **nicht** als offen: ein neuer Ausfall nach dem Lösen erzeugt einen
**neuen** Incident, es wird nichts wiedereröffnet (Entscheidung F).

**Recovery.** `outage_recovered {outage_id, ci_id, since, recovered_at, duration_s}` sucht den
Incident über `outage_id`:

- Regel `auto_resolve: true` (aus `custom_fields.monitor_rule`, nicht aus dem Payload) →
  Übergang nach `resolved` mit `resolution_code: auto_recovered` und generierten
  `resolution_notes`; Benachrichtigung `record_state_changed` wie bei jedem Übergang.
- `auto_resolve: false` → Kommentar „Monitor wieder erreichbar …“ (löst `record_commented` aus).
- Übergang laut Katalog nicht erlaubt (z. B. aus `on_hold`) → Kommentar, Status bleibt.
- Gleichzeitige Änderung (Versionskonflikt) → einmal neu lesen und wiederholen; hat inzwischen
  jemand gelöst oder geschlossen, passiert nichts mehr; sonst Log-Eintrag.
- Bereits `resolved`/`closed`/`cancelled` → nichts. Unbekannte `outage_id` → nichts, außer das CI
  hat einen offenen Monitoring-Incident mit Arbeitsnotiz zu diesem Ausfall (Dedupe 2) — dann
  wird auch das Ende dort notiert.

**Benachrichtigung.** Automatisch angelegte Incidents mit P1/P2 erzeugen `record_created`
(Severity `warning`, Payload-`link` `/apps/lyndrix-plugin-itsm/itsm?record=<uuid>`).

**Texte.** Titel, Beschreibung, Notizen und Lösungstext kommen aus `locales/itsm.<locale>.json`,
Abschnitt `auto`, in der Sprache der Einstellung `auto_text_locale` (Standard `de`, Fallback
Englisch). Zeitstempel stehen in UTC.

## Tasks aus Wartungs-Ereignissen (ab 0.3.0)

Hat ein Service `deploy_policy: window`, liegt aber im Planungshorizont (Standard 30 Tage) kein
Wartungsfenster, deployt der IaC-Orchestrator sofort (mit Warnung) und sendet
`maintenance:window_missing {event_id, ci_id, kind, name, host_ci_id?, site, stage,
horizon_days, job_id, source, detected_at, reason}` (Entscheidung H). Daraus entsteht ein
**Task**, damit die Lücke in der SSoT geschlossen wird:

| Feld | Wert |
|---|---|
| `title` | „Wartungsfenster definieren: `<name>`“ |
| `description` | Service, Site/Stage, Horizont, Job, Zeitpunkt, Grund und die Anleitung (Fenster in `service.yml` → `maintenance.windows` oder im Controller → Host-Fenster / `stage_vars.maintenance_default`) |
| Priorität | P4 (`impact 3`, `urgency 2`) |
| `category` / `assignment_group` | `maintenance` / `infra` |
| `tags` | `auto`, `maintenance`, `<kind>` |
| `custom_fields` | `source_ref` = `event_id`, `due` = `detected_at` + 14 Tage |
| `source` / `external_id` | `maintenance` / `event_id` (`window_missing:<ci_id>:<job_id>`) |
| CI-Links | Service `affected`, Host `related` |

Dedupe wie bei den Incidents: je Ereignis über `(maintenance, event_id)` und je CI — ein offener
Task (`open`, `in_progress`) bekommt die Arbeitsnotiz „Erneuter Deploy ohne Wartungsfenster: Job
… “, ein `done`/`cancelled` Task wird nicht wiedereröffnet (der nächste Deploy ohne Fenster legt
einen neuen an). Keine Benachrichtigung; der Task erscheint in der Liste mit dem Tag `auto`
(Filter „nur automatische“).

## Einstellungen (Plugin-Settings-Dialog, in Vault gespeichert)

| Schlüssel | Standard | Bedeutung |
|---|---|---|
| `attachment_dir` | `/data/storage/itsm/attachments` | Ablageort für Anhänge |
| `max_attachment_mb` | `25` | Größenlimit je Datei |
| `catalog_dir` | leer (= mitgelieferter Katalog) | eigenes Verzeichnis mit `record_types/*.yml` — **ersetzt** den mitgelieferten Satz |
| `auto_text_locale` | `de` | Sprache (`de`/`en`) der Texte, die das Plugin selbst schreibt (Incidents aus dem Monitoring, Tasks aus Wartungs-Ereignissen) |

Die Einstellungen werden nach `vault:ready_for_data` und bei jedem `plugin:settings_changed`
(Core ≥ 1.5.1, wird an alle Instanzen verteilt) angewendet — Speichern im Settings-Dialog wirkt
also sofort auf jeder Instanz. Ist `catalog_dir` gesetzt, aber auf dieser Instanz nicht vorhanden,
bleibt der zuletzt geladene Katalog aktiv und `health()` meldet `degraded` (`catalog_dir_missing`).

## Cluster-Betrieb (ab 0.2.0)

Bei mehreren Core-Instanzen (`LYNDRIX_CLUSTER_MODE=cluster`) laufen alle Routen auf jeder Instanz
gegen die gemeinsame MariaDB.

- **Anhänge brauchen geteilten Speicher.** `attachment_dir` muss auf **allen** Instanzen dasselbe
  Verzeichnis sein (RWX-Volume bzw. NFS-Mount, in k3s z. B. `/data/storage` als RWX-PVC). Sonst
  fehlt eine über Instanz A hochgeladene Datei auf Instanz B (Download → 410). Ein Docker-Host-
  Runner mit eigenem `/data/storage` teilt das Verzeichnis **nicht**.
- **Erkennung:** Jede Instanz schreibt `<attachment_dir>/.cluster/<instance_id>`. Sieht eine Instanz
  die Markierung mindestens einer anderen lebenden Instanz, gilt das Verzeichnis als geteilt.
  Sind andere Instanzen aktiv, aber keine ihrer Markierungen sichtbar, melden `health()` und der
  Runtime-Status `degraded` (`attachment_dir_not_shared`, Banner, nicht blockierend). Ohne
  lebende Nachbarn bleibt der Zustand „unbekannt“ (kein `degraded`). Die Prüfung wird 60 s gecacht.
- **Pfade:** `storage_path` wird seit 0.2.0 **relativ** zu `attachment_dir` gespeichert
  (`<record>/<attachment>`); absolute Pfade älterer Datensätze werden weiterhin gelesen.
- **Nummernvergabe** läuft in einer eigenen kurzen READ-COMMITTED-Transaktion
  (`UPDATE … SET last_value = last_value + 1`); das Anlegen wird bei MariaDB 1020/1205/1213
  wiederholt. Nummern sind eindeutig, aber nicht lückenlos.
- **Optimistisches Sperren** über ein bedingtes `UPDATE … WHERE uuid = :u AND version = :v`.
  Ein verlorenes Rennen (oder 1020/1205/1213) ergibt `VersionConflict` → HTTP 409; ohne
  mitgeschickte `version` wird einmal mit frischen Daten wiederholt.
- **Katalog:** `POST /catalog/reload` sendet `itsm:catalog_reloaded` mit `cluster=True`; die anderen
  Instanzen laden ebenfalls neu.
- **Bootstrap:** Tabellen entstehen unter `db_instance.advisory_lock("lyndrix_bootstrap")` im
  Thread-Pool; „existiert bereits“ (1050/1060/1061) gilt als Erfolg, Fehlschläge werden wiederholt.
- **`health()`** zählt nur offene Datensätze; Details je Typ/Priorität liefert `GET /stats`.

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

- **Liste**: Suche, Filter Typ/Status/Priorität, „geschlossene einblenden“, „nur automatische“
  (Tag `auto`: vom Plugin angelegte Incidents und Tasks), CI-Filter-Chip; Tags als Chips, `auto`
  hervorgehoben (auch im Detailkopf).
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
docker exec -w /tmp/itsm -e PYTHONPATH=/app:/tmp/pytest-sm:/tmp/itsm \
  -e LYNDRIX_CLUSTER_MODE=single -e LYNDRIX_COORDINATOR=local \
  lyndrix-core-dev python -m pytest -q tests
```

Die Service-Tests (SQLite im Speicher, Fixture `svc` in `tests/conftest.py`, Doubles in
`tests/fakes.py`) decken Nummernvergabe, Prioritätsmatrix, Katalogvalidierung, unerlaubte
Übergänge, Pflichtfelder, Wiedereröffnen, optimistisches Sperren, Journal, Verknüpfungen in beide
Richtungen, Anhänge mit Limits und den Ingest-Schlüssel ab. `test_monitoring_incidents.py` und
`test_maintenance_tasks.py` spielen die Bus-Ereignisse des Wartungsfenster-Vertrags gegen den
echten Service durch (Prioritätsmapping, beide Dedupe-Stufen, Auto-Resolve inkl. `on_hold`- und
Versionskonflikt-Fällen, Registry nicht erreichbar, verlorenes Einfüge-Rennen, neuer Incident
nach `resolved`, Task-Anlage/-Notiz/-Neuanlage).
