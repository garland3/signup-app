"""GET /api/models: the models a new key may use, for the create-key picker."""
import logging

from fastapi import APIRouter, HTTPException, Request

from app.core.config import get_settings
from app.core.litellm_client import LiteLLMClient
from app.core.model_access import allowed_models
from app.core.team_claims import overage_detail, session_teams

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


def _upstream_error(op: str, exc: Exception) -> HTTPException:
    logger.exception("LiteLLM %s failed: %s", op, exc)
    return HTTPException(status_code=502, detail="Upstream service error")


@router.get("/models")
async def list_models(request: Request, team_id: str | None = None):
    """With teams, the models ``team_id`` (one of the caller's teams) allows;
    without, every model on the proxy. Key creation checks the same rules."""
    s = get_settings()
    client = LiteLLMClient(s)
    if not s.FEATURE_TEAMS_ENABLED:
        try:
            return {"team_id": None, "models": await allowed_models(client, None)}
        except Exception as e:
            raise _upstream_error("list_models", e)

    if not team_id:
        raise HTTPException(status_code=400, detail=f"Choose a {s.team_word()}")
    try:
        # Read-only, so the sign-in's groups (as /api/me), not a token refresh.
        if s.TEAM_SOURCE == "claims":
            teams = await session_teams(request, client)
        else:
            teams = await client.list_teams(user_id=request.state.user_id)
    except Exception as e:
        raise _upstream_error("list_teams", e)
    if teams is None:  # groups left out of the token: as for key creation
        raise HTTPException(status_code=403, detail=overage_detail(s))
    if team_id not in {t.get("team_id") for t in teams}:
        raise HTTPException(status_code=400, detail=f"Invalid {s.team_word()} for this user")
    try:
        team = await client.get_team(team_id)
    except Exception as e:
        raise _upstream_error("get_team", e)
    if team is None:  # removed since the membership check: offer nothing
        raise HTTPException(status_code=400, detail=f"Invalid {s.team_word()} for this user")
    try:
        models = await allowed_models(client, team)
    except Exception as e:
        raise _upstream_error("list_models", e)
    return {"team_id": team_id, "models": models}
