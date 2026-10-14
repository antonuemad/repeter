# Repeater application image: web (uvicorn), worker and one-shot commands share it.
#
#   docker build -t repeater:test .
#   docker build --target test -t repeater:ci-test .   # CI: the media tests run on the shipped ffmpeg
#
# Behind a TLS-intercepting proxy, pass its CA bundle as an optional build secret
# (never baked into the image):
#   docker build --secret id=build_ca,src=/path/to/ca-bundle.crt -t repeater:test .

ARG PYTHON_IMAGE=python:3.12-slim

# --------------------------------------------------------------------------- deno
# yt-dlp needs a JavaScript runtime for YouTube (https://github.com/yt-dlp/yt-dlp/wiki/EJS);
# Deno is the one it enables by default. Pinned release, verified by SHA-256.
FROM ${PYTHON_IMAGE} AS deno
ARG DENO_VERSION=2.9.5
ARG DENO_SHA256_AMD64=8b010a3b1a4a0188a67cdb8a7a27348b2a501af78aec7fc74f2ace167368d530
ARG DENO_SHA256_ARM64=6b7cae3a8fc4385a59dea3146fcb8bad7fea4230e0ad36a8c692afacbc254be0
ARG TARGETARCH
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/*
RUN --mount=type=secret,id=build_ca \
    set -eu; \
    case "${TARGETARCH:-amd64}" in \
        amd64) triple=x86_64-unknown-linux-gnu; sum="${DENO_SHA256_AMD64}" ;; \
        arm64) triple=aarch64-unknown-linux-gnu; sum="${DENO_SHA256_ARM64}" ;; \
        *) echo "unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    ca=/etc/ssl/certs/ca-certificates.crt; \
    if [ -s /run/secrets/build_ca ]; then ca=/run/secrets/build_ca; fi; \
    curl -fsSL --retry 3 --connect-timeout 20 --max-time 600 --cacert "$ca" -o /tmp/deno.zip \
        "https://github.com/denoland/deno/releases/download/v${DENO_VERSION}/deno-${triple}.zip"; \
    echo "${sum}  /tmp/deno.zip" | sha256sum -c -; \
    unzip -q /tmp/deno.zip -d /usr/local/bin; \
    chmod 0755 /usr/local/bin/deno; \
    rm /tmp/deno.zip; \
    /usr/local/bin/deno --version

# --------------------------------------------------------------------------- base
# Everything the web, the worker and the tests need; the last stage ("app") is the default target.
FROM ${PYTHON_IMAGE} AS base

LABEL org.opencontainers.image.title="repeater" \
      org.opencontainers.image.description="Repeater: short-video rotation (web, worker, migrations)" \
      org.opencontainers.image.source="https://rotator.magic-lap.com"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    REPEATER_MEDIA_ROOT=/srv/repeater/media

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 repeater \
    && useradd --system --uid 10001 --gid 10001 --create-home --home-dir /home/repeater \
        --shell /usr/sbin/nologin repeater \
    && install -d -o 10001 -g 10001 -m 0750 /srv/repeater/media

COPY --from=deno /usr/local/bin/deno /usr/local/bin/deno

WORKDIR /app

# Dependencies first, from pyproject.toml only, so code changes reuse this layer.
# yt-dlp[default] adds yt-dlp-ejs, the YouTube JS challenge solver Deno runs.
COPY pyproject.toml /tmp/build/pyproject.toml
RUN --mount=type=secret,id=build_ca \
    --mount=type=cache,target=/root/.cache/pip \
    set -eu; \
    if [ -s /run/secrets/build_ca ]; then export PIP_CERT=/run/secrets/build_ca; fi; \
    python -c "import tomllib; print('\n'.join(tomllib.load(open('/tmp/build/pyproject.toml', 'rb'))['project']['dependencies']))" \
        > /tmp/build/requirements.txt; \
    pip install -r /tmp/build/requirements.txt "yt-dlp[default]" "setuptools>=69"

# Bump to pull the newest yt-dlp without touching other dependencies
# (deploy.sh passes the current date, so each day's first deploy refreshes it).
ARG YTDLP_REFRESH=0
RUN --mount=type=secret,id=build_ca \
    --mount=type=cache,target=/root/.cache/pip \
    set -eu; \
    if [ -s /run/secrets/build_ca ]; then export PIP_CERT=/run/secrets/build_ca; fi; \
    echo "yt-dlp refresh: ${YTDLP_REFRESH}"; \
    pip install --upgrade "yt-dlp[default]"

COPY pyproject.toml README.md alembic.ini ./
COPY migrations ./migrations
COPY repeater ./repeater
# Editable install: repeater.cli finds alembic.ini and migrations/ next to the package in /app.
RUN pip install --no-deps --no-build-isolation -e . \
    && python -m compileall -q /app/repeater /app/migrations \
    && rm -rf /tmp/build

# --------------------------------------------------------------------------- test (CI only, never deployed)
# The same Debian ffmpeg, Python and libraries as the shipped image, plus the dev tools. Tests are
# not copied in; CI bind-mounts them:
#   docker run --rm --network host -e REPEATER_TEST_PG=... -v "$PWD/tests:/app/tests:ro" repeater:ci-test
FROM base AS test
RUN --mount=type=secret,id=build_ca \
    --mount=type=cache,target=/root/.cache/pip \
    set -eu; \
    if [ -s /run/secrets/build_ca ]; then export PIP_CERT=/run/secrets/build_ca; fi; \
    python -c "import tomllib; print('\n'.join(tomllib.load(open('/app/pyproject.toml', 'rb'))['project']['optional-dependencies']['dev']))" \
        > /tmp/dev-requirements.txt; \
    pip install -r /tmp/dev-requirements.txt; \
    rm /tmp/dev-requirements.txt
USER 10001:10001
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_media.py", "tests/test_sources.py"]

# --------------------------------------------------------------------------- app
FROM base AS app
USER 10001:10001
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --start-interval=3s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"]

ENTRYPOINT ["/usr/bin/tini", "--"]
# Access logs are off: request lines carry OAuth codes and signed media links.
# X-Forwarded-For/Proto are honoured only from the addresses in $FORWARDED_ALLOW_IPS (uvicorn reads
# it; its default is loopback). docker-compose.yml sets it to loopback plus Docker's private ranges
# (REPEATER_FORWARDED_ALLOW_IPS). Never "*": uvicorn would then trust the LEFTMOST X-Forwarded-For
# entry, which the client controls whenever the proxy appends instead of overwriting.
CMD ["uvicorn", "repeater.web.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--no-access-log", "--timeout-graceful-shutdown", "20"]
