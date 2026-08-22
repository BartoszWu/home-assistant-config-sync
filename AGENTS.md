# AGENTS.md

This file is the operating guide for agents working on **HA Config Sync**. Read it before changing code, metadata, versions, permissions, or Home Assistant state.

## Project purpose

This repository contains the application code for two Home Assistant Apps:

- **HA Config Sync — Export**: exports selected, sanitized Home Assistant configuration to GitHub.
- **HA Config Sync — Import**: shows GitHub-versus-HA dashboard differences in an Ingress UI and applies only explicitly approved dashboard changes.

The exported home data is intentionally stored in a different repository:

- App code: `BartoszWu/home-assistant-config-sync`
- Home data/config snapshots: `BartoszWu/home-assistant-config`

Do not merge the two repositories and do not commit generated home data here.

The preferred Mac checkout is:

```text
~/Repos/Projekty/home-assistant-workspace/home-assistant-config-sync
```

When both projects are needed, open
`~/Repos/Projekty/home-assistant-workspace` as the Codex workspace and follow
its root `AGENTS.md` for task routing. Dashboard and home-data changes belong in
the sibling `home-assistant-config` repository, not in this repository.

## Repository structure

```text
.
├── repository.yaml
├── export/
├── import/
└── scripts/
```

`repository.yaml` exists only at the repository root. Each App has its own directory and `config.yaml`.

Current product identity:

| Directory | Display name | Slug | Current source version |
| --- | --- | --- | --- |
| `export/` | `HA Config Sync — Export` | `ha_config_sync_export` | `0.5.0` |
| `import/` | `HA Config Sync — Import` | `ha_config_sync_import` | `0.3.1` |

Treat the slugs as stable identifiers. Do not rename them after users have installed the Apps.

## Security architecture and invariants

The two-App split is a deliberate security boundary:

- Export has the GitHub **write** deploy key and no Web UI.
- Import has a separate GitHub **read-only** deploy key and an Ingress Web UI.
- Import may write to Home Assistant only through the HA API, only after explicit UI approval, a fresh conflict check, and read-back verification.
- Never put the GitHub write credential in Import.
- Never reuse the same deploy key for Export and Import.

Do not add any of the following unless the user explicitly approves a reviewed architectural change:

- `full_access`
- `docker_api`
- `host_network`
- access to `/config/.storage`
- broad Home Assistant config mappings
- host filesystem access

Current required permissions:

- Export: `homeassistant_api: true`; writable `addon_config` mounted at `/export`; no Ingress; `startup: once`; `boot: manual_only`.
- Import: `homeassistant_api: true`; Ingress on internal port `8099`; read-only `addon_config` mounted at `/review`; no Docker, host network, or full access.

Do not weaken the Ingress-only request check in `import/app.py` without understanding the Supervisor proxy boundary.

## Credentials and sensitive data

Credentials are runtime files in Home Assistant-generated `addon_configs` directories. Repository-installed App IDs include a repository-specific hashed prefix, so discover the actual directories after installation instead of guessing them.

Paths inside the containers:

- Export key: `/export/ssh/github_ed25519`
- Export host keys: `/export/ssh/known_hosts_443`
- Import key: `/review/ssh/github_ed25519`
- Import host keys: `/review/ssh/known_hosts_443`

GitHub SSH uses `ssh.github.com` on port `443` with strict host key checking.

Never commit, print, paste into chat, or expose:

- private deploy keys
- tokens or passwords
- `addon_config` / `addon_configs`
- generated secrets
- private-key material
- credential-bearing URLs

Do not add a token to a custom App Repository URL. If Home Assistant cannot clone a private App Repository, prefer making the code repository public after explicit user approval rather than embedding credentials in Supervisor configuration.

Run `./scripts/check` before every commit. It validates the repository structure and scans for common credential patterns and forbidden files.

## Export behavior

Export is a one-shot job. Preserve these properties:

- Inventory is built from allowlisted fields, not raw HA objects.
- MAC addresses, identifier-like values, and private IPs are sanitized from human-readable fields.
- States, history, raw `.storage`, and credentials are not exported.
- Dashboards are read through the Home Assistant WebSocket API.
- Automations, scripts, and scenes are exported as sanitized snapshots through supported HA APIs.
- Dashboard base state contains SHA-256 hashes only.
- Git staging uses an explicit allowlist; never replace it with repository-wide `git add .`.
- No commit is created when the staged export is unchanged.
- A pending GitHub dashboard change must not be overwritten by Export.
- Dashboard deletion is never automatic.

