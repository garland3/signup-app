# AGENTS.md

This file provides guidance to AI coding agents when working with code in this repository.

## Project Overview

Signup App is an API key management UI backed by a LiteLLM proxy. Authenticated users can create, view, and delete API keys. The app does not manage a database directly -- all key operations are proxied to LiteLLM's admin API.

**Tech Stack:**
- Backend: FastAPI (Python 3.11+)
- Frontend: Plain HTML/CSS/JS (no framework, no build step)
- Key Storage: LiteLLM proxy (admin API)
- Auth: Reverse proxy header injection (production), debug bypass (development)
- Containerization: Docker Compose

## Style and Conventions

**No Emojis**: No emojis anywhere in this codebase (code, comments, docs, commit messages).

**File Naming**: Use descriptive names that reflect purpose (e.g., `auth_middleware.py`, `litellm_client.py`). Do not use generic names like `utils.py` or `helpers.py`.

**File Size**: Prefer files with 400 lines or fewer when practical.

**Keep It Simple**: This is a small, focused app. Do not over-engineer.

**No Build Step for Frontend**: The frontend is plain HTML/CSS/JS served from `/static`. Do not introduce a build tool, bundler, or framework.

**Documentation Date-Time Stamping**: When creating markdown files, include date-time stamps either in the filename or as a header. Format: `YYYY-MM-DD`.

## Architecture

```
signup-app/
    app/
        main.py              # FastAPI app entrypoint, serves static files
        core/
            config.py        # Pydantic settings, env vars
            middleware.py    # Auth middleware (header extraction + debug bypass)
            litellm_client.py # HTTP client wrapping LiteLLM admin API
        routes/
            health.py        # Health check endpoint
            users.py         # User info endpoint
            keys.py          # API key management (proxies to LiteLLM)
    mocks/
        litellm_mock.py      # Mock LiteLLM server for testing
    static/
        index.html           # Single-page frontend
        style.css            # Styles
        app.js               # Frontend logic
    tests/                   # Unit and integration tests
    docker-compose.yml       # Mock LiteLLM + app services
    Dockerfile               # Container build (RHEL 9 UBI)
    .env.example             # All configuration variables
    pyproject.toml           # Python dependencies (single source of truth)
```

## How It Works

The app is a thin UI layer over LiteLLM's key management API:

```
User Browser -> Reverse Proxy -> Signup App (FastAPI) -> LiteLLM Proxy (admin API)
```

1. Reverse proxy authenticates the user and injects `X-User-Email` header
2. Signup App's auth middleware extracts the email
3. Key operations (create/list/delete) are forwarded to LiteLLM using the admin key
4. Keys are scoped to the user via LiteLLM's `user_id` field

The app holds a single `LITELLM_ADMIN_KEY` that has full access. Users never see or use this key -- they only interact with their own generated keys.

## Authentication

### Production (Reverse Proxy)
- Default header: `X-User-Email` (configurable via `AUTH_USER_HEADER`)
- Optional proxy secret validation via `PROXY_SECRET_HEADER` and `PROXY_SECRET`
- Requests without a valid auth header receive 401

### Development (Debug Mode)
- Set `DEBUG_MODE=true` in `.env`
- Falls back to `TEST_USER` env var (default: `test@test.com`)
- Proxy secret validation is skipped

### Middleware Order
```
Request -> Auth -> Route -> LiteLLM Proxy
```

## LiteLLM Integration

The `LiteLLMClient` in `app/core/litellm_client.py` wraps these LiteLLM admin endpoints:

| Our Endpoint | LiteLLM Endpoint | Method |
|-------------|------------------|--------|
| POST /api/keys | /key/generate | POST |
| GET /api/keys | /key/list | GET |
| PATCH /api/keys/{token} | /key/update | POST |
| DELETE /api/keys/{token} | /key/delete | POST |
| POST /api/keys/{token}/block | /key/block | POST |
| POST /api/keys/{token}/unblock | /key/unblock | POST |

All requests to LiteLLM include `Authorization: Bearer {LITELLM_ADMIN_KEY}`.

## Configuration

All configuration is via environment variables, loaded through Pydantic `BaseSettings`.

```env
# Core
DEBUG_MODE=false

# LiteLLM Proxy
LITELLM_BASE_URL=http://localhost:4000
LITELLM_ADMIN_KEY=sk-your-admin-key

# Auth
AUTH_USER_HEADER=X-User-Email
TEST_USER=test@test.com
PROXY_SECRET_HEADER=X-Proxy-Secret
PROXY_SECRET=
FEATURE_PROXY_SECRET_ENABLED=false
```

## Mock LiteLLM Server

For development and testing, `mocks/litellm_mock.py` provides an in-memory implementation of the LiteLLM key management API. It stores keys in a dict and implements all the routes the app uses.

Run it: `python -m uvicorn mocks.litellm_mock:app --port 4000`

Admin key for the mock: `sk-mock-admin-key`

## Development Commands

### Quick Start
```bash
# Start mock LiteLLM
python -m uvicorn mocks.litellm_mock:app --port 4000 &

# Setup
cp .env.example .env  # set LITELLM_ADMIN_KEY=sk-mock-admin-key
python -m uvicorn app.main:app --reload --port 8000
```

### Docker
```bash
docker compose up --build
```

### Testing
```bash
pytest tests/ -v
```

## API Endpoints

### No Auth Required
- `GET /api/health` -- returns `{"status": "ok"}`

### Auth Required
- `GET /api/me` -- current user email
- `GET /api/keys` -- list user's API keys (masked)
- `POST /api/keys` -- create new key (returns full key once)
- `PATCH /api/keys/{token}` -- update key settings
- `DELETE /api/keys/{token}` -- delete key permanently
- `POST /api/keys/{token}/block` -- block a key
- `POST /api/keys/{token}/unblock` -- unblock a key

## Common Issues

1. **502 from key endpoints**: LiteLLM proxy is unreachable or `LITELLM_ADMIN_KEY` is wrong
2. **No auth header in production**: Ensure reverse proxy injects `AUTH_USER_HEADER`
3. **Key shown as undefined**: Full key is only returned in the POST create response
4. **Empty key list**: Check that `user_id` matches the email used to create keys

## Critical Restrictions

- **NEVER expose the LITELLM_ADMIN_KEY to users** -- it stays server-side only
- **NEVER return full API keys after creation** -- only the prefix in list views
- **NEVER skip auth middleware for key management endpoints**
- **NEVER introduce a frontend build step** -- keep it plain HTML/CSS/JS
- **NEVER add dependencies without adding them to pyproject.toml**
