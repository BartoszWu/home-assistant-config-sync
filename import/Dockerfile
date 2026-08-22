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
        "Flask==3.1.1" \
        "gunicorn==23.0.0" \
        "websocket-client==1.8.0"

COPY app.py /app.py
COPY run.sh /run.sh

RUN chmod 0755 /run.sh

CMD ["/run.sh"]
