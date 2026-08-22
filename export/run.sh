#!/usr/bin/with-contenv bashio
set -euo pipefail

bashio::log.info "HA Config Sync — Export starting..."

python3 /exporter.py

bashio::log.info "Inventory export finished."
bashio::log.info "Exporting UI dashboards..."

python3 /dashboard_exporter.py

bashio::log.info "Dashboard export finished."
bashio::log.info "Exporting automations, scripts and scenes..."

python3 /managed_config_exporter.py

bashio::log.info "Managed config export finished."
bashio::log.info "Starting GitHub synchronization..."

/push_to_git.sh

bashio::log.info "HA Config Sync — Export finished."
