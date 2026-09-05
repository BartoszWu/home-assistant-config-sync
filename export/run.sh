#!/usr/bin/with-contenv bashio
set -euo pipefail

bashio::log.info "HA Config Sync — Export starting..."

python3 /export_status.py

/push_to_git.sh

bashio::log.info "HA Config Sync — Export finished."
