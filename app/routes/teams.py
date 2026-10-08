import logging

import httpx
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel

from app.core.audit import audit
from app.core.config import get_settings
from app.core.litellm_client import LiteLLMClient

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


def _get_client() -> LiteLLMClient:
    return LiteLLMClient(get_settings())


def _upstream_error(op: str, exc: Exception) -> HTTPException:
    """Log the real reason and return a generic 502 to the client.

    Mirrors the helper in keys.py / dashboard.py so upstream internals never
    leak to end users.
    """
    logger.exception("LiteLLM %s failed: %s", op, exc)
    return HTTPException(status_code=502, detail="Upstream service error")


def _require_teams_enabled() -> None:
    if not get_settings().FEATURE_TEAMS_ENABLED:
        # 404 (not 403) so the feature is invisible when disabled.
        raise HTTPException(status_code=404, detail="Not found")


def _team_summary(team: dict) -> dict:
    """Reduce a LiteLLM team record to the fields the frontend needs."""
    team_id = team.get("team_id", "")
    return {
        "team_id": team_id,
        "team_alias": team.get("team_alias") or team_id,
    }


class JoinTeamRequest(BaseModel):
    team_id: str


@router.get("/teams/available")
async def list_available_teams(request: Request):
    """Teams the current user may join, excluding ones already joined."""
    _require_teams_enabled()
    client = _get_client()
    user_email = request.state.user_email
    try:
        available = await client.list_available_teams(user_id=user_email)
        joined = await client.list_teams(user_id=user_email)
    except Exception as e:
        raise _upstream_error("list_available_teams", e)

    joined_ids = {t.get("team_id") for t in joined}
    return [
        _team_summary(t)
        for t in available
        if t.get("team_id") and t.get("team_id") not in joined_ids
    ]


@router.post("/teams/join", status_code=200)
async def join_team(body: JoinTeamRequest, request: Request):
    """Self-service: add the CURRENT user to a team they are entitled to.

    The target is always ``request.state.user_email`` (never a client-supplied
    email), the team must be in the caller's own available set, and the role is
    pinned to "user" so a user cannot grant themselves team-admin.
    """
    _require_teams_enabled()
    client = _get_client()
    user_email = request.state.user_email
    team_id = body.team_id

    try:
        joined = await client.list_teams(user_id=user_email)
    except Exception as e:
        raise _upstream_error("list_teams", e)
    if team_id in {t.get("team_id") for t in joined}:
        raise HTTPException(
            status_code=409, detail="You are already a member of this team."
        )

    try:
        available = await client.list_available_teams(user_id=user_email)
    except Exception as e:
        raise _upstream_error("list_available_teams", e)
    if team_id not in {t.get("team_id") for t in available}:
        # The caller is not entitled to join this team.
        raise HTTPException(status_code=403, detail="Team not available to join")

    # Ensure the user exists in LiteLLM ("add or create") before adding them.
    try:
        await client.ensure_user(user_email)
    except Exception as e:
        raise _upstream_error("ensure_user", e)

    try:
        await client.add_team_member(team_id, user_email, role="user")
    except httpx.HTTPStatusError as e:
        status = e.response.status_code if e.response is not None else None
        text = (e.response.text if e.response is not None else "").lower()
        if status == 400 and "already" in text:
            raise HTTPException(
                status_code=409,
                detail="You are already a member of this team.",
            )
        raise _upstream_error("team_member_add", e)
    except Exception as e:
        raise _upstream_error("team_member_add", e)

    audit("team_self_join", user=user_email, team_id=team_id)
    return {"status": "ok", "team_id": team_id}
