"""GET /api/models: the models a new key may use, for the create-key picker."""
from fastapi import APIRouter, HTTPException, Request

from app.core.config import get_settings
from app.core.litellm_client import LiteLLMClient
from app.core.model_access import models_for
from app.core.team_membership import require_member

router = APIRouter(prefix="/api")


@router.get("/models")
async def list_models(request: Request, team_id: str | None = None):
    """With teams, the models ``team_id`` (one of the caller's teams) allows;
    without, every model on the proxy. Key creation checks the same rules."""
    s = get_settings()
    client = LiteLLMClient(s)
    if not s.FEATURE_TEAMS_ENABLED:
        return {"team_id": None, "models": await models_for(client, None, s.team_word())}

    if not team_id:
        raise HTTPException(status_code=400, detail=f"Choose a {s.team_word()}")
    # Read-only, so the sign-in's groups (as /api/me), not a token refresh.
    await require_member(request, client, team_id, refresh_claims=False)
    return {"team_id": team_id, "models": await models_for(client, team_id, s.team_word())}
