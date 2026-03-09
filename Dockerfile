FROM registry.access.redhat.com/ubi9/ubi:latest

WORKDIR /app

RUN dnf install -y python3.11 python3.11-pip python3.11-devel && \
    dnf clean all

COPY pyproject.toml .
RUN python3.11 -m pip install --no-cache-dir .

COPY app/ app/
COPY static/ static/

RUN useradd -r appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

CMD ["python3.11", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
