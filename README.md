# Signup App

API key management service where authenticated users can create, view, and revoke API keys. Similar to OpenAI or Anthropic's API key dashboards.

## Tech Stack

- **Backend:** FastAPI (Python 3.11+)
- **Frontend:** Plain HTML/CSS/JS (no build step)
- **Database:** PostgreSQL 16 with SQLAlchemy 2.0+
- **Auth:** Reverse proxy header injection (production), debug bypass (development)

## Quick Start

```bash
# Start PostgreSQL
docker compose up -d postgres

# Setup
cp .env.example .env
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Run
python -m uvicorn app.main:app --reload --port 8000
```

Open http://localhost:8000 to access the key management UI.

## Docker

```bash
cp .env.example .env
docker compose up --build
```

## Authentication

In **production**, the app sits behind a reverse proxy that handles authentication and injects an `X-User-Email` header. The app trusts this header and auto-creates user records on first request.

In **development**, set `DEBUG_MODE=true` in `.env` to bypass auth and use the `TEST_USER` value.

## API Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/health` | No | Health check |
| GET | `/api/me` | Yes | Current user info |
| GET | `/api/keys` | Yes | List keys (masked) |
| POST | `/api/keys` | Yes | Create key (full key returned once) |
| PATCH | `/api/keys/{id}` | Yes | Update key name |
| DELETE | `/api/keys/{id}` | Yes | Revoke key |
| POST | `/api/keys/verify` | No | Verify a key (for downstream services) |

## Feature Flags

Progressive functionality controlled via environment variables:

```env
FEATURE_KEY_LAST_USED=false     # Track last used timestamp
FEATURE_KEY_EXPIRATION=false    # Allow setting key expiry
FEATURE_KEY_RATE_LIMIT=false    # Per-key rate limits
FEATURE_KEY_SCOPES=false        # Per-key permission scopes
```

## Tests

```bash
pytest tests/ -v
```

Requires a running PostgreSQL instance. Configure via `TEST_DATABASE_URL` env var.
