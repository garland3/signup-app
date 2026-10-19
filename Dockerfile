# Two-stage build on Red Hat Hardened Images (Project Hummingbird).
#
# Base images are pinned by digest to a fixed Python 3.14 variant
# (3.14.7-builder / 3.14.7) so the build is reproducible. CI runs the test
# suite on this same Python (the "Test (Python 3.14)" job) in addition to the
# 3.11 floor from pyproject.toml (requires-python >=3.11). The human-readable
# tag is kept alongside the digest for clarity; podman/docker resolve by
# digest. Refresh both the tag and the digest together when bumping the base
# image, and keep the CI job's Python version in step.

# Build stage: Hardened Images Python builder (retains a shell, pip, and build
# tooling so we can resolve and install dependencies into an isolated venv).
FROM registry.access.redhat.com/hi/python:3.14.8-builder@sha256:9b36f26736c446ca2efd983b57c0187997f1c81c21349c9b8a716c8c6093e25a AS builder
USER root
ENV HOME=/root
WORKDIR /app

# Build the venv straight from the committed lockfile so the image ships the
# EXACT dependency set resolved in the repo / CI (uv.lock) rather than a fresh
# resolution at build time.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON=/usr/sbin/python3 \
    UV_PYTHON_DOWNLOADS=never

# Dependency manifests (incl. the lockfile) plus app sources.
COPY pyproject.toml uv.lock ./
COPY app/ app/
COPY mocks/ mocks/
COPY static/ static/

# Install uv, then sync runtime dependencies from the lockfile into /opt/venv.
#   --frozen: install exactly what uv.lock pins; never re-resolve.
#   --no-dev: exclude the dev/test extra from the runtime image.
RUN python3 -m pip install --no-cache-dir uv && \
    uv sync --frozen --no-dev

# Runtime stage: minimal distroless Hardened Image (no shell, no package
# manager) for a near-zero-CVE footprint, pinned to the matching 3.14 runtime
# digest. Only the venv and app code ship.
FROM registry.access.redhat.com/hi/python:3.14.8@sha256:be9566f3448e5a58a9c1a0dd2d97dc07d2f7fd88eed62d3e83f1d84803c415f8 AS runtime
WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY app/ app/
COPY mocks/ mocks/
COPY static/ static/

ENV PATH="/opt/venv/bin:${PATH}"
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
