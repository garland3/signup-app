"""Teams from the identity provider (TEAM_SOURCE=claims).

A person's teams are the groups in their sign-in claims (GROUPS_FIELD) that
start with TEAM_GROUP_PREFIX and name a LiteLLM team, by team_alias or team_id.
Operators create the teams; the identity provider decides who is in them.

Groups are read at sign-in, and again before a key is made, changed or
unblocked: the app refreshes the person's tokens (refresh-token grant) and
re-reads the claims, so someone taken out of a group can't make or widen keys
in it with an old session.

Anything uncertain fails closed: a refresh the provider refuses, a refreshed
token for someone else, no refresh token (unless the sign-in's groups are
recent: SIGN_IN_GRACE_SECONDS or TEAM_CLAIMS_MAX_AGE_SECONDS), or a token whose
groups were left out because there are too many of them (Entra ID's "groups
overage").

This app doesn't remove people from LiteLLM teams, or delete their keys, when
they leave a group: it only acts when someone uses it. A reconciler outside it
must (README: "Teams from the identity provider").
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time

import httpx
from fastapi import HTTPException, Request

from app.core.audit import audit
from app.core.config import Settings, get_settings
from app.core.litellm_client import LiteLLMClient

logger = logging.getLogger(__name__)

TEAMS_TTL_SECONDS = 60
# A group naming no team (yet) reloads the team list, but at most this often:
# a person with a stale group would otherwise reload it on every request.
FORCED_RELOAD_SECONDS = 10
_teams_cache: tuple[float, dict[str, dict]] | None = None  # (loaded at, name -> team)
_last_forced_reload = 0.0

# Token refreshes in flight, by a digest of the refresh token used: concurrent
# requests from one session (a double click, a retry) share one refresh.
# Sending the same refresh token twice would trip the provider's reuse
# detection (Keycloak's "Revoke Refresh Token"), refusing the second and
# possibly ending the whole sign-in session.
_inflight: dict[str, asyncio.Task] = {}

_now = time.time  # the clock for sign-in ages; tests replace it

# Without a refresh token, groups from a sign-in this recent are as fresh as a
# refresh would give, so signing in again always lets a key be made (otherwise
# a provider that issues no refresh token would loop: 401, sign in, 401, ...).
SIGN_IN_GRACE_SECONDS = 60


class RefreshRefused(Exception):
    """The provider says the refresh token is no good (``invalid_grant``)."""


def reset_cache() -> None:
    """For tests."""
    global _teams_cache, _last_forced_reload
    _teams_cache = None
    _last_forced_reload = 0.0
    _inflight.clear()


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
    # hasgroups is about the groups claim only, not app roles (GROUPS_FIELD=roles).
    return settings.GROUPS_FIELD == "groups" and claims.get("hasgroups") in (True, "true")


def store_groups(session: dict, claims: dict, settings: Settings) -> list[str]:
    """Keep the claims' team groups in the session, with when and whether they're known."""
    groups = groups_from_claims(claims, settings)
    session["groups"] = groups
    session["groups_at"] = _now()
    session["groups_overage"] = groups_overage(claims, settings)
    return groups


def groups_from_claims(claims: dict, settings: Settings) -> list[str]:
    """The claim's group names that have the team prefix (a list or one string;
    anything else counts as no groups)."""
    value = claims.get(settings.GROUPS_FIELD)
    if isinstance(value, str):
        value = [value]
    elif not isinstance(value, list):
        value = []
    prefix = settings.TEAM_GROUP_PREFIX
    return sorted({g for g in value if isinstance(g, str) and g.startswith(prefix)})


async def _teams_by_name(client: LiteLLMClient, *, refresh: bool = False) -> dict[str, dict]:
    """Teams by name: their team_alias or their team_id.

    A name that could mean more than one team (an alias two teams share, or one
    team's alias and another's ID) means none, and is logged: whoever can set a
    team's alias mustn't be able to take another team's group members.
    """
    global _teams_cache
    now = time.monotonic()
    if refresh or _teams_cache is None or now - _teams_cache[0] > TEAMS_TTL_SECONDS:
        candidates: dict[str, dict[str, dict]] = {}  # name -> {team_id: team}
        for t in await client.list_all_teams():
            team_id = t.get("team_id")
            if not (isinstance(team_id, str) and team_id):
                continue
            for name in {team_id, t.get("team_alias")}:
                if isinstance(name, str) and name:
                    candidates.setdefault(name, {})[team_id] = t
        index = {}
        for name, teams in candidates.items():
            if len(teams) == 1:
                index[name] = next(iter(teams.values()))
            else:
                logger.warning(
                    "Group name %s could mean teams %s; it means none until that's resolved",
                    name, ", ".join(sorted(teams)),
                )
        _teams_cache = (now, index)
    return _teams_cache[1]


async def teams_for_groups(groups: list[str], client: LiteLLMClient) -> list[dict]:
    """[{team_id, team_alias}] for the groups that name a LiteLLM team."""
    global _last_forced_reload
    index = await _teams_by_name(client)
    if any(g not in index for g in groups):  # a team created since the last load?
        now = time.monotonic()
        if now - _last_forced_reload >= FORCED_RELOAD_SECONDS:
            _last_forced_reload = now
            index = await _teams_by_name(client, refresh=True)
    out = []
    for g in groups:
        team = index.get(g)
        if team and team.get("team_id"):
            out.append({"team_id": team["team_id"], "team_alias": team.get("team_alias") or g})
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
    groups from sign-in are younger than SIGN_IN_GRACE_SECONDS or
    TEAM_CLAIMS_MAX_AGE_SECONDS, whichever is longer. A token that leaves the
    groups out (overage) is refused (403). Concurrent requests share one
    refresh (_refresh_shared).
    """
    s = get_settings()
    session = request.session
    refresh_token = session.get("refresh_token")
    if not refresh_token:
        age = _now() - float(session.get("groups_at") or 0)
        if age <= max(SIGN_IN_GRACE_SECONDS, s.TEAM_CLAIMS_MAX_AGE_SECONDS):
            if session.get("groups_overage"):
                audit("team_claims_overage", user=session.get("user_email"))
                raise HTTPException(status_code=403, detail=OVERAGE_DETAIL)
            return session_groups(request)
        audit("team_claims_stale", user=session.get("user_email"), age=int(age))
        raise HTTPException(status_code=401, detail="Sign in again to confirm your teams")
    try:
        tokens, claims = await _refresh_shared(refresh_token, s, session)
    except RefreshRefused:
        if session.get("refresh_token") != refresh_token:
            # A newer sign-in (or a logout) replaced the session meanwhile: the
            # refused token was the old one, so the session hasn't ended.
            raise HTTPException(status_code=401, detail="Your sign-in changed; sign in again")
        user = session.get("user_email")
        session.clear()
        audit("oauth_refresh_refused", user=user)
        raise HTTPException(status_code=401, detail="Your sign-in has ended; sign in again")
    # A sign-in or logout while the refresh ran replaced the session (the refresh
    # task writes back only the rotated token): leave the newer state alone.
    if session.get("refresh_token") not in {refresh_token, tokens.get("refresh_token")} - {None}:
        raise HTTPException(status_code=401, detail="Your sign-in changed; sign in again")
    # The refreshed claims must still be this person.
    field = s.OAUTH_USER_ID_FIELD or s.OAUTH_EMAIL_FIELD
    expected = session.get("user_id") if s.OAUTH_USER_ID_FIELD else session.get("user_email")
    if claims.get(field) != expected:
        user = session.get("user_email")
        session.clear()
        audit("oauth_refresh_other_user", user=user, field=field)
        raise HTTPException(status_code=401, detail="Your sign-in has ended; sign in again")
    groups = store_groups(session, claims, s)  # the rotated token: written by the task
    if session["groups_overage"]:
        audit("team_claims_overage", user=session.get("user_email"))
        raise HTTPException(status_code=403, detail=OVERAGE_DETAIL)
    return groups


