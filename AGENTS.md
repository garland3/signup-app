# AGENTS.md

This file provides guidance to AI coding agents when working with code in this repository.

## Project Overview

Signup App is an API key management service where authenticated users can create, view, and revoke API keys. Similar to OpenAI or Anthropic's API key dashboards.

**Tech Stack:**
- Backend: FastAPI (Python 3.11+)
- Frontend: Plain HTML/CSS/JS (no framework, no build step)
- Database: PostgreSQL 16 with SQLAlchemy 2.0+
- Auth: Reverse proxy header injection (production), debug bypass (development)
- Containerization: Docker Compose

## Style and Conventions

**No Emojis**: No emojis anywhere in this codebase (code, comments, docs, commit messages).

**File Naming**: Use descriptive names that reflect purpose (e.g., `auth_middleware.py`, `key_service.py`). Do not use generic names like `utils.py` or `helpers.py`.

**File Size**: Prefer files with 400 lines or fewer when practical.

**Keep It Simple**: This is a small, focused app. Do not over-engineer. No unnecessary abstractions, no premature optimization, no features beyond what is explicitly requested.

**No Build Step for Frontend**: The frontend is plain HTML/CSS/JS served from `/static`. Do not introduce a build tool, bundler, or framework.

**Documentation Date-Time Stamping**: When creating markdown files, include date-time stamps either in the filename or as a header. Format: `YYYY-MM-DD` or `YYYY-MM-DD HH:MM`.

## Architecture

```
signup-app/
    app/
        main.py              # FastAPI app entrypoint, serves static files
        core/
            config.py        # Pydantic settings, env vars, feature flags
            middleware.py    # Auth middleware (header extraction + debug bypass)
            security.py      # Key generation, hashing
        models/
            user.py          # User SQLAlchemy model
            api_key.py       # APIKey SQLAlchemy model
        routes/
            health.py        # Health check endpoint
            users.py         # User info endpoints
            keys.py          # API key CRUD endpoints
        database.py          # Database connection, session management
    static/
        index.html           # Single-page frontend
        style.css            # Styles
        app.js               # Frontend logic
    docker-compose.yml       # PostgreSQL + app services
    Dockerfile               # Container build
    .env.example             # All configuration variables
    pyproject.toml           # Python dependencies (single source of truth)
```

## Authentication

### Production (Reverse Proxy)
The app sits behind a reverse proxy that handles authentication and injects a user identity header.

- Default header: `X-User-Email` (configurable via `AUTH_USER_HEADER`)
- Optional proxy secret validation via `PROXY_SECRET_HEADER` and `PROXY_SECRET`
- Requests without a valid auth header receive 401
- User records are auto-created on first authenticated request

### Development (Debug Mode)
- Set `DEBUG_MODE=true` in `.env`
- Falls back to `TEST_USER` env var (default: `test@test.com`)
- Proxy secret validation is skipped

### Middleware Order
```
Request -> Auth -> Route
```

Auth middleware must:
1. Check proxy secret (if enabled and not debug mode)
2. Extract user email from configured header
3. Fall back to TEST_USER if debug mode
4. Return 401 if no user identified (production)
5. Set `request.state.user_email` for downstream use

## Database

- ORM: SQLAlchemy 2.0+ with async support
- Tables auto-created on startup via `create_all()` (Alembic to be added later)
- Connection string via `DATABASE_URL` env var
- No database-level CASCADE constraints for portability

## API Key Management

### Key Generation
- Format: `sk-<48 random hex chars>`
- Prefix `sk-` stored separately for display identification
- Full key shown exactly once at creation, then only the prefix is available
- Storage: SHA-256 hash of the full key; plaintext is never persisted

### Key Verification
- Hash the provided key, compare against stored hash
- Update `last_used_at` on successful verification (if feature flag enabled)
- Check `is_active` and `expires_at` (if feature flag enabled)

## Feature Flags

Feature flags control progressive functionality. All flags default to `false`.

```env
FEATURE_KEY_LAST_USED=false        # Track last used timestamp
FEATURE_KEY_EXPIRATION=false       # Allow setting key expiry
FEATURE_KEY_RATE_LIMIT=false       # Per-key rate limits
FEATURE_KEY_SCOPES=false           # Per-key permission scopes
```

When a feature flag is disabled:
- The corresponding database columns still exist but are unused
- API responses omit the related fields
- Frontend hides the related UI elements

## Configuration

All configuration is via environment variables, loaded through Pydantic `BaseSettings`.

```env
# Core
DEBUG_MODE=false
DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/signup_app

# Auth
AUTH_USER_HEADER=X-User-Email
TEST_USER=test@test.com
PROXY_SECRET_HEADER=X-Proxy-Secret
PROXY_SECRET=
FEATURE_PROXY_SECRET_ENABLED=false

# Feature Flags
FEATURE_KEY_LAST_USED=false
FEATURE_KEY_EXPIRATION=false
FEATURE_KEY_RATE_LIMIT=false
FEATURE_KEY_SCOPES=false
```

## API Endpoints

### No Auth Required
- `GET /api/health` — returns `{"status": "ok"}`
- `POST /api/keys/verify` — verify an API key (for downstream services)

### Auth Required
- `GET /api/me` — current user info
- `GET /api/keys` — list user's API keys (masked, never full key)
- `POST /api/keys` — create new key (returns full key once in response)
- `PATCH /api/keys/{id}` — update key name or settings
- `DELETE /api/keys/{id}` — revoke key (soft delete: sets `is_active=false`, `revoked_at`)

## Development Commands

### Quick Start
```bash
docker compose up -d postgres
cp .env.example .env  # edit as needed
python -m uvicorn app.main:app --reload --port 8000
```

### Docker
```bash
docker compose up --build
```

### Testing
```bash
pytest tests/
```

## Common Issues

1. **No auth header in production**: Ensure reverse proxy is configured to inject `AUTH_USER_HEADER`
2. **Can't connect to database**: Check `DATABASE_URL` and that PostgreSQL is running
3. **Key shown as None after creation**: Full key is only returned in the POST response; it is not stored

## Critical Restrictions

- **NEVER store API keys in plaintext** — always hash with SHA-256
- **NEVER return full API keys after creation** — only the prefix is shown in list views
- **NEVER skip auth middleware for key management endpoints**
- **NEVER introduce a frontend build step** — keep it plain HTML/CSS/JS
- **NEVER add dependencies without adding them to pyproject.toml**
