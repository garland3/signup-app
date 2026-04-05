# Signup App

API key management UI backed by LiteLLM proxy. Authenticated users can create, view, and delete API keys through a clean web interface.

## Tech Stack

- **Backend:** FastAPI (Python 3.11+)
- **Frontend:** Plain HTML/CSS/JS (no build step)
- **Key Storage:** LiteLLM proxy (admin API)
- **Auth:** Reverse proxy header injection (production), debug bypass (development)

## Quick Start

```bash
# Start mock LiteLLM server
uv run uvicorn mocks.litellm_mock:app --port 4000 &

# Setup
cp .env.example .env
# Set LITELLM_ADMIN_KEY=sk-mock-admin-key in .env

uv sync --extra dev
uv run uvicorn app.main:app --reload --port 8000
```

Open http://localhost:8000 to access the key management UI.

## Docker

```bash
cp .env.example .env
docker compose up --build
```

## How It Works

The app is a thin UI layer over LiteLLM's key management API:

```
User -> Reverse Proxy -> Signup App -> LiteLLM Proxy
```

The app holds a single admin key (`LITELLM_ADMIN_KEY`) to manage keys on behalf of users. Users are scoped by email via LiteLLM's `user_id` field.

## Authentication

Two modes, selected via `AUTH_MODE` in `.env`:

### `AUTH_MODE=proxy` (default)

The app sits behind a reverse proxy that injects an `X-User-Email` header. In
development, set `DEBUG_MODE=true` to fall back to `TEST_USER`. Optionally set
`FEATURE_PROXY_SECRET_ENABLED=true` + `PROXY_SECRET=...` to require a shared
secret header from the proxy.

### `AUTH_MODE=oauth`

The app runs a standard OAuth 2.0 / OIDC authorization code flow. Configure:

```
AUTH_MODE=oauth
OAUTH_CLIENT_ID=...
OAUTH_CLIENT_SECRET=...
OAUTH_AUTHORIZE_URL=...
OAUTH_TOKEN_URL=...
OAUTH_USERINFO_URL=...
OAUTH_SCOPES=openid email profile
OAUTH_REDIRECT_URL=http://localhost:8000/api/auth/callback
OAUTH_EMAIL_FIELD=email
SESSION_SECRET=<random secret>
```

Unauthenticated users hit `GET /api/auth/login` to start the flow; the callback
lands on `GET /api/auth/callback`, which sets a signed session cookie. Log out
with `GET /api/auth/logout`. See `.env.example` for Google/GitHub examples.

#### Running behind a TLS-terminating proxy (Kubernetes)

By default the session cookie is marked `Secure` and only sent over HTTPS. If
TLS is terminated upstream (e.g. a Kubernetes ingress or load balancer) and the
app only sees plain HTTP traffic internally, set:

```
SESSION_COOKIE_SECURE=false
```

The cookie will still be signed and `HttpOnly`; the browser just won't require
HTTPS on the hop between itself and your ingress. Make sure the external URL
(the one users hit, and `OAUTH_REDIRECT_URL`) is still HTTPS.

## API Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/health` | No | Health check |
| GET | `/api/me` | Yes | Current user email |
| GET | `/api/keys` | Yes | List keys (masked) |
| POST | `/api/keys` | Yes | Create key (full key returned once) |
| PATCH | `/api/keys/{token}` | Yes | Update key settings |
| DELETE | `/api/keys/{token}` | Yes | Delete key |

## Mock LiteLLM Server

For development without a real LiteLLM instance, use the included mock:

```bash
uv run uvicorn mocks.litellm_mock:app --port 4000
```

Admin key: `sk-mock-admin-key`

## Tests

```bash
uv run --extra dev pytest tests/ -v
```
