# Two-stage build on Red Hat Hardened Images (Project Hummingbird).
#
# Base images are pinned by digest to a fixed Python 3.11 variant
# (3.11.15-builder / 3.11.15) so the build is reproducible and the runtime
# Python stays aligned with pyproject.toml (requires-python >=3.11) and CI
# (`uv python install 3.11`). The human-readable tag is kept alongside the
# digest for clarity; podman/docker resolve by digest. Refresh both the tag
# and the digest together when bumping the base image.

# Build stage: Hardened Images Python builder (retains a shell, pip, and build
# tooling so we can resolve and install dependencies into an isolated venv).
FROM registry.access.redhat.com/hi/python:3.11.15-builder@sha256:74d89307d275d5dad3f2670ba701a248270ba92bdba88d68653e79481ee7f815 AS builder
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
# manager) for a near-zero-CVE footprint, pinned to the matching 3.11 runtime
# digest. Only the venv and app code ship.
FROM registry.access.redhat.com/hi/python:3.11.15@sha256:b2819d6ec6adbc2f794339aa0b4cba2a57cb1ddff79ddf7e13a430be581e9d5c AS runtime
WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY app/ app/
COPY mocks/ mocks/
COPY static/ static/

ENV PATH="/opt/venv/bin:${PATH}"
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
