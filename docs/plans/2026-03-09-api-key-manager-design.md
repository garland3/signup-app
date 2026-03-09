# API Key Manager - Design Document (2026-03-09)

## Overview

A signup/login app with PostgreSQL where users can manage API keys (create, view, revoke) similar to OpenAI or Anthropic's key management dashboards.

## Architecture

- **Backend**: FastAPI (Python), serves REST API and static frontend
- **Frontend**: Plain HTML/CSS/JS (no build step), served from `/static`
- **Database**: PostgreSQL 16
- **Auth**: Reverse proxy header injection in production, debug bypass for dev
- **Containerization**: Docker Compose (postgres + app)

```
Production:  User -> Reverse Proxy -> X-User-Email header -> FastAPI -> PostgreSQL
Development: User -> FastAPI (DEBUG_MODE=true, TEST_USER fallback) -> PostgreSQL
```

## Database Schema

### users
| Column | Type | Notes |
|--------|------|-------|
| id | UUID, PK | |
| email | VARCHAR(255), UNIQUE, NOT NULL | Populated from auth header |
| display_name | VARCHAR(255), nullable | |
| created_at | TIMESTAMP WITH TZ | |
| updated_at | TIMESTAMP WITH TZ | |

### api_keys
| Column | Type | Notes |
|--------|------|-------|
| id | UUID, PK | |
| user_id | UUID, FK -> users.id | |
| name | VARCHAR(255), NOT NULL | User-given label |
| prefix | VARCHAR(12), NOT NULL | First chars for display (e.g., `sk-abc1...`) |
| key_hash | VARCHAR(64), NOT NULL | SHA-256 hash of full key |
| created_at | TIMESTAMP WITH TZ | |
| last_used_at | TIMESTAMP WITH TZ, nullable | Feature flag: standard+ |
| expires_at | TIMESTAMP WITH TZ, nullable | Feature flag: full |
| rate_limit | INTEGER, nullable | Feature flag: full |
| scopes | JSONB, nullable | Feature flag: full |
| is_active | BOOLEAN, DEFAULT true | |
| revoked_at | TIMESTAMP WITH TZ, nullable | |

## Feature Flags (Progressive)

```env
# Level 1 (basic): create, list, revoke - always on
FEATURE_KEY_LAST_USED=false        # Level 2: track last used timestamp
FEATURE_KEY_EXPIRATION=false       # Level 3: allow setting expiry
FEATURE_KEY_RATE_LIMIT=false       # Level 3: per-key rate limits
FEATURE_KEY_SCOPES=false           # Level 3: per-key permission scopes
```

## API Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | /api/health | No | Health check |
| GET | /api/me | Yes | Current user info |
| GET | /api/keys | Yes | List user's keys (masked) |
| POST | /api/keys | Yes | Create key (returns full key once) |
| PATCH | /api/keys/{id} | Yes | Update key name/settings |
| DELETE | /api/keys/{id} | Yes | Revoke key |
| GET | /api/keys/{id}/verify | No | Verify a key is valid (for downstream) |

## Auth Flow

- Production: `AUTH_USER_HEADER` (default `X-User-Email`) set by reverse proxy
- Optional: `PROXY_SECRET` header validation
- Debug: `DEBUG_MODE=true` falls back to `TEST_USER` env var
- Auto-creates user record on first authenticated request

## API Key Format

- Prefix: `sk-`
- Format: `sk-<48 random hex chars>`
- Storage: SHA-256 hash only; full key shown once at creation
- Display: prefix stored separately for identification (e.g., `sk-a1b2...`)

## Frontend

Single-page plain HTML/CSS/JS:
- Key list table (name, prefix, created, last used, status)
- Create key modal with one-time display and copy button
- Revoke button per key
- Minimal, clean design