async def _refresh_shared(refresh_token: str, s: Settings, session: dict) -> tuple[dict, dict]:
    """(tokens, claims) for a refresh token, refreshing it only once.

    The refresh runs as a task of its own, which every request for the same
    refresh token awaits (shielded). A request that goes away (its client
    disconnected) doesn't cancel it, and the task itself writes the rotated
    refresh token back to the session, so the provider's rotation isn't lost.
    Each request then checks the claims and updates its own session.

    Nothing is kept once the task is done: a request reads the session's
    refresh token and joins the task without yielding in between, and the task
    writes the rotated token and leaves _inflight without yielding either. So a
    request either joins a running refresh or reads the rotated token; it can't
    send the old one again.
    """
    key = hashlib.sha256(refresh_token.encode()).hexdigest()
    task = _inflight.get(key)
    if task is None:
        task = asyncio.create_task(_refresh_task(key, refresh_token, s, session))
        task.add_done_callback(_retrieve_exception)
        _inflight[key] = task
    return await asyncio.shield(task)


def _retrieve_exception(task: asyncio.Task) -> None:
    """Mark a failed refresh's exception as seen, for when no request awaits it."""
    if not task.cancelled():
        task.exception()


async def _refresh_task(key: str, refresh_token: str, s: Settings, session: dict) -> tuple[dict, dict]:
    try:
        tokens, claims = await _refresh(refresh_token, s)
        # The session dict is the store's own (the in-memory store hands it out by
        # reference), so this write is kept even if the request that started the
        # task is gone. A store that copies sessions would need its own write here.
        if tokens.get("refresh_token") and session.get("refresh_token") == refresh_token:
            session["refresh_token"] = tokens["refresh_token"]  # rotated
        return tokens, claims
    finally:
        _inflight.pop(key, None)


