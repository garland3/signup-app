"""Which models a key may use: the models its team (project) allows.

A LiteLLM team's ``models`` list decides it. An empty list, or one containing
``all-proxy-models``, means every model on the proxy. ``no-default-models``
means none. Otherwise it's the listed names. Without teams, every model on
the proxy is allowed.
"""
from __future__ import annotations

import fnmatch
import logging
import time

from fastapi import HTTPException

from app.core.litellm_client import LiteLLMClient

logger = logging.getLogger(__name__)

ALL_PROXY_MODELS = "all-proxy-models"
NO_DEFAULT_MODELS = "no-default-models"
PROXY_MODELS_TTL_SECONDS = 60

_proxy_models_cache: tuple[float, list[str]] | None = None


def reset_cache() -> None:
    """For tests."""
    global _proxy_models_cache
    _proxy_models_cache = None


async def proxy_models(client: LiteLLMClient) -> list[str]:
    """Every model on the proxy, cached for a minute."""
    global _proxy_models_cache
    now = time.monotonic()
    if _proxy_models_cache is None or now - _proxy_models_cache[0] > PROXY_MODELS_TTL_SECONDS:
        _proxy_models_cache = (now, await client.list_models())
    return list(_proxy_models_cache[1])


def _team_list(team: dict) -> list[str]:
    models = team.get("models")
    return [m for m in models if isinstance(m, str) and m] if isinstance(models, list) else []


async def allowed_models(client: LiteLLMClient, team: dict | None) -> list[str]:
    """The models a key in ``team`` may use (``None``: no teams, so the proxy's)."""
    if team is None:
        return await proxy_models(client)
    listed = _team_list(team)
    if not listed or ALL_PROXY_MODELS in listed:
        return await proxy_models(client)
    return sorted({m for m in listed if m != NO_DEFAULT_MODELS})


def is_allowed(model: str, allowed: list[str]) -> bool:
    """Whether ``model`` is one of ``allowed``, or matches one of its wildcards
    (a team list's ``openai/*`` allows ``openai/gpt-4o``, as in LiteLLM).

    Anything else is refused, including an access-group name the team doesn't
    list: keys may only name what the project lists or a wildcard covers.
    """
    return any(
        model == a or ("*" in a and fnmatch.fnmatchcase(model, a)) for a in allowed
    )


async def models_for(client: LiteLLMClient, team_id: str | None, team_word: str) -> list[str]:
    """The models a key in ``team_id`` may use (``None``: the proxy's), for both
    the picker and the create/update checks. 400 if the team doesn't exist,
    502 if LiteLLM can't say."""
    try:
        team = await client.get_team(team_id) if team_id else None
    except Exception as e:
        logger.exception("LiteLLM get_team failed: %s", e)
        raise HTTPException(status_code=502, detail="Upstream service error")
    if team_id and team is None:
        raise HTTPException(status_code=400, detail=f"Invalid {team_word} for this user")
    try:
        return await allowed_models(client, team)
    except Exception as e:
        logger.exception("LiteLLM list_models failed: %s", e)
        raise HTTPException(status_code=502, detail="Upstream service error")


def requested_models(requested: list[str] | None) -> list[str]:
    """The requested model names, trimmed and de-duplicated; a 400 for a blank
    one, so ``[""]`` can't stand for "all models". Empty or missing means all
    the models the team allows: the key inherits the team's access."""
    wanted = [m.strip() if isinstance(m, str) else "" for m in (requested or [])]
    if "" in wanted:
        raise HTTPException(status_code=400, detail="Model names can't be blank")
    return list(dict.fromkeys(wanted))


def check_models(wanted: list[str], allowed: list[str], where: str) -> list[str]:
    """``wanted`` if every model is allowed; else a 400 naming the others."""
    not_allowed = [m for m in wanted if not is_allowed(m, allowed)]
    if not_allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Not available in {where}: {', '.join(not_allowed)}",
        )
    return wanted
