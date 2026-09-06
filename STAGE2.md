# Stage 2: structural analysis in the existing Export

Export 0.7.0 extends the existing post-sanitization publication phase. Import
remains unchanged. No HA → InfluxDB configuration, connection settings, Grafana
changes, backup changes or production Apply are part of this release.

## Contract and revisions

`inventory/changes.json` (schema 1) compares canonical inventory schema 3 by exact
entity_id, internal device_id, area_id and floor_id. It compares all structural
metadata fields, membership and relationships. Top-level runtime state/value,
availability and timestamps are excluded. Canonical v3 already excludes runtime
attribute values. It never guesses renames; entity rename is removed + added.
Last-observed metadata retention from Stage 1 still applies: absence on an
offline state is not proof that a capability was removed.

Both snapshots must be complete for each compared section. Incomplete sections
produce no changes, including removals. Baseline schema/identity mismatch reports
`migration_required`, without inferred changes or automatic policy transfer.
Missing baseline creates an initial baseline, not hundreds of added entities.
The last complete baseline and entity lifetime ledger live in the generated
`inventory/analysis-state.json`; incomplete exports cannot overwrite them.
The section diff can report complete sections while candidate/lifecycle changes
wait for all inventory sections to be complete.

Revisions are SHA-256 of stable content, **not Git commit IDs or per-run clocks**.
The diff carries baseline/current inventory revisions and a changes revision.
`inventory/summary.json` has a current structural export-content revision,
including static source configurations and section completeness. Logs identify
individual executions; Git HEAD identifies the published repository revision.

The latest significant diff survives no-change/runtime-only exports. The raw
summary's `structural_changes` therefore means "a retained structural diff
exists", not "this invocation detected a change". The per-invocation comparison
is printed to App logs. The local `summary` command applies the acknowledgement
marker and returns the effective flag for this checkout. A later structural diff
replaces the previous diff; unreviewed candidate seeds remain in a backlog.

## Static dependencies

`inventory/dependencies.json` includes entity→device/area/floor/integration
edges and dashboard/automation/script usage with source filename and RFC 6901
JSON pointer (including view/section/card indexes). Only declared entity fields
(`entity`, `entity_id`, `entities`, `zone`) establish literal references.
Service/action names, event names, trigger types and prose do not.

Templates are never executed. Jinja and JavaScript produce `dynamic/unknown`;
recognized literal Jinja arguments and states.domain.object references can be
resolved, but missing references in a template are conservatively unresolved.
A static missing reference is confirmed broken only with a complete inventory
and successful source read. `zone.home` is runtime-only resolved. Stale retained
source snapshots cannot confirm broken references. Helper internals are not
available in these API snapshots, so no helper-definition graph is invented.
This is a bounded static analyzer, not a full HA/custom-card language parser.

## User policy and candidate lifecycle

`policy/influxdb.json` is the only decision source, schema 1:

```json
{
  "schema_version": 1,
  "identity_policy": "exact_entity_id_lifetime_v1",
  "decisions": []
}
```

Copy an exact `entity_ref`, `entity_id`, optional `attribute`, and
`metadata_revision` from a candidate. Add a decision (`INCLUDE`, `EXCLUDE`,
`PENDING`) and nonempty reason. Optional reviewed_at/revision record the review;
revision is a SHA-256 content revision. Attribute decisions override entity-wide
decisions, so an entity EXCLUDE plus selected attribute INCLUDE implements
"skip all other series". Policy is not an HA integration config.

INCLUDE and EXCLUDE are not proposed again. PENDING means already reviewed and
deferred; it is re-proposed only if structural metadata differs from the reviewed
metadata fingerprint. When reviewing again, copy the current metadata_revision.
If omitted on first insertion, Export records the current fingerprint once.
Export updates lifecycle metadata (`inactive`, initial metadata_revision), never
chooses or changes a decision, reason or review date.