The destination data repository and branch are currently fixed in code as `BartoszWu/home-assistant-config`, branch `main`.

## Import behavior

Import is a long-running Flask/Gunicorn Ingress App. Preserve these properties:

- GitHub access is read-only.
- Only JSON files directly under `dashboards/` are considered.
- Path traversal and nested paths are rejected.
- Desired dashboard JSON is scanned for credential-like fields and URLs.
- Status is derived from GitHub HEAD, current HA state, and the exported base hash.
- Apply is allowed only for `READY TO APPLY`.
- Every POST refreshes GitHub and repeats the conflict check before saving.
- Apply uses `lovelace/config/save` through the Home Assistant WebSocket API.
- Every save is read back and hash-verified.
- Automations, scripts, and scenes are currently review/snapshot-only; do not add Apply support casually.

During migrations, prefer an Import smoke test of Ingress, GitHub read access, status, and diff. Do not perform a risky Apply unless a clearly safe test change already exists and the status is unambiguous.

## Development and release workflow

Normal workflow:

```bash
./scripts/check
./scripts/bump <export|import> <patch|minor|major>
./scripts/check
git diff
git status
git commit
git push
```

`./scripts/release <export|import> <patch|minor|major>` runs bump, checks, and displays Git state. It must not automatically commit or push.

Every code change that must reach Home Assistant needs a version bump in the changed App's `config.yaml`. Do not bump the unaffected App merely to keep versions aligned.

Production update flow:

```text
edit on Mac
→ scripts/check
→ bump the changed App
→ scripts/check
→ review diff
→ commit and push
→ Home Assistant App Store: Check for updates
→ Update
```

Do not create a Home Assistant-side alias that runs `git pull` into `/addons`. Production updates go through the Home Assistant App Repository.

Do not add GHCR images or GitHub Actions builds unless the user asks for that next phase.

## Home Assistant migration and testing rules

The legacy local Apps may exist under these IDs:

- `local_ha_config_exporter`
- `local_ha_config_review`

Do not assume their installed versions; verify them with Home Assistant before acting.

Migration order is strict:

1. Make the App Repository available to Home Assistant.
2. Confirm both new Apps appear with the exact names and slugs above.
3. Install the new Apps without removing or stopping the legacy Apps.
4. Discover the new generated App IDs and `addon_configs` paths.
5. Copy only the required credential files on HAOS, without displaying their contents.
6. Set directory permissions to `0700` and credential file permissions to `0600`.
7. Test Export manually and inspect logs, including SSH-over-443 and no-empty-commit behavior.
8. Smoke-test Import Ingress, GitHub read access, diff, and status. Avoid Apply during migration unless the test is clearly safe.
9. Update the existing automation named `Sync HA config to GitHub` to call the new Export App ID. Preserve its schedule and delay; do not create a duplicate.
10. Only after both new Apps pass tests, stop and uninstall the old local Apps.

Prefer leaving old `addon_configs` directories as temporary credential backups until the user explicitly chooses cleanup. Never delete them before the new Apps have proven access.

## Working-tree discipline

- Inspect `git status` before editing.
- Preserve user changes and unrelated untracked files.
- Never force-push or rewrite published history unless the user explicitly requests it.
- Do not use destructive Git commands to resolve ordinary mistakes.
- Keep changes focused and review `git diff` plus `git diff --check` before committing.
- Update this file when architecture, permissions, slugs, credential paths, migration state, or release workflow changes.

## Current migration checkpoint

As of 2026-08-22:

- The initial commit IDs were intentionally rewritten to remove private author metadata; do not rely on pre-cleanup hashes.
- Sources were copied from the running HAOS local Apps rather than reconstructed from chat history.
- `scripts/check` passed and no credentials were committed.
- The legacy Apps were deliberately left running.
- Adding the custom repository in Home Assistant failed because GitHub requested authentication, indicating that the App Repository was private.
- No new repository Apps were installed, no credentials were copied, no automation was changed, and no legacy App was stopped or uninstalled.

Before continuing that migration, verify the current state instead of assuming this checkpoint is still accurate.
