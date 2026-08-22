#!/usr/bin/with-contenv bashio
set -euo pipefail

bashio::log.info "HA Config Sync — Import starting..."

cd /

exec gunicorn \
    --bind 0.0.0.0:8099 \
    --workers 1 \
    --threads 4 \
    --access-logfile - \
    --error-logfile - \
    app:app
