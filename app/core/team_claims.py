"""Teams from the identity provider (TEAM_SOURCE=claims).

A person's teams are the groups in their sign-in claims (GROUPS_FIELD) that
start with TEAM_GROUP_PREFIX and name a LiteLLM team, by team_alias or team_id.
Operators create the teams; the identity provider decides who is in them.

Groups are read at sign-in, and again before a key is made: the app refreshes
the person's tokens (refresh-token grant) and re-reads the claims, so someone
taken out of a group can't make keys in it with an old session.

Anything uncertain fails closed: a refresh the provider refuses, a refreshed
token for someone else, no refresh token (unless TEAM_CLAIMS_MAX_AGE_SECONDS
allows the sign-in's groups), or a token whose groups were left out because
there are too many of them (Entra ID's "groups overage").

This app doesn't remove people from LiteLLM teams, or delete their keys, when
they leave a group: it only acts when someone uses it. A reconciler outside it
must (README: "Teams from the identity provider").
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


OVERAGE_DETAIL = (
    "Your sign-in doesn't list your groups (there are too many), so your teams can't be "
    "checked. Ask your administrators to send only the groups this app uses."
)


def groups_overage(claims: dict, settings: Settings) -> bool:
    """Whether the token left the groups out because there are too many.

    Entra ID then omits the claim and points to Microsoft Graph instead
    (``_claim_names`` naming the claim), or sets ``hasgroups`` in implicit-flow
    tokens. The groups are unknown, not empty.
    """
    names = claims.get("_claim_names")
    if isinstance(names, dict) and settings.GROUPS_FIELD in names:
        return True
    return claims.get("hasgroups") in (True, "true")


def store_groups(session: dict, claims: dict, settings: Settings) -> list[str]:
    """Keep the claims' team groups in the session, with when and whether they're known."""
    groups = groups_from_claims(claims, settings)
    session["groups"] = groups
    session["groups_at"] = time.time()
    session["groups_overage"] = groups_overage(claims, settings)
    return groups


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
    offline_access scope), the person must sign in again (401), unless the
    groups from sign-in are younger than TEAM_CLAIMS_MAX_AGE_SECONDS. A token
    that leaves the groups out (overage) is refused (403).
    """
    from app.routes.auth import claims_from_tokens  # avoid an import cycle

    s = get_settings()
    session = request.session
    refresh_token = session.get("refresh_token")
    if not refresh_token:
        age = time.time() - float(session.get("groups_at") or 0)
        if s.TEAM_CLAIMS_MAX_AGE_SECONDS and age <= s.TEAM_CLAIMS_MAX_AGE_SECONDS:
            if session.get("groups_overage"):
                raise HTTPException(status_code=403, detail=OVERAGE_DETAIL)
            return session_groups(request)
        audit("team_claims_stale", user=session.get("user_email"), age=int(age))
        raise HTTPException(status_code=401, detail="Sign in again to confirm your teams")
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
    groups = store_groups(session, claims, s)
    if tokens.get("refresh_token"):
        session["refresh_token"] = tokens["refresh_token"]  # rotated
    if session["groups_overage"]:
        audit("team_claims_overage", user=session.get("user_email"))
        raise HTTPException(status_code=403, detail=OVERAGE_DETAIL)
    return groups
