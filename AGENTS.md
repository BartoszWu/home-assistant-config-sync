# HA Config Sync — agent instructions

This public repository contains the Export and Import Apps. Home data belongs
in the separate private `home-assistant-config` repository. Never copy its data,
inventory, credentials or raw logs into this repository, tests or PRs.

## Start and commands

Run commands from this repository root. Read `git status --short --branch`,
preserve unrelated work, fetch `origin`, and compare `main...origin/main`
before editing. Update only by fast-forward when local work is safe.
Read nested `AGENTS.md` files before editing their directories.

- Required before every commit: `./scripts/check`.
- Focused Python test: `python3 -m unittest discover -s tests -p 'test_stage1_security.py'`.
- Preview JavaScript: `node --check import/static/visual-preview.mjs` and
  `node --test tests/test_visual_preview.mjs`.
- Final diff: `git diff --check`, `git diff`, `git status --short`.

`scripts/check` is the executable validation contract: Ruby with YAML, Python 3,
Node.js for JavaScript checks, and Bash. Read the script if setup fails; report
missing dependencies or skipped checks accurately. Runtime dependencies and
versions are defined in each App's Dockerfile and `config.yaml`.
Documentation-only changes still run `./scripts/check`; they need no App
version bump, HA access or deployment.

## Read the relevant contracts before implementation

Detailed mandatory rules are in [agent-contracts.md](docs/agent-contracts.md).
All paths in that document's prose/code are repository-root-relative unless
explicitly stated otherwise. Read the sections matching the task:

| Task | Required sections |
| --- | --- |
| Any code or permission change | Security architecture and invariants; Credentials and sensitive data |
| Export, sanitization, inventory | Export behavior; Stage 2 analysis contract, then `STAGE2.md` |
| Import, UI, Apply, managed files | Import behavior; Security architecture and invariants |
| Release | Development and release workflow |
| Installation/migration | Home Assistant migration and testing rules; Current migration checkpoint (historical, verify LIVE) |

Use [README.md](README.md) for architecture/setup. For work involving the house,
read the parent workspace `AGENTS.md` and its
[project skill](../.agents/skills/home-assistant-project/SKILL.md), if available.
A standalone code checkout must not rely on private sibling files to run checks.

## Non-negotiable boundaries

- Export owns the GitHub write key and has no Web UI. Import has a distinct
  read-only GitHub key and Ingress-only UI. Preserve the Ingress request check.
- Never log, commit or include credentials, credential-bearing URLs, private
  keys or `addon_config` contents in output. Keep synthetic fixtures sanitized.
- Do not broaden permissions (`full_access`, Docker, host network, arbitrary
  host mounts). Import writes only declared managed paths or supported HA APIs
  after explicit approval, fresh conflict checks and read-back verification.
- Never write `.storage`, bypass path guards, or let the data repository expand
  the managed-file allowlist. Preserve AppArmor defense in depth.
- Keep Apply pinned to its reviewed commit. Preserve rollback and fail-closed
  provenance guards; feature deployments must never become canonical `main`
  through Export. No automatic Core restart.
- Export must preserve pending Git changes, avoid empty commits and never
  delete dashboards automatically. Import never merges or pushes to GitHub.
- Keep mirrored security/provenance modules identical across App build contexts;
  `scripts/check` verifies this. Test regressions at the affected boundary.

## PR and release

- A request for a PR authorizes a focused branch, commit, push and PR; use
  `codex/...`, stage explicit paths, and target this repository's `main`.
  It does not authorize a merge, release, installation or HA Apply.
- Do not use `git add .`, include existing user changes, force-push or rewrite
  published history without explicit authorization.
- Describe the problem, resulting behavior, checks and any limitations in the PR.
- Code intended for HA requires a bump of only the changed App using
  `./scripts/bump <export|import> <patch|minor|major>`, then `./scripts/check`.
  The bump/release scripts do not authorize production updates.
- Keep these instructions and linked contracts aligned when behavior changes.
  Read current versions from `config.yaml`; old migration notes are not LIVE facts.
