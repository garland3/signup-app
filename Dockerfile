# Two-stage build on Red Hat Hardened Images (Project Hummingbird).
#
# Base images are pinned by digest to a fixed Python 3.15 variant
# (3.15.0-builder / 3.15.0) so the build is reproducible. CI runs the test
# suite on this same Python (the "Test (Python 3.15)" job) in addition to the
# 3.11 floor from pyproject.toml (requires-python >=3.11). The human-readable
# tag is kept alongside the digest for clarity; podman/docker resolve by
# digest. Refresh both the tag and the digest together when bumping the base
# image (builder and runtime to the same Python), and keep the CI job's Python
# version in step. The "Build image" CI job builds and smoke-tests this file.

# Build stage: Hardened Images Python builder (retains a shell, pip, and build
# tooling so we can resolve and install dependencies into an isolated venv).
FROM registry.access.redhat.com/hi/python:3.15.1-builder@sha256:88675838068606429a0c6e39337bfd7bc27a888c096e650f997cc9c560466f7c AS builder
USER root
ENV HOME=/root
WORKDIR /app

# Build the venv straight from the committed lockfile so the image ships the
# EXACT dependency set resolved in the repo / CI (uv.lock) rather than a fresh
# resolution at build time.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never

# Dependency manifests (incl. the lockfile) plus app sources.
COPY pyproject.toml uv.lock ./
COPY app/ app/
COPY mocks/ mocks/
COPY static/ static/

# Install uv, then sync runtime dependencies from the lockfile into /opt/venv.
#   --frozen: install exactly what uv.lock pins; never re-resolve.
#   --no-dev: exclude the dev/test extra from the runtime image.
# The venv is built on the interpreter's real path (e.g. /usr/bin/python3.15),
# found here rather than hard-coded, so a base-image bump needs no edit; the
# runtime stage checks that the same path exists there.
#
# Behind a TLS-inspecting corporate proxy, pass its CA so pip and uv can
# download packages:
#   docker build --secret id=corp_ca,src=corp-ca.pem .
# The CA is added to the system bundle for this step only; it is mounted as a
# secret, so it is never stored in an image layer. Without the secret the
# build is unchanged.
RUN --mount=type=secret,id=corp_ca \
    if [ -s /run/secrets/corp_ca ]; then \
      cat /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem /run/secrets/corp_ca > /tmp/ca-bundle.pem && \
      export SSL_CERT_FILE=/tmp/ca-bundle.pem PIP_CERT=/tmp/ca-bundle.pem; \
    fi && \
    python3 -m pip install --no-cache-dir uv && \
    UV_PYTHON="$(python3 -c 'import os, sys; print(os.path.realpath(sys.executable))')" \
    uv sync --frozen --no-dev && \
    rm -f /tmp/ca-bundle.pem

# Runtime stage: minimal distroless Hardened Image (no shell, no package
# manager) for a near-zero-CVE footprint, pinned to the matching 3.15 runtime
# digest. Only the venv and app code ship.
FROM registry.access.redhat.com/hi/python:3.15.1@sha256:34105beff4de6c524595e139b2055ba3a8a240890bd185e7c27d1c755658e2e4 AS runtime
WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY app/ app/
COPY mocks/ mocks/
COPY static/ static/

# Fail the build, not the deployment, if the venv's interpreter is missing
# here (builder and runtime on different Python versions) or a compiled
# dependency doesn't load. Exec form: this image has no shell.
RUN ["/opt/venv/bin/python", "-c", "import fastapi, pydantic_core, uvicorn, uvloop, httptools, yaml"]

ENV PATH="/opt/venv/bin:${PATH}"
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
