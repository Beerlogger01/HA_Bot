ARG BUILD_FROM
FROM ${BUILD_FROM}

# Install runtime dependencies
RUN apk add --no-cache \
    sqlite

# Set working directory
WORKDIR /app

# Copy app files
COPY app/requirements.txt /app/
RUN pip3 install --no-cache-dir -r requirements.txt

COPY app/ /app/
COPY run.sh /

# Make run.sh executable
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
