# HA Config Sync

Home Assistant App Repository containing two deliberately separate Apps for synchronizing selected Home Assistant configuration through GitHub.

The application code lives here. The exported home data remains in the separate [`BartoszWu/home-assistant-config`](https://github.com/BartoszWu/home-assistant-config) repository.

Export also writes a sanitized current-state snapshot for entities provided by
the official Home Connect and LG ThinQ integrations. The snapshot contains the
current state, availability and a small allowlist of dashboard-relevant
metadata. It never contains history, raw attributes, credentials,
private URLs, IP addresses, MAC addresses or external device identifiers. The
current state's `last_changed` and `last_updated` timestamps and Home Assistant
registry `device_id` are included to make each record self-describing.

The runtime file is an LLM-facing API contract. Every record uses
`record_type: entity_state`, `source: home_assistant`, an explicit `domain`,
`enabled`, `status` and `state`. The status vocabulary is closed to
`available`, `unavailable`, `unknown` and `disabled`; disabled entities always
have `state: null`. The file-level `snapshot_semantics` field states that the
timestamps describe one point-in-time export rather than history.

## Architecture

| App | Runtime | GitHub access | Home Assistant access |
| --- | --- | --- | --- |
| **HA Config Sync — Export** | One-shot, no Web UI | Write deploy key for `home-assistant-config` | Reads selected data through the Supervisor-backed HA API, reads LIVE dashboard provenance from `/homeassistant/.config-sync/live-dashboards.json`, and writes sanitized output to its own app data directory |
| **HA Config Sync — Import** | Long-running Ingress Web UI | Separate read-only deploy key for `home-assistant-config` | Reads dashboards through the HA API and allowlisted managed files from `/homeassistant`; writes only after explicit UI approval, conflict checking and read-back verification |

The split keeps the GitHub write credential out of the web-facing Import App. Neither App receives Docker access, host networking, or full access. Import 0.5+ mounts writable `homeassistant_config` at `/homeassistant` for Managed Files only, constrained by `import/managed_files.yaml`, path guards, and `import/apparmor.txt`.

After at least one dashboard is successfully applied and verified, Import fires the Home Assistant event `ha_config_sync_import_applied`. The existing `Sync HA config to GitHub` automation listens for that event and starts Export. Import does not know the installed Export App ID and never receives its GitHub write key. A failed or blocked Apply does not request an Export.

## Everyday dashboard workflow

```text
edit dashboard JSON → commit and push → Import → review → Apply
→ automatic Export → GitHub reflects the verified Home Assistant state
```

The Export request happens immediately after a successful Apply. The same Home Assistant automation may still run Export on its normal schedule and after Home Assistant starts. Export never copies a feature-deployed LIVE dashboard onto `main`; inventory and other complete sections still publish.

## Import source revision (0.6)

Import defaults to `main`. The Ingress **Source** control can pin a remote branch or an explicit 40-character commit SHA for the current review only. That choice is not saved as a new default.

Preview, dashboard JSON, managed files and frontend staging all come from the same resolved commit. Apply is refused if a branch tip moves after the review (`SOURCE UPDATED — REFRESH REVIEW`). Import never merges branches or pushes to Git.

## Canonical vs feature deployments (Import 0.7 / Export 0.8)

Feature-branch Apply is a temporary LIVE test. Import records per-dashboard provenance after a verified Apply:

- Apply from `main` → `CANONICAL MAIN`
- Apply from a feature branch or explicit SHA → `NON-CANONICAL`

Shared provenance lives at `/homeassistant/.config-sync/live-dashboards.json` (not under `www` / `/local`). Import also keeps `/data/dashboard-provenance.json`. Export may cache a copy in `/data`, but that cache can only restrict dashboard writes — a stale cached `CANONICAL MAIN` record never authorizes a write to `main`.

Git cannot mark a feature branch canonical. While a dashboard is non-canonical, Export skips that dashboard's `main` sync and does not move `state/dashboard-bases.json` for it. Other canonical dashboards still sync. After the guard has been initialized, missing or corrupt shared provenance fail-closes all dashboard desired-state sync. Installations that have never written provenance remain legacy and keep the previous canonical-main Export behaviour.

Non-canonical Apply is not SUCCESS until provenance persists and is read back. If persist fails, Import rolls LIVE back; if rollback fails, it arms the fail-closed guard.

After the feature is merged to `main` outside Import, open Import on `main` and Refresh. If LIVE hash equals Git `main`, Import marks that dashboard `CANONICAL MAIN` without rewriting the identical dashboard. Manual HA edits during a feature deployment stay conflicts in Import; Export will not publish them to `main`.

If a later Git change lands before Export has moved `state/dashboard-bases.json`, Import still treats the last canonical Apply hash as BASE whenever that hash still matches Git or LIVE. A Git-only follow-up stays `READY TO APPLY` instead of a false `CONFLICT`. Export's BASE remains authoritative when it already matches Git or LIVE.

## Managed files and frontend modules

Exact allowlisted files stay in `import/managed_files.yaml`: `packages/temperatura.yaml`, `packages/diagnostyka.yaml`, `www/temperature-card.mjs`, and `custom_templates/temperatura.jinja` (written to HA `/config/custom_templates/temperatura.jinja`). In addition, Import discovers `.js` and `.mjs` files under `www/dashboard/` in the pinned commit. New dashboard modules belong in that prefix; `www/temperature-card.mjs` remains on its exact V1 path. There is no `custom_templates/*` wildcard. Delete is not supported. Live files that are absent from the selected Git revision are left in place.

Frontend preview staging writes
`/homeassistant/www/.config-sync-preview/<commit>/…` while preserving directory structure so relative ES module imports resolve. Canonical `www/` paths change only on Apply.

All allowlisted `frontend_module` files, including modules discovered under
`www/dashboard/`, use `cache_bust: content_hash`. After a successful Managed
File deploy, Import updates the matching Lovelace resource URL to include a
content-derived version query (`/local/…?v=<sha256-prefix>`). This prevents
stale browser caches without direct `.storage` access. Import lists and updates
resources through the Home Assistant WebSocket API. A cache-bust warning does
not undo the file write. An already open dashboard may still require a normal
page refresh.

Lovelace resource desired state is derived for every allowlisted
`frontend_module` that has a `resource_url`. This includes every top-level
`.js` and `.mjs` dashboard module discovered directly in the App-owned
`www/dashboard/` prefix, so adding a dashboard module there does not require
another hardcoded resource entry. Nested modules remain managed dependencies;
Import does not register them as standalone Lovelace resources.
Import does not infer resources from `custom:*` cards or paths outside the
allowlist. Review is read-only (`lovelace/resources/list`). Missing →
`READY TO APPLY — CREATE RESOURCE`;
same URL + `module` → `OK`; same URL, other type → `CONFLICT` (no automatic
change). Apply creates only missing allowlisted resources via
`lovelace/resources/create`, is idempotent, and deletes a resource only if this
Apply created it and a later step fails.

Apply order:

```text
Managed File
→ create missing Resource
→ create missing Dashboard
→ save dashboard config
→ provenance
→ verification
```

For a dashboard JSON that exists in Git but is not registered in Home
Assistant, Import 0.10 labels it `READY TO APPLY — CREATE DASHBOARD`. Apply
creates the Lovelace dashboard through `lovelace/dashboards/create` (title and
icon from the first Git view; sidebar visible, not admin-only), then saves the
Git configuration. Home Assistant requires a hyphen in the URL path; a Git
file whose stem has no hyphen stays a conflict. A dashboard that is already
registered as a semantically empty shell still uses
`READY TO APPLY — NEW DASHBOARD`. Both states require the exact preview hashes
from the review immediately before Apply. After the verified bootstrap Apply,
the normal automatic Export creates the base hash. A non-empty registered
dashboard without a base remains blocked as a conflict. If create or save
fails after Import registered the dashboard, Import deletes that dashboard as
rollback; it does not delete dashboards it did not create in that Apply.

If Apply succeeds but the automatic Export request fails, GitHub and HA match
while the base is still absent. Import reports
`IN SYNC — BASE NOT INITIALIZED` and provides a button to request Export again.

## Import review UI

The opening screen shows application availability and configuration changes in
one place. The initial page returns before Git/HA reads, with loading placeholders.
After one Git checkout, application status and configuration comparisons run in
parallel and appear independently through an Ingress-protected event stream. Each
review stays on that one immutable SHA; unfinished or failed configuration checks
never expose selectable Apply items. An interrupted stream blocks selection and
offers a fresh check. The all-current summary appears only after both sections
finish. Without JavaScript, **otwórz pełny przegląd** opens the complete synchronous
review. **Aplikacje** compares current backend and loaded HA integration
versions with the approved version set in the reviewed `main` commit. These
are read-only status calls with short timeouts; opening Import does not prepare
a deployment, download a package or approve an update. Unavailable versions
remain unknown. Saved pending/failed jobs retain their recovery link; a previous
successful job never substitutes for current version checks. Pending plans show
their own pinned target versions, labelled **przygotowane**, alongside any
other available versions from the reviewed configuration. A new version set
remains visible without replacing or approving the saved job. If the reviewed
configuration is unavailable, that check is explicitly marked unknown. Application
updates still require their separate review and explicit approval. Feature
branches and explicit commit reviews do not offer application updates from a
different source.

**Konfiguracja** groups dashboards, cards, sensors/rules and templates by their
subject, including the affected dashboard tabs when JSON identifies them. Files,
content fingerprints, original statuses and LIVE provenance
are available in **Szczegóły**. There is no rendered content diff, line count or
duplicate file index. Unchanged items and source controls are collapsed. Partial
read failures never produce an all-current summary. A dashboard with an invalid
JSON container structure is marked as an individual error and cannot be applied;
other valid dashboard items remain available for review.

**Zaznacz gotowe zmiany** selects eligible dashboards, managed files and Lovelace
resources together. Items with security warnings require individual selection
after GitHub review. **Wyczyść wybór** clears the selection. The Apply action is
labelled **Wgraj zaznaczone** and remains explicit; application updates are not
part of this selection. The action bar becomes sticky only after a selection.

Import pins the reviewed commit and checks both the Git desired hash and current
HA state again before writing, then verifies the result. The item list and
individual selection are available without JavaScript; JavaScript adds bulk
selection and progress feedback. The shared visual style supports light/dark
mode, keyboard focus, reduced motion and narrow screens without remote fonts.

Example layouts use synthetic data: [desktop](docs/import-ui/desktop.png),
[mobile dark mode](docs/import-ui/mobile-dark.png),
[previous screen](docs/import-ui/before.png).

## Repository layout

```text
.
├── repository.yaml
├── export/
│   ├── config.yaml
│   ├── Dockerfile
│   ├── run.sh
│   ├── runtime_inventory.py
│   └── source files
├── import/
│   ├── config.yaml
│   ├── Dockerfile
│   ├── run.sh
│   └── app.py
└── scripts/
    ├── bump
    ├── check
    └── release
```

## Credentials

Credentials are runtime-only files in Home Assistant's generated `addon_configs` directories. They are never part of this repository.

- Export expects `/export/ssh/github_ed25519` and `/export/ssh/known_hosts_443`.
- Import expects `/review/ssh/github_ed25519` and `/review/ssh/known_hosts_443`.
- Export uses the write deploy key; Import uses a different read-only deploy key.

## Development workflow

Edit on the Mac, validate, bump one App version, validate again, commit and push, then use **App Store → Check for updates → Update** in Home Assistant.

```bash
./scripts/check
./scripts/bump export patch
./scripts/check
git diff
git status
git commit -am "Release HA Config Sync — Export 0.5.1"
git push
```

For a transparent combined local preparation step, use:

```bash
./scripts/release import patch
```

`scripts/release` does not commit or push. Production updates are delivered through the Home Assistant App Repository; there is intentionally no server-side `git pull /addons` alias.

## Add to Home Assistant

Add this URL as a custom App Repository:

```text
https://github.com/BartoszWu/home-assistant-config-sync
```

The Apps are currently built locally by Home Assistant. Pre-built GHCR images and GitHub Actions can be added later.

## Stage 1 inventory and safety contract

`inventory/entities.json` uses `schema_version: 3`. It retains the `entities`
array and adds canonical `devices`, `areas`, and `floors` arrays in the same
snapshot, so relations and completeness share one export boundary. ENTITIES.md
and DEVICES.md (including area/floor tables) are rendered from that JSON.
No separate registry JSON files or second exporter are needed.

Entity metadata joins the registry with the same Export's `get_states` response:
class, state class, unit, friendly name, disabled/hidden flags, device/area/floor
relations, and allowlisted climate capabilities. Climate temperatures and action
values are omitted; only known runtime attribute names are recorded. Missing
metadata can retain the last observed value only within schema v3 and the exact
entity ID. Unsupported schema versions are never guessed or migrated implicitly.

Identity v1 preserves exact safe entity IDs and internal HA device registry IDs.
It never uses integration unique IDs, serials, connections or a new secret key.
Future entity renames mean removed + added; name similarity is not identity.
`zone.home` alone has an explicit runtime-only existence policy, with no state
or location attributes. This does not authorize arbitrary runtime entities.

The existing sanitizers share `export/security.py`; `import/security.py` is its
identical mirror because App Docker build contexts are independent. After a
policy edit, copy the source to the mirror; `scripts/check` rejects drift.
Registry/state metadata remains an explicit field allowlist. Configuration
snapshots retain their existing schema scope and undergo recursive security
validation. Credential-like text, normalized secret keys, serial/user IDs,
MAC variants, IPv4/IPv6 and credential-bearing URLs are rejected or redacted.
Unknown metadata fields are not exported.

Apply forms carry both the canonical SHA-256 of HA current and Git desired.
POST refreshes Git and compares both approved contents, then re-reads HA before
saving that same in-memory desired object. Any mismatch requires a new preview.
HA does not provide an atomic compare-and-swap here; read-back verification
remains required after saving.

`inventory/dashboards.json` versions the dashboard manifest separately from the
Import's `dashboards/*.json` inputs. It includes storage panel metadata, views,
custom card types, safe local resources, Git/HA comparison and scope/security
exclusions. Built-in dashboards are intentionally excluded from config export.

`inventory/export-status.json` records timestamp, source and section read status.
Object statuses distinguish `success`, `intentionally_excluded`, `unsupported`,
`read_error`, and `security_excluded`. Inspect `complete` and object statuses;
a successful list call alone does not establish full coverage. Read failures
preserve previous snapshots and mark them as retained. Unsupported config reads
(including integration scenes without Config API snapshots) do not imply deletion.
An explicit security exclusion removes the corresponding unsafe managed snapshot.
No dashboard is automatically deleted. A failed section cannot publish stale
files left in the staging directory.

Runtime remains the existing sanitized Home Connect/LG ThinQ `states.json` and
STATES.md. Expanded runtime cache and transport across computers are deferred:
a future cache should be outside Git, timestamped with source/completeness, use
per-domain allowlists and refresh through authenticated HA access on each machine.
Do not treat copying a cache or Git pull as live acquisition. No broad state
snapshot, history, secret or additional runtime cache is introduced in this stage.

## Structural analysis (Export 0.7.0)

The existing Export now generates semantic inventory changes, a static dependency
report, explainable InfluxDB review candidates, and a small agent summary. User
InfluxDB decisions live in one private JSON policy; no integration is configured.
See [Stage 2 contract and offline commands](STAGE2.md) for completeness, baseline,
revision-aware notifications, candidate review, and runtime-only no-commit behavior.

## Import projektów z backendem na serwerze

Import 0.12.0 udostępnia **Aktualizacje projektów → JDG → Sprawdź aktualizację →
Importuj JDG**. Wspólny moduł przygotowuje niezmienny plan, wykonuje backup HA,
aktualizuje backend i integrację, restartuje Core gdy potrzeba, stosuje zestaw
dashboardów i sprawdza działanie. Przyciski na ekranie projektu pokazują od razu
stan oczekiwania, również podczas powrotu do Importu i anulowania przygotowanej
aktualizacji. Anulowanie potwierdza komunikat; nie uruchamia importu. Link i formularze
działają także bez JavaScriptu. JDG jest pierwszym profilem; ten sam moduł
jest testowany także z drugim syntetycznym projektem i inną paczką integracji.

`import/project_profiles.py` określa obsługiwane projekty: domenę, dokładne pliki
integracji, dashboardy, moduły JS, zasoby i minimalne wersje kontraktu.
Dodanie projektu wymaga przeglądu profilu i uprawnień App oraz adaptera wykonawcy
po stronie jego backendu. Git wybiera wersje wyłącznie już obsługiwanych profili;
nie może dodawać poleceń, repozytoriów ani uprawnień instalacji. Generator
`project_apparmor.py` wyprowadza dokładne katalogi instalatora z tego rejestru.
Obecnie produkcyjny rejestr zawiera tylko JDG; zwykłe managed files nadal
odrzucają wszystkie `custom_components`, pozostałe integracje i `.storage`
pozostają zablokowane. Pierwsza instalacja backendu/wpisu integracji jest
bootstrapem, a nie operacją tego modułu aktualizacji.

Wykonawca działa w dedykowanym gościu backendu i posiada własny ograniczony
GitHub token **Contents: read-only** do prywatnego repo produktu. Źródła integracji
JDG oraz ZIP i SHA-256 są wydawane w tym samym prywatnym repo co backend.
Import pobiera paczkę przez uwierzytelniony lokalny `GET /integration?version=...`;
potwierdza jej powiązanie z SHA wybranego backendu, wersję, checksum i allowlistę
plików. Import nie potrzebuje klucza do prywatnych Releases i nie pobiera kodu
z dawnego publicznego repo integracji. Wykonawca nie przyjmuje dowolnych poleceń,
ścieżek, repozytoriów ani URL-i. Każdy projekt może używać osobnego wykonawcy,
klucza i sposobu uruchamiania; wspólny jest interface aktualizacji.

Jednorazowo zainstaluj wykonawcę (`deploy/install-deploy-agent.sh` w repo produktu),
skonfiguruj jego repo-scoped klucz GitHub oraz LAN/firewall ograniczony do HA.
Nie używaj tokenu KSeF, klucza encji ani klucza Exportu. Poza zaufanym LAN użyj
prywatnego tunelu/TLS. W lokalnym `/review` umieść `jdg-deploy-token` i `jdg.json`:

```json
{
  "backend_url": "http://<lokalny-wykonawca>:3001",
  "token_file": "/review/jdg-deploy-token"
}
```

Plik klucza jest zwykłym plikiem w tym samym katalogu; połączenie nie dopuszcza
przekierowania z bearerem. Adres i klucz pozostają poza Git. Dla kolejnego
obsługiwanego projektu lokalne pliki mają jego ID, np. `<id>.json`.
W canonical konfiguracji HA wersje wybiera `deployments/jdg.json`:

```json
{
  "schema_version": 2,
  "project": "jdg",
  "backend_version": "0.3.0",
  "integration_version": "0.7.0"
}
```

Najpierw publikujemy prywatne wydanie backendu z paczką integracji, następnie
scalamy zestaw konfiguracji. Dashboardy i moduły pochodzą z tego samego
zatwierdzonego SHA konfiguracji. Konflikty lub ostrzeżenia zatrzymują skrót;
rozwiąż je w zwykłym review. Integracja musi być już skonfigurowana i działająca.

`backup/generate` kopiuje HA z bazą do skonfigurowanych miejsc, zachowując hasło;
Import potwierdza ukończenie. Kopia pomija Apps, aby nie zatrzymać własnego
wykonawcy. JDG ma jednorazowe przejęcie zarządzania: `hacs/repositories/list`
i `remove` dla dokładnego dawnego repo/domeny, ze sprawdzeniem zachowania plików.
Nie używamy uninstall ani nie usuwamy całego HACS. Dopiero po sprawdzonej
migracji repo integracji można zmienić na prywatne i zarchiwizować.

Stan każdego projektu jest oddzielny w `/data/projects/<id>`, a trwały
`owner.json` rezerwuje HA dla jednego zatwierdzonego zadania. Pozostałe projekty
i zwykły Apply czekają, aż właściciel zakończy lub odzyska aktualizację.
Niepowodzenie zachowuje właściciela i plan; **Wznów import** kontynuuje ten sam
zestaw bez ponownego wdrożenia backendu/restartu po zakończonym etapie.
Restart App wznawia wyłącznie zatwierdzone zadanie. Plan zawiera hash profilu:
zmiana domeny/listy plików/selekcji uniemożliwia przypadkowe wznowienie według
nowych uprawnień. Dokończ zadania przed aktualizacją profili samego Importu.

Zwykły Apply i projektowy adapter dashboardów używają wspólnego `apply_plan`
bez syntetycznego requestu Flask. Zachowane są konflikty, provenance, read-back,
cache bust i rollback. Export jest żądany dopiero po końcowej kontroli backendu,
załadowanej integracji i wszystkich dashboardów. Niepełny staging, nieznany
protokół lub uszkodzony właściciel wymagają lokalnej kontroli, a nie nowego planu.
Zatwierdzony projekt zachowuje dokładny SHA zestawu również wtedy, gdy niezależny
Export przesunie `main`. Przy każdym odczycie/Apply Git potwierdza, że ten SHA
nadal jest przodkiem aktualnego `main`; usunięta historia lub obcy commit
zatrzymują zadanie. Zapis wymaga jego trwałego właściciela, zgodnych hashy
artefaktów i świeżego stanu HA. Zwykły Apply nadal wymaga niezmienionego HEAD.
Staging i poprzedni kod są przechowywane w prywatnym katalogu
`/homeassistant/.config-sync-integrations/<domain>-{stage,previous}`, poza
`custom_components`: loader HA wykrywa również ukryte katalogi z manifestem.
Przy odzyskiwaniu zatwierdzonego zadania rozpoczętego na 0.12.0 najpierw
zatrzymaj Import i przenieś jego dokładne katalogi `.domain-stage` i
`.domain-previous` do nowych ścieżek bez zmiany zawartości. Wznów to samo zadanie;
jeśli HA wcześniej wykrył kopię jako integrację, wymagany jest restart Core.

Rollback całego zestawu pozostaje świadomą operacją z backupów, bez automatycznego
przywracania baz. Schema 1 i eksperymentalny `/data/jdg` z niewydanego PR 0.11.0
nie są migrowane automatycznie: przerwane próby trzeba najpierw dokończyć/odzyskać
na poprzednim kodzie. Produkcyjne Import/Apply, restart i bootstrap wymagają
osobnego zlecenia; ta zmiana przygotowuje PR, nie wykonuje wdrożenia.

### Dziennik domu w aktualizacjach aplikacji

Profil `home_journal` aktualizuje backend/narratora w LXC, integrację
`home_journal`, `dashboard-home-journal.json` i moduł karty. Korzysta z tego
samego zatwierdzanego planu, backupu HA, sprawdzania konfliktów i trwałego
wznawiania co JDG. Nie rozszerza dostępu do innych integracji ani `.storage`.
Wersje wybiera `deployments/home_journal.json` w prywatnym repo konfiguracji;
adres i klucz wykonawcy pozostają lokalnie w `/review/home_journal.json` oraz
pliku tokena obok niego. Brak wykonawcy pokazuje „Połącz wykonawcę”.

Jednorazowe przygotowanie LXC i cykl wydań opisuje
[runbook Home Journal](https://github.com/BartoszWu/homelab/blob/main/home-journal/deploy/IMPORT.md).
Po jego przygotowaniu: **Dziennik domu → Sprawdź aktualizację → Importuj
Dziennik domu**. Przy pierwszej migracji integracji 0.1.0 jej wersja LIVE może
być nieznana; review nadal pozwala zainstalować nowy kod z wymaganym restartem.
Nie wgrywaj osobno samej karty historii przed zmianą backendu/integracji.
