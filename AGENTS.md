# HA Config Sync

Public App code only; private home data belongs in `home-assistant-config`.
Never copy home snapshots, credentials or identifying logs into this repo/PRs.

## Start and checks

- Run from this root: `./scripts/check` before every commit, plus `git diff --check`.
  This script defines required tools/checks; report failures and skipped checks.
- For iteration: `python3 -m unittest discover -s tests -p 'test_stage1_security.py'`
  or the relevant test file.
- Versions/dependencies come from App `config.yaml` and Dockerfiles. Do not
  duplicate them here. Docs-only changes need no bump, HA access or deployment.

## Read for the task

Use sections of [README](README.md), not the entire project history:

| Task | Read first |
| --- | --- |
| Permissions/credentials | Architecture; Credentials |
| Export/sanitization | Stage 1 inventory and safety contract |
| Analysis and policy | [STAGE2.md](STAGE2.md) |
| Import/Apply | Import source revision; Canonical vs feature deployments; Managed files and frontend modules |
| Import review UI | Import review UI |
| Release/installation | Development workflow; Add to Home Assistant |

For house-specific work also read the workspace instructions and
[project skill](../.agents/skills/home-assistant-project/SKILL.md), if available.
Standalone code checks must not depend on private sibling files.

## Invariants to preserve

- Export owns the GitHub write key, with no UI. Import has a distinct read-only
  key and Ingress-only UI. Never weaken the Ingress check or print secrets.
- No permission expansion, `.storage` access, automatic Core restart or broader
  host mounts. Managed-file allowlists live in App code, never in home data.
  Preserve application path guards and AppArmor; synthetic fixtures only.
- Apply requires explicit UI approval, immutable reviewed SHA, fresh conflict
  check, atomic writes/rollback and read-back. Feature LIVE must never be
  exported as canonical main. Missing/corrupt provenance after initialization
  must fail closed; cached CANONICAL provenance cannot authorize writes.
- Preserve pending Git dashboard edits, no-empty-commit and no automatic
  dashboard deletion. Import never merges or pushes. Snapshot automations,
  scripts and scenes do not imply Apply support.
- Verified dashboard Apply requests Export once; blocked/failed Apply and
  managed-file-only Apply do not. Discover installation IDs, never hard-code.
- Keep item status and reviewed commit visible, and require an explicit selection
  before Apply. Security warnings remain individually selected, outside bulk selection.
- Preserve identical security/provenance modules across App build contexts;
  `scripts/check` enforces the mirrors. Add regression tests at changed boundaries.

## Delivery

- Submit each independent change as a separate PR; update the existing PR for
  follow-up fixes to the same task. No direct pushes to main or merge/deployment
  without authorization.
- Code intended for HA: bump only the changed App with
  `./scripts/bump <export|import> <patch|minor|major>`, then rerun checks.
- Production updates use the App Repository; no HA-side git-pull aliases.
  No GHCR/build infrastructure unless requested. Never remove credential backups
  or migrate installations merely to test code; verify LIVE before such work.
- Report scope, checks, limitations and commit/PR. Update docs when contracts change.
