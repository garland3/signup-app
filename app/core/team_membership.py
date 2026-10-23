"""Whether the caller is in a team: one check for key creation and for
GET /api/models, so what the picker offers and what creation accepts can't
drift apart."""
from __future__ import annotations

import logging

from fastapi import HTTPException, Request

from app.core.config import get_settings
from app.core.litellm_client import LiteLLMClient
from app.core.team_claims import overage_detail, refresh_groups, session_teams, teams_for_groups

logger = logging.getLogger(__name__)


async def require_member(
    request: Request, client: LiteLLMClient, team_id: str, *, refresh_claims: bool
) -> None:
    """A 400 unless ``team_id`` is one of the caller's teams; 502 if LiteLLM
    can't say. With TEAM_SOURCE=claims the teams come from the groups: the
    identity provider's current ones when ``refresh_claims`` (before a key is
    made), else the sign-in's (read-only views). Group overage is a 403."""
    s = get_settings()
    claims = s.TEAM_SOURCE == "claims"
    groups = await refresh_groups(request) if claims and refresh_claims else None
    try:
        if not claims:
            teams = await client.list_teams(user_id=request.state.user_id)
        elif groups is not None:
            teams = await teams_for_groups(groups, client)
        else:
            teams = await session_teams(request, client)
    except Exception as e:
        logger.exception("LiteLLM list_teams failed: %s", e)
        raise HTTPException(status_code=502, detail="Upstream service error")
    if teams is None:  # the sign-in's token left the groups out
        raise HTTPException(status_code=403, detail=overage_detail(s))
    if team_id not in {t.get("team_id") for t in teams}:
        raise HTTPException(status_code=400, detail=f"Invalid {s.team_word()} for this user")
