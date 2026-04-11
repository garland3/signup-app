"""In-memory server-side session store.

Replaces Starlette's stateless signed-cookie SessionMiddleware with a
server-side store so that sessions can actually be revoked (e.g. on
logout), and so that session contents are never exposed in the cookie.

The cookie holds only an opaque session ID (cryptographically random).
Session data lives in a process-local dict, keyed by session ID, with
both an absolute and an idle expiry. This works fine for a single
replica; for horizontal scale-out, swap this for a Redis-backed store
behind the same interface.
"""
from __future__ import annotations

import secrets
import time
from typing import Any

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class _SessionData:
    __slots__ = ("data", "created_at", "last_seen_at")

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.created_at: float = time.time()
        self.last_seen_at: float = self.created_at


class SessionStore:
    """Process-local session dict with absolute and idle expiry."""

    def __init__(self, max_age: int, idle_timeout: int) -> None:
        self._sessions: dict[str, _SessionData] = {}
        self._max_age = max_age
        self._idle_timeout = idle_timeout

    def _is_expired(self, s: _SessionData, now: float) -> bool:
        if (now - s.created_at) > self._max_age:
            return True
        if (now - s.last_seen_at) > self._idle_timeout:
            return True
        return False

    def _sweep(self, now: float) -> None:
        expired = [sid for sid, s in self._sessions.items() if self._is_expired(s, now)]
        for sid in expired:
            self._sessions.pop(sid, None)

    def load(self, sid: str) -> dict[str, Any] | None:
        now = time.time()
        self._sweep(now)
        s = self._sessions.get(sid)
        if s is None:
            return None
        if self._is_expired(s, now):
            self._sessions.pop(sid, None)
            return None
        s.last_seen_at = now
        return s.data

    def save(self, sid: str | None, data: dict[str, Any]) -> str:
        """Persist ``data`` and return the session ID to set on the cookie."""
        if sid and sid in self._sessions:
            self._sessions[sid].data = data
            self._sessions[sid].last_seen_at = time.time()
            return sid
        new_sid = secrets.token_urlsafe(32)
        rec = _SessionData()
        rec.data = data
        self._sessions[new_sid] = rec
        return new_sid

    def revoke(self, sid: str) -> None:
        self._sessions.pop(sid, None)


class InMemorySessionMiddleware:
    """ASGI middleware exposing ``request.session`` backed by SessionStore.

    Interface is compatible with Starlette's SessionMiddleware from the
    route handler's point of view: ``request.session`` is a dict that can
    be read/written and ``request.session.clear()`` empties it (which
    this middleware treats as "revoke session").
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        cookie_name: str = "signup_session",
        max_age: int = 60 * 60 * 24 * 7,
        idle_timeout: int = 60 * 60 * 24,
        https_only: bool = True,
        same_site: str = "lax",
        path: str = "/",
        store: SessionStore | None = None,
    ) -> None:
        self.app = app
        self.cookie_name = cookie_name
        self.max_age = max_age
        self.https_only = https_only
        self.same_site = same_site
        self.path = path
        self.store = store or SessionStore(max_age, idle_timeout)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        cookie_sid = request.cookies.get(self.cookie_name)

        loaded: dict[str, Any] | None = None
        if cookie_sid:
            loaded = self.store.load(cookie_sid)

        had_initial_session = loaded is not None
        session_dict: dict[str, Any] = loaded if loaded is not None else {}
        scope["session"] = session_dict

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                self._finalize(headers, cookie_sid, had_initial_session, session_dict)
            await send(message)

        await self.app(scope, receive, send_wrapper)

    def _finalize(
        self,
        headers: MutableHeaders,
        cookie_sid: str | None,
        had_initial_session: bool,
        session_dict: dict[str, Any],
    ) -> None:
        if session_dict:
            new_sid = self.store.save(cookie_sid, session_dict)
            # Issue a new cookie if we don't already have one, or if the
            # server-side store issued a fresh SID.
            if new_sid != cookie_sid:
                self._set_cookie(headers, value=new_sid, max_age=self.max_age)
            return

        # Session is empty on the way out. If a session existed on the
        # way in, revoke it and clear the cookie.
        if had_initial_session and cookie_sid:
            self.store.revoke(cookie_sid)
            self._set_cookie(headers, value="", max_age=0)

    def _set_cookie(
        self, headers: MutableHeaders, *, value: str, max_age: int
    ) -> None:
        parts = [f"{self.cookie_name}={value}"]
        parts.append(f"Path={self.path}")
        parts.append(f"Max-Age={max_age}")
        parts.append("HttpOnly")
        parts.append(f"SameSite={self.same_site.capitalize()}")
        if self.https_only:
            parts.append("Secure")
        headers.append("set-cookie", "; ".join(parts))
