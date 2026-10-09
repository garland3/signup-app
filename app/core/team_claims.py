"""Teams from the identity provider (TEAM_SOURCE=claims).

A person's teams are the groups in their sign-in claims (GROUPS_FIELD) that
start with TEAM_GROUP_PREFIX and name a LiteLLM team, by team_alias or team_id.
Operators create the teams; the identity provider decides who is in them.

Groups are read at sign-in, and again before a key is made: the app refreshes
the person's tokens (refresh-token grant) and re-reads the claims, so someone
taken out of a group can't make keys in it with an old session.
"""
from __future__ import annotations

import logging
import time

import httpx
from fastapi import HTTPException, Request

from app.core.audit import audit
from app.core.config import Settings, get_settings
from app.core.litellm_client import LiteLLMClient

logger = logging.getLogger(__name__)

TEAMS_TTL_SECONDS = 60
_teams_cache: tuple[float, dict[str, dict]] | None = None  # (loaded at, name -> team)


def reset_cache() -> None:
    """For tests."""
    global _teams_cache
    _teams_cache = None


def groups_from_claims(claims: dict, settings: Settings) -> list[str]:
    """The claim's group names that have the team prefix (a list or one string)."""
    value = claims.get(settings.GROUPS_FIELD) or []
    if isinstance(value, str):
        value = [value]
    prefix = settings.TEAM_GROUP_PREFIX
    return sorted({g for g in value if isinstance(g, str) and g.startswith(prefix)})


async def _teams_by_name(client: LiteLLMClient, *, refresh: bool = False) -> dict[str, dict]:
    global _teams_cache
    now = time.monotonic()
    if refresh or _teams_cache is None or now - _teams_cache[0] > TEAMS_TTL_SECONDS:
        index: dict[str, dict] = {}
        for t in await client.list_all_teams():
            for name in (t.get("team_alias"), t.get("team_id")):
                if isinstance(name, str) and name:
                    index.setdefault(name, t)
        _teams_cache = (now, index)
    return _teams_cache[1]


async def teams_for_groups(groups: list[str], client: LiteLLMClient) -> list[dict]:
    """[{team_id, team_alias}] for the groups that name a LiteLLM team."""
    index = await _teams_by_name(client)
    if any(g not in index for g in groups):  # a team created since the last load
        index = await _teams_by_name(client, refresh=True)
    out = []
    for g in groups:
        team = index.get(g)
        if team and team.get("team_id"):
            out.append({"team_id": team["team_id"], "team_alias": g})
    return out


def session_groups(request: Request) -> list[str]:
    session = getattr(request, "session", None) or {}
    return list(session.get("groups") or [])


async def refresh_groups(request: Request) -> list[str]:
    """Refresh the person's tokens and return their current team groups.

    Fails closed: when the provider refuses the refresh (the sign-in session
    ended, the person was disabled), the app's session ends too (401). With
    no refresh token (the provider issued none; Entra ID needs the
    offline_access scope), the groups from sign-in are used.
    """
    from app.routes.auth import claims_from_tokens  # avoid an import cycle

    s = get_settings()
    session = request.session
    refresh_token = session.get("refresh_token")
    if not refresh_token:
        return session_groups(request)
    async with httpx.AsyncClient(timeout=10.0) as http:
        try:
            r = await http.post(
                s.OAUTH_TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": s.OAUTH_CLIENT_ID,
                    "client_secret": s.OAUTH_CLIENT_SECRET,
                },
                headers={"Accept": "application/json"},
            )
        except httpx.RequestError:
            logger.exception("Token refresh transport error")
            raise HTTPException(status_code=502, detail="Could not check your teams; try again")
        if r.status_code in (400, 401):
            user = session.get("user_email")
            session.clear()
            audit("oauth_refresh_refused", user=user, status=r.status_code)
            raise HTTPException(status_code=401, detail="Your sign-in has ended; sign in again")
        if r.status_code >= 400:
            logger.error("Token refresh failed: %s", r.text)
            raise HTTPException(status_code=502, detail="Could not check your teams; try again")
        tokens = r.json()
        claims = await claims_from_tokens(tokens, s, http, nonce=None)
    # The refreshed claims must still be this person.
    field = s.OAUTH_USER_ID_FIELD or s.OAUTH_EMAIL_FIELD
    expected = session.get("user_id") if s.OAUTH_USER_ID_FIELD else session.get("user_email")
    if claims.get(field) != expected:
        session.clear()
        raise HTTPException(status_code=401, detail="Your sign-in has ended; sign in again")
    groups = groups_from_claims(claims, s)
    session["groups"] = groups
    if tokens.get("refresh_token"):
        session["refresh_token"] = tokens["refresh_token"]  # rotated
    return groups
