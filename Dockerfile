# Build stage: Red Hat Hardened Images (Project Hummingbird) Python builder.
# The "-builder" variant retains a shell, pip, and build tooling so we can
# resolve and install dependencies into an isolated virtual environment.
FROM registry.access.redhat.com/hi/python:latest-builder AS builder
USER root
ENV HOME=/root
WORKDIR /app

COPY pyproject.toml .
COPY app/ app/
COPY mocks/ mocks/
COPY static/ static/

# Install uv, create a self-contained venv under /opt/venv, and install the
# project (and its dependencies) into it.
RUN mkdir -p /root/.config/uv && \
    python3 -m pip install --no-cache-dir uv && \
    uv venv /opt/venv && \
    uv pip install --python /opt/venv/bin/python --no-cache .

# Runtime stage: minimal distroless Hardened Image (no shell, no package
# manager) for a near-zero-CVE footprint. Only the venv and app code ship.
FROM registry.access.redhat.com/hi/python:latest AS runtime
WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY app/ app/
COPY mocks/ mocks/
COPY static/ static/

ENV PATH="/opt/venv/bin:${PATH}"
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
