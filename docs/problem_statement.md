# Problem Statement (2026-03-09)

## Context

We run a LiteLLM proxy that provides a unified API gateway to multiple LLM providers (OpenAI, Anthropic, Google, etc.). LiteLLM has a built-in admin API for managing API keys, but it requires a master admin key and is not designed for end-user self-service.

We need a way for authenticated users to manage their own API keys without having access to the admin key or the LiteLLM admin dashboard.

## Problem

There is no user-facing interface for API key management. Today, only administrators can create, view, or revoke keys through LiteLLM's admin API or dashboard. Users who need keys must request them manually, creating a bottleneck.

We need a self-service web application where users can:

1. **See their existing API keys** -- name, masked prefix, creation date, spend, and status
2. **Create new API keys** -- with optional duration, budget limits, and model restrictions
3. **Delete API keys** -- permanently remove keys they no longer need

## Constraints

- **Authentication is handled by a reverse proxy** (e.g., nginx, AWS ALB, Caddy) that sits in front of the application. The proxy authenticates users and injects an `X-User-Email` header. The app trusts this header. This matches the pattern used by our other internal tools (atlas-ui-3).
- **No direct database management** -- the app does not own a database. All key storage and lifecycle management is delegated to the LiteLLM proxy via its admin API.
- **The app holds a single admin key** (`LITELLM_ADMIN_KEY`) server-side. This key is never exposed to users. All operations are scoped to the authenticated user via LiteLLM's `user_id` field.
- **Simple frontend** -- plain HTML/CSS/JS with no build step. The UI should be clean and minimal, similar to OpenAI's or Anthropic's API key management pages.
- **Debug mode** -- for local development without a reverse proxy, the app falls back to a configurable test user.

## Architecture

```
User Browser
    |
    v
Reverse Proxy (authenticates, injects X-User-Email)
    |
    v
Signup App (FastAPI) --- thin UI layer, auth middleware
    |
    v
LiteLLM Proxy (admin API) --- stores and manages keys
```

The signup app is a thin proxy: it authenticates the user via the header, then forwards key operations to LiteLLM with the admin key attached. It translates LiteLLM's admin API responses into a simple format for the frontend.

## Success Criteria

- Users can create, view, and delete their own API keys through a web UI
- Keys are shown once at creation, then only the masked prefix is visible
- Users can only see and manage their own keys (scoped by email)
- The app works behind a reverse proxy in production and standalone in development
- No database to manage -- LiteLLM is the single source of truth for keys
- A mock LiteLLM server is included for development and testing without a real LiteLLM instance