The reference combines exact entity_id with a local inventory lifetime number.
It is **not** proof of a persistent HA registry identity. A confirmed disappearance
retains the old decision as inactive. Reappearance starts a new lifetime even
with the same name/ID; v3 has no stronger entity key to authorize transfer.
No fuzzy matching, unique_id, HMAC or new secret is used. Do not delete the
analysis-state ledger to reset notifications. Schema migrations require explicit
review rather than a silent new identity baseline.

The one-time INITIAL LONG-TERM METRICS REVIEW selects active measurement and
climate candidates plus dashboard-used derived binary activity. It does not mark
existing entities PENDING or present every registry row as new. Subsequent seeds
are additions and metadata changes; unreviewed seeds survive later exports.
Ratings are deterministic and explain domain, measurement class, state class,
unit, integration, device/area, observed attributes, usage and potential redundant
same-device measurements. Binary activity and history_stats aggregates need
human review. Numeric value types are inferred only from schema metadata; raw
live values are not scanned. Technical commands/updates/battery/diagnostic
metadata generally receive NOT_RECOMMENDED. No recommendation writes a decision.

Climate has an ENTITY mode candidate and only observed ATTRIBUTE candidates.
Fractional temperature components receive NEEDS_REVIEW; a raw integer component
must not silently substitute for the combined measurement. No series mapping is
configured. Markdown groups candidates per device and separates the bootstrap.

## Offline commands and progressive disclosure

Run with the current App Python tooling, without HA credentials:

```sh
python3 export/analyze.py analyze /path/to/private-data-repo
python3 export/analyze.py summary /path/to/private-data-repo
python3 export/analyze.py mark-surfaced /path/to/private-data-repo --expected-summary-revision HASH_FROM_SUMMARY
```

`analyze` regenerates JSON and Markdown from already-exported files; it is not a
second exporter and does not establish HA freshness. Export calls the same code
automatically. `summary` reads only the small summary and optional local marker.
`mark-surfaced` is explicit, guarded against acknowledging a changed/unseen
summary, and writes only `.agent-local/surfaced.json`, ignored in the data repo.
It does not review candidates or write a policy decision. Each checkout/session
owns its own notification state; no agent notifications are committed to Git.

Start recommendation for a later AGENTS cleanup: ensure a fresh authorized
**Export (HA → Git)**, fast-forward the data repo, read the effective small
summary, open CHANGES only for unsurfaced structural changes and
INFLUXDB-CANDIDATES only for unsurfaced candidates, briefly surface them, then
acknowledge the exact summary revision and proceed to the actual task. Full
inventory/dependencies are on demand. **Import = Git → HA preview/apply**, never
"refresh from HA". This release does not rewrite the existing AGENTS workflow.

## Publication and determinism

JSON is sorted, Markdown is rendered exclusively from its persisted JSON, and
the final validation gate checks both formats and new artifact value boundaries.
No per-run clock enters analysis. export-status observed_at records the last
published section-status transition (explicit observation_semantics); App logs
are the execution-freshness record. Appliance states/STATES are still captured
but cannot alone create a Git commit. They may accompany a substantive export.
Git staging explicitly allows the new analysis outputs and policy lifecycle;
local markers, credentials, runtime cache and backups are not staged.

| Path in data repo | Ownership | Read |
|---|---|---|
| inventory/summary.json | generated | always, through local summary command |
| inventory/changes.json; docs/CHANGES.md | generated latest structural diff | on new unsurfaced revision |
| inventory/dependencies.json; docs/DEPENDENCIES.md | generated | on demand |
| inventory/influxdb-candidates.json; docs/INFLUXDB-CANDIDATES.md | generated | unsurfaced review candidates |
| inventory/analysis-state.json | generated baseline/lifetime/backlog | generator only |
| policy/influxdb.json | human decisions, generated lifecycle | decision review / generator |
| .agent-local/surfaced.json | local CLI, gitignored | summary command only |
