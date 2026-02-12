ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.12-alpine3.20
FROM ${BUILD_FROM}

ARG BUILD_VERSION=2.0.0
ARG BUILD_ARCH=amd64

LABEL \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.type="addon" \
    io.hass.arch="${BUILD_ARCH}"

# Install runtime dependencies
RUN apk add --no-cache sqlite

# Set working directory
WORKDIR /app

# Copy and install Python dependencies
COPY app/requirements.txt /app/
RUN pip3 install --no-cache-dir -r requirements.txt

# Copy application
COPY app/ /app/
COPY run.sh /

RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
