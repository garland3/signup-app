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
        # The teams the user belongs to, for the team-scoped key selector.
        # /api/me must stay resilient, so an upstream failure doesn't fail
        # the whole call; instead teams_unavailable tells the UI the list is
        # unknown (not empty), so it can show an error rather than treat the
        # user as having no teams. Key creation re-checks membership itself
        # and fails closed.
        teams, ok = await _load_user_teams(request.state.user_id)
        payload["teams"] = teams
        payload["teams_unavailable"] = not ok
    return payload


async def _load_user_teams(user_id: str) -> tuple[list[dict], bool]:
    try:
        client = LiteLLMClient(get_settings())
        teams = await client.list_teams(user_id=user_id)
    except Exception as e:
        logger.warning("Could not load teams for %s: %s", user_id, e)
        return [], False
    out = []
    for t in teams:
        team_id = t.get("team_id")
        if team_id:
            out.append(
                {"team_id": team_id, "team_alias": t.get("team_alias") or team_id}
            )
    return out, True
