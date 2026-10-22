"""Which models a key may use: the models its team (project) allows.

A LiteLLM team's ``models`` list decides it. An empty list, or one containing
``all-proxy-models``, means every model on the proxy. ``no-default-models``
means none. Otherwise it's the listed names. Without teams, every model on
the proxy is allowed.
"""
from __future__ import annotations

import fnmatch
import time

from fastapi import HTTPException

from app.core.litellm_client import LiteLLMClient

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


def check_models(requested: list[str] | None, allowed: list[str], where: str) -> list[str]:
    """The requested models, de-duplicated, if all are allowed; else a 400.

    An empty or missing list means "all the models the team allows", so it is
    returned empty and the key inherits the team's access.
    """
    wanted = list(dict.fromkeys(m.strip() for m in (requested or []) if m and m.strip()))
    not_allowed = [m for m in wanted if not is_allowed(m, allowed)]
    if not_allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Not available in {where}: {', '.join(not_allowed)}",
        )
    return wanted
