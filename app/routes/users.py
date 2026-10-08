import logging

from fastapi import APIRouter, Request

from app.core.config import get_settings
from app.core.litellm_client import LiteLLMClient

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


@router.get("/me")
async def me(request: Request):
    s = get_settings()
    payload = {
        "email": request.state.user_email,
        "auth_mode": s.AUTH_MODE,
    }
    if s.FEATURE_TEAMS_ENABLED:
        # The teams the user already belongs to, for the team-scoped key
        # selector. /api/me must stay resilient, so an upstream failure
        # degrades to an empty list rather than failing the whole call.
        payload["teams"] = await _safe_user_teams(request.state.user_email)
    return payload


async def _safe_user_teams(user_email: str) -> list[dict]:
    try:
        client = LiteLLMClient(get_settings())
        teams = await client.list_teams(user_id=user_email)
    except Exception as e:
        logger.warning("Could not load teams for %s: %s", user_email, e)
        return []
    out = []
    for t in teams:
        team_id = t.get("team_id")
        if team_id:
            out.append(
                {"team_id": team_id, "team_alias": t.get("team_alias") or team_id}
            )
    return out
