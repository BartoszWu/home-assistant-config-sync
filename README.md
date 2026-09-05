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
| **HA Config Sync — Export** | One-shot, no Web UI | Write deploy key for `home-assistant-config` | Reads selected data through the Supervisor-backed HA API and writes sanitized output to its own app data directory |
| **HA Config Sync — Import** | Long-running Ingress Web UI | Separate read-only deploy key for `home-assistant-config` | Reads dashboards through the HA API; writes a dashboard only after it is explicitly selected and submitted in the UI, followed by conflict checking and read-back verification |

The split keeps the GitHub write credential out of the web-facing Import App. Neither App receives Docker access, host networking, full access, or direct access to `/config/.storage`.

After at least one dashboard is successfully applied and verified, Import fires the Home Assistant event `ha_config_sync_import_applied`. The existing `Sync HA config to GitHub` automation listens for that event and starts Export. Import does not know the installed Export App ID and never receives its GitHub write key. A failed or blocked Apply does not request an Export.

## Everyday dashboard workflow

```text
edit dashboard JSON → commit and push → Import → review → Apply
→ automatic Export → GitHub reflects the verified Home Assistant state
```

The Export request happens immediately after a successful Apply. The same Home Assistant automation may still run Export on its normal schedule and after Home Assistant starts.

For a new dashboard, first create its empty UI-controlled shell in Home
Assistant so the URL path and sidebar metadata exist. Import recognizes that
semantically empty shell without requiring an exported base, labels it
`READY TO APPLY — NEW DASHBOARD`, and requires the exact HA configuration hash
from the review immediately before Apply. After the verified bootstrap Apply,
the normal automatic Export creates the base hash. A non-empty dashboard
without a base remains blocked as a conflict.

If Apply succeeds but the automatic Export request fails, GitHub and HA match
while the base is still absent. Import reports
`IN SYNC — BASE NOT INITIALIZED` and provides a button to request Export again.

## Visual dashboard preview (Import)

The existing side-by-side JSON diff is preserved under the **YAML diff** tab
(the label does not convert the exported JSON into YAML). **Visual** is the
preferred dashboard tab when JavaScript is available. Click **Generate visual
preview** to load desktop (1440×900) and mobile (390×844), Before/After.
Apply and its fresh conflict check/read-back verification are unchanged.
Cancel clears the selection and disposes previews; nothing needs rolling back.

The renderer loads the installed Home Assistant frontend in same-origin frames
using the browser's existing HA session, then supplies the current/desired
review configurations to HA's own `hui-root` component. After exists only in
frame memory. **No temporary dashboard is created, and no production dashboard
is overwritten.** The renderer does not use the backend Supervisor credential,
extract browser tokens, save screenshots, or implement its own cards. Frames
are non-interactive and the preview's HA facade rejects writes/unknown commands;
save, delete and edit callbacks are disabled. The original HA frontend remains
hidden inside each frame to retain its session while the preview is mounted.

This is an experimental frontend adapter, **not a supported public HA preview
API**. The integration seam follows
[`ha-panel-lovelace`](https://github.com/home-assistant/frontend/blob/dev/src/panels/lovelace/ha-panel-lovelace.ts)
and [`hui-root`](https://github.com/home-assistant/frontend/blob/dev/src/panels/lovelace/hui-root.ts).
Missing authentication, blocked frames, incompatible frontend versions, errors
and timeouts show “Visual preview unavailable. YAML diff is still available.”
They never disable Apply. If JavaScript itself fails to load, the original diff
is visible without enhancement.

MVP limits:

- These are live frontend renders, **not PNG screenshots**. Only the first view
  is shown, with a fixed-height viewport; lower content is cropped.
- Before uses the current HA config read for the review; After uses the exact
  GitHub config in that same review. Entity states are sampled separately while
  rendering, not frozen or synchronized across the four frames.
- Requires the standard same-origin HA web session and root-relative dashboard
  URLs. Companion-app auth, path-prefix proxies and cross-origin Ingress may not
  support this mechanism. There is no token-passing workaround.
- Targets explicit native-card dashboards. Custom cards in card slots (including
  sections, stacks and conditional cards) are replaced **only in deep-copied
  preview configurations** with inert native button placeholders. They show the
  omitted type, never the custom content or actions. A partial-preview notice
  reports Before/After counts. Basic section dimensions and visibility conditions
  are retained; placeholder heights/layout are approximate. Original JSON,
  YAML diff, hashes and Apply payloads are never rewritten.
- Custom views, badges, entity rows and strategy-generated configurations still
  fail closed: a card placeholder would be invalid in those slots.
  Additional card data uses a read-command allowlist;
  unsupported data requests may produce a card error. Themes come from the session.
- The read allowlist includes HA's `lovelace/info` bootstrap query. Import 0.4.0
  omitted it, so the read-only facade rejected native `hui-root` initialization.
  Import 0.4.2 covers this seam with unit/browser regression tests and uses a
  content hash in the module URL so HA/browser caches cannot retain the old adapter.
- Export currently enumerates registered storage dashboards. Import still only
  reviews direct `dashboards/*.json`; this feature does not add YAML-mode writes
  or imports of automations/scripts/scenes.
- At most one dashboard's four frames are retained. Tab switching reuses them.
  Cancel, another dashboard preview, Apply or leaving the page destroys them.
  A new explicit generation after cleanup starts a new preview session.
- There are no persisted temporary resources to orphan after a browser crash.
  Render failures also remove frames and disconnect resize observers.

Validation: `scripts/check` runs Python tests and, when Node.js is available,
the dependency-free JS tests. Install Import's Flask/websocket-client runtime
dependencies to include the HTTP integration tests (otherwise they report skips).
An optional browser harness uses a separately installed Playwright:

```bash
PLAYWRIGHT_MODULE=/absolute/path/to/playwright \
IMPORT_TEST_PYTHON=/absolute/path/to/venv/bin/python \
node --test tests/browser_visual_preview.mjs
```

`CHROMIUM_EXECUTABLE` may select an existing Chrome binary. The browser harness
uses synthetic data and a mock of the HA component boundary. It does **not**
establish real-HA compatibility. Before production use, smoke-test the installed
HA version: all four renders, YAML diff, Cancel, frame cleanup and a known-safe
Apply with read-back verification. Confirm no preview dashboards are registered.

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
