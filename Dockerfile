# Two-stage build on Red Hat Hardened Images (Project Hummingbird).
#
# Base images are pinned by digest to a fixed Python 3.15 variant
# (3.15.0-builder / 3.15.0) so the build is reproducible. CI runs the test
# suite on this same Python (the "Test (Python 3.15)" job) in addition to the
# 3.11 floor from pyproject.toml (requires-python >=3.11). The human-readable
# tag is kept alongside the digest for clarity; podman/docker resolve by
# digest. Refresh both the tag and the digest together when bumping the base
# image, and keep the CI job's Python version in step.

# Build stage: Hardened Images Python builder (retains a shell, pip, and build
# tooling so we can resolve and install dependencies into an isolated venv).
FROM registry.access.redhat.com/hi/python:3.15.0-builder@sha256:cf9e10442a6098f35b2de1021c170be0fad1706cad10a9c2d58ae4c01f98b427 AS builder
USER root
ENV HOME=/root
WORKDIR /app

# Build the venv straight from the committed lockfile so the image ships the
# EXACT dependency set resolved in the repo / CI (uv.lock) rather than a fresh
# resolution at build time. UV_PYTHON is the interpreter's real path, which
# the builder and runtime images share; /opt/venv links to it, so it must
# exist in both (update it with the Python version).
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON=/usr/bin/python3.15 \
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
# manager) for a near-zero-CVE footprint, pinned to the matching 3.15 runtime
# digest. Only the venv and app code ship.
FROM registry.access.redhat.com/hi/python:3.15.0@sha256:cdc5079f0c2a48a1884ef5e47180700f881f11a753566694cb0cace60325dd9c AS runtime
WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY app/ app/
COPY mocks/ mocks/
COPY static/ static/

ENV PATH="/opt/venv/bin:${PATH}"
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