async def _refresh(refresh_token: str, s: Settings) -> tuple[dict, dict]:
    """One refresh-token grant: (tokens, claims).

    Only ``invalid_grant`` means the refresh token or the person is gone
    (RefreshRefused, which ends the session). Anything else the token endpoint
    refuses (``invalid_client`` after a secret rotation, a misconfiguration) is
    the app's problem, not the person's: logged, and 502.
    """
    from app.routes.auth import claims_from_tokens  # avoid an import cycle

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
        try:
            body = r.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            body = None  # not JSON, or not an object: the endpoint isn't answering as OAuth
        if r.status_code >= 400 or body is None:
            error = body.get("error") if body else None
            if r.status_code in (400, 401) and error == "invalid_grant":
                raise RefreshRefused()
            # Never the raw body: a success the app can't read may still hold tokens
            # (a form-encoded token response, say). OAuth's error fields only.
            if body is not None:
                detail = f"error_description={str(body.get('error_description'))[:200]!r}"
            else:
                detail = f"not a JSON object: {r.headers.get('content-type')!r}, {len(r.content)} bytes"
            logger.error("Token refresh failed (%s, %s): %s", r.status_code, error, detail)
            audit("oauth_refresh_error", status=r.status_code, error=error)
            raise HTTPException(status_code=502, detail="Could not check your teams; try again")
        tokens = body
        if s.OAUTH_CLAIMS_SOURCE == "id_token" and not tokens.get("id_token"):
            logger.error("Token refresh returned no ID token (OAUTH_CLAIMS_SOURCE=id_token)")
            raise HTTPException(
                status_code=502,
                detail="Your sign-in provider returned no ID token when refreshing; "
                "ask your administrators",
            )
        claims = await claims_from_tokens(tokens, s, http, nonce=None)
    return tokens, claims
