from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel

from app.core.config import get_settings
from app.core.litellm_client import LiteLLMClient

router = APIRouter(prefix="/api")


def _get_client() -> LiteLLMClient:
    return LiteLLMClient(get_settings())


class CreateKeyRequest(BaseModel):
    name: str
    duration: str | None = None
    models: list[str] | None = None
    max_budget: float | None = None
    rpm_limit: int | None = None
    tpm_limit: int | None = None


class UpdateKeyRequest(BaseModel):
    key_alias: str | None = None
    models: list[str] | None = None
    max_budget: float | None = None
    rpm_limit: int | None = None
    tpm_limit: int | None = None
    duration: str | None = None


def _mask_key(token: str) -> str:
    """Show first 8 chars + '...' for display."""
    if not token:
        return ""
    return token[:8] + "..." if len(token) > 8 else token


def _format_key_response(key_data: dict, include_full_key: str | None = None) -> dict:
    """Format a LiteLLM key object for the frontend."""
    data = {
        "id": key_data.get("token_id", key_data.get("token", "")),
        "name": key_data.get("key_alias") or key_data.get("key_name", ""),
        "prefix": _mask_key(
            key_data.get("token", key_data.get("key", ""))
        ),
        "created_at": key_data.get("created_at", ""),
        "expires": key_data.get("expires", ""),
        "is_active": not key_data.get("blocked", False),
        "spend": key_data.get("spend", 0),
        "max_budget": key_data.get("max_budget"),
        "models": key_data.get("models", []),
        "rpm_limit": key_data.get("rpm_limit"),
        "tpm_limit": key_data.get("tpm_limit"),
        "user_id": key_data.get("user_id", ""),
    }
    if include_full_key:
        data["key"] = include_full_key
    return data


@router.post("/keys", status_code=201)
async def create_key(body: CreateKeyRequest, request: Request):
    client = _get_client()
    user_email = request.state.user_email

    kwargs = {"user_id": user_email, "key_alias": body.name}
    if body.duration:
        kwargs["duration"] = body.duration
    if body.models:
        kwargs["models"] = body.models
    if body.max_budget is not None:
        kwargs["max_budget"] = body.max_budget
    if body.rpm_limit is not None:
        kwargs["rpm_limit"] = body.rpm_limit
    if body.tpm_limit is not None:
        kwargs["tpm_limit"] = body.tpm_limit

    try:
        result = await client.generate_key(**kwargs)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"LiteLLM error: {e}")

    full_key = result.get("key", "")
    return _format_key_response(result, include_full_key=full_key)


@router.get("/keys")
async def list_keys(request: Request):
    client = _get_client()
    user_email = request.state.user_email

    try:
        result = await client.list_keys(user_id=user_email)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"LiteLLM error: {e}")

    keys = result if isinstance(result, list) else result.get("keys", [])
    return [_format_key_response(k) for k in keys]


@router.patch("/keys/{token}")
async def update_key(token: str, body: UpdateKeyRequest, request: Request):
    client = _get_client()

    kwargs = {}
    if body.key_alias is not None:
        kwargs["key_alias"] = body.key_alias
    if body.models is not None:
        kwargs["models"] = body.models
    if body.max_budget is not None:
        kwargs["max_budget"] = body.max_budget
    if body.rpm_limit is not None:
        kwargs["rpm_limit"] = body.rpm_limit
    if body.tpm_limit is not None:
        kwargs["tpm_limit"] = body.tpm_limit
    if body.duration is not None:
        kwargs["duration"] = body.duration

    try:
        result = await client.update_key(key=token, **kwargs)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"LiteLLM error: {e}")

    return result


@router.delete("/keys/{token}")
async def delete_key(token: str, request: Request):
    client = _get_client()
    try:
        result = await client.delete_key(keys=[token])
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"LiteLLM error: {e}")
    return result


@router.post("/keys/{token}/block")
async def block_key(token: str, request: Request):
    client = _get_client()
    try:
        result = await client.block_key(key=token)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"LiteLLM error: {e}")
    return result


@router.post("/keys/{token}/unblock")
async def unblock_key(token: str, request: Request):
    client = _get_client()
    try:
        result = await client.unblock_key(key=token)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"LiteLLM error: {e}")
    return result
