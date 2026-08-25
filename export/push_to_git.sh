#!/usr/bin/with-contenv bashio
set -euo pipefail

REPO="ssh://git@ssh.github.com:443/BartoszWu/home-assistant-config.git"
BRANCH="main"

KEY="/export/ssh/github_ed25519"
KNOWN_HOSTS="/export/ssh/known_hosts_443"

WORKDIR="/tmp/home-assistant-config"
CURRENT_DASHBOARDS="/tmp/ha-current/dashboards"

if [[ ! -f "$KEY" ]]; then
    echo "ERROR: GitHub deploy key missing"
    exit 1
fi

if [[ ! -f "$KNOWN_HOSTS" ]]; then
    echo "ERROR: GitHub known_hosts missing"
    exit 1
fi

chmod 600 "$KEY" "$KNOWN_HOSTS"

export GIT_SSH_COMMAND="ssh \
  -i $KEY \
  -o IdentitiesOnly=yes \
  -o UserKnownHostsFile=$KNOWN_HOSTS \
  -o StrictHostKeyChecking=yes \
  -p 443"

echo "Testing access to GitHub repository..."
git ls-remote "$REPO" HEAD >/dev/null
echo "GitHub access OK."

rm -rf "$WORKDIR"

git clone \
    --depth 1 \
    --branch "$BRANCH" \
    "$REPO" \
    "$WORKDIR"

mkdir -p \
    "$WORKDIR/inventory" \
    "$WORKDIR/docs" \
    "$WORKDIR/config"

cp /export/entities.json "$WORKDIR/inventory/entities.json"
cp /export/states.json "$WORKDIR/inventory/states.json"
cp /export/ENTITIES.md "$WORKDIR/docs/ENTITIES.md"
cp /export/DEVICES.md "$WORKDIR/docs/DEVICES.md"
cp /export/STATES.md "$WORKDIR/docs/STATES.md"

python3 /sync_dashboards.py "$WORKDIR" "$CURRENT_DASHBOARDS"

# Managed config remains snapshot-only in this release. Apply is still disabled
# for automations, scripts and scenes.
rm -rf "$WORKDIR/config/storage"
cp -a /export/config/storage "$WORKDIR/config/storage"

cd "$WORKDIR"

git config user.name "Home Assistant Exporter"
git config user.email "home-assistant-exporter@localhost"

# Explicit allowlist. Never use a repository-wide `git add .` here.
git add -- \
    inventory/entities.json \
    inventory/states.json \
    docs/ENTITIES.md \
    docs/DEVICES.md \
    docs/STATES.md

git add -A -- \
    dashboards \
    state/dashboard-bases.json \
    config/storage

if git diff --cached --quiet; then
    echo "✅ HA export unchanged - nothing to commit."
    exit 0
fi

echo
echo "Files that will be committed:"
git diff --cached --name-status

echo
echo "Diff summary:"
git diff --cached --stat

git commit -m "Update Home Assistant export"
git push origin "HEAD:${BRANCH}"

echo
echo "✅ HA export pushed to GitHub."
