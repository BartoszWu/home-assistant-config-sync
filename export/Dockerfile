FROM ghcr.io/home-assistant/base-python:3.14-alpine3.24

ARG BUILD_VERSION
ARG BUILD_ARCH

LABEL \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.type="app" \
    io.hass.arch="${BUILD_ARCH}"

RUN apk add --no-cache \
        git \
        openssh-client \
    && pip3 install \
        --no-cache-dir \
        --break-system-packages \
        "websockets==15.0.1"

COPY security.py /security.py
COPY run.sh /run.sh
COPY exporter.py /exporter.py
COPY inventory.py /inventory.py
COPY export_status.py /export_status.py
COPY validate_export.py /validate_export.py
COPY runtime_inventory.py /runtime_inventory.py
COPY dashboard_exporter.py /dashboard_exporter.py
COPY managed_config_exporter.py /managed_config_exporter.py
COPY sync_dashboards.py /sync_dashboards.py
COPY push_to_git.sh /push_to_git.sh

RUN chmod 0755 /run.sh /push_to_git.sh

CMD ["/run.sh"]
