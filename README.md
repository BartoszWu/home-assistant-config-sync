# HA Config Sync

Home Assistant App Repository containing two deliberately separate Apps for synchronizing selected Home Assistant configuration through GitHub.

The application code lives here. The exported home data remains in the separate [`BartoszWu/home-assistant-config`](https://github.com/BartoszWu/home-assistant-config) repository.

## Architecture

| App | Runtime | GitHub access | Home Assistant access |
| --- | --- | --- | --- |
| **HA Config Sync — Export** | One-shot, no Web UI | Write deploy key for `home-assistant-config` | Reads selected data through the Supervisor-backed HA API and writes sanitized output to its own app data directory |
| **HA Config Sync — Import** | Long-running Ingress Web UI | Separate read-only deploy key for `home-assistant-config` | Reads dashboards through the HA API; writes a dashboard only after it is explicitly selected and submitted in the UI, followed by conflict checking and read-back verification |

The split keeps the GitHub write credential out of the web-facing Import App. Neither App receives Docker access, host networking, full access, or direct access to `/config/.storage`.

## Repository layout

```text
.
├── repository.yaml
├── export/
│   ├── config.yaml
│   ├── Dockerfile
│   ├── run.sh
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
