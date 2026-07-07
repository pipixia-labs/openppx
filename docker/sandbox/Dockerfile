ARG PYTHON_BASE_IMAGE=python:3.14-slim
FROM ${PYTHON_BASE_IMAGE}

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        coreutils \
        git \
        nodejs \
        npm \
        procps \
        tini \
    && rm -rf /var/lib/apt/lists/*

RUN cat > /usr/local/bin/openppx-sandbox-entrypoint <<'EOF' \
    && chmod +x /usr/local/bin/openppx-sandbox-entrypoint
#!/bin/sh
set -eu

: "${HOME:=/tmp/openppx-home}"
mkdir -p "$HOME"
export HOME
export TMPDIR="${TMPDIR:-/tmp}"

exec "$@"
EOF

ENTRYPOINT ["/usr/local/bin/openppx-sandbox-entrypoint"]
CMD ["python", "--version"]
