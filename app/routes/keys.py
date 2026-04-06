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
    metadata: dict[str, str] | None = None


class UpdateKeyRequest(BaseModel):
    key_alias: str | None = None
    models: list[str] | None = None
    max_budget: float | None = None
    rpm_limit: int | None = None
    tpm_limit: int | None = None
    duration: str | None = None
    metadata: dict[str, str] | None = None


def _mask_key(token: str) -> str:
    """Show first 6 chars + '...' + last 2 chars for display."""
    if not token:
        return ""
    if len(token) <= 10:
        return token
    return token[:6] + "..." + token[-2:]


def _format_key_response(key_data: dict, include_full_key: str | None = None) -> dict:
    """Format a LiteLLM key object for the frontend.

    Never includes the full key unless include_full_key is explicitly passed
    (only at creation time).
    """
    metadata = key_data.get("metadata") or {}
    data = {
        "id": key_data.get("token_id", key_data.get("token", "")),
        "name": key_data.get("key_alias") or key_data.get("key_name", ""),
        "prefix": (metadata.get("key_preview") if isinstance(metadata, dict) else "")
            or _mask_key(key_data.get("token", key_data.get("key", ""))),
        "created_at": key_data.get("created_at", ""),
        "expires": key_data.get("expires", ""),
        "duration": metadata.get("duration", "") if isinstance(metadata, dict) else "",
        "is_active": not key_data.get("blocked", False)
            and not _is_expired(key_data.get("expires")),
        "spend": key_data.get("spend", 0),
        "max_budget": key_data.get("max_budget"),
        "models": key_data.get("models", []),
        "rpm_limit": key_data.get("rpm_limit"),
        "tpm_limit": key_data.get("tpm_limit"),
        "user_id": key_data.get("user_id", ""),
        "metadata": metadata if isinstance(metadata, dict) else {},
    }
    if include_full_key:
        data["key"] = include_full_key
    return data


def _is_expired(expires: str | None) -> bool:
    if not expires:
        return False
    try:
        from datetime import datetime, timezone
        s = expires.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt <= datetime.now(timezone.utc)
    except Exception:
        return False


def _normalize_key_alias(name: str, user_email: str) -> str:
    """Ensure key alias starts with exactly one '{user_email}-' prefix.

    Collapses repeated prefixes (e.g. "alice-alice-MyKey" → "alice-MyKey")
    and handles STRIP_USER_DOMAIN mode where user_email may be just the
    username (e.g. "alice") while the user-supplied name might contain the
    full email prefix (e.g. "alice@corp-mail.com-MyKey"). In that case the
    full-email prefix is replaced with the canonical stripped prefix so we
    avoid double-prefixing.
    """
    prefix = f"{user_email}-"
    # Collapse any repeated canonical prefixes
    while name.startswith(prefix):
        name = name[len(prefix):]
    # When STRIP_USER_DOMAIN is active, user_email has no "@".
    # Detect and strip a full-email variant of the same username prefix.
    # The regex requires a TLD-like pattern (e.g. ".com") before the "-"
    # separator so hyphens within domains (e.g. "corp-mail.com") aren't
    # mistaken for the separator.
    if "@" not in user_email and name.startswith(user_email + "@"):
        import re
        match = re.match(
            re.escape(user_email) + r"@[^@]+\.[a-zA-Z]{2,}-", name
        )
        if match:
            name = name[match.end():]
    return prefix + name


async def _verify_key_ownership(
    client: LiteLLMClient, token: str, user_email: str
) -> dict:
    """Fetch key info and verify the authenticated user owns it.

    Returns the key record if ownership is confirmed.
    Raises 404 if the key doesn't exist or doesn't belong to the user.
    """
    try:
        key_info = await client.get_key_info(key=token)
    except Exception:
        raise HTTPException(status_code=404, detail="Key not found")

    # key_info may be nested under an "info" key depending on LiteLLM version
    info = key_info.get("info", key_info)
    key_owner = info.get("user_id", "")

    if key_owner != user_email:
        raise HTTPException(status_code=404, detail="Key not found")

    return info


@router.get("/config")
async def get_config():
    s = get_settings()
    return {
        "app_name": s.APP_NAME,
        "required_metadata": s.required_metadata_fields,
        "max_active_keys": s.MAX_ACTIVE_KEYS_PER_USER,
    }


@router.post("/keys", status_code=201)
async def create_key(body: CreateKeyRequest, request: Request):
    settings = get_settings()
    client = _get_client()
    user_email = request.state.user_email

    # Validate required metadata fields
    required = settings.required_metadata_fields
    metadata = dict(body.metadata or {})
    missing = [f for f in required if not str(metadata.get(f, "")).strip()]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Missing required metadata: {', '.join(missing)}",
        )

    # Enforce max active keys per user
    if settings.MAX_ACTIVE_KEYS_PER_USER is not None:
        try:
            existing = await client.list_keys(user_id=user_email)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"LiteLLM error: {e}")
        keys = existing if isinstance(existing, list) else existing.get("keys", [])
        active_count = sum(
            1 for k in keys
            if not k.get("blocked", False) and not _is_expired(k.get("expires"))
        )
        if active_count >= settings.MAX_ACTIVE_KEYS_PER_USER:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Active key limit reached "
                    f"({settings.MAX_ACTIVE_KEYS_PER_USER}). "
                    "Delete an existing key first."
                ),
            )

    # Ensure the user exists in LiteLLM before creating a key
    try:
        await client.ensure_user(user_email)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"LiteLLM error: {e}")

    # Persist duration in metadata so we can show it in the UI
    if body.duration:
        metadata["duration"] = body.duration

    # Force key alias to always start with "{user_email}-" prefix
    key_name = _normalize_key_alias(body.name, user_email)

    kwargs = {
        "user_id": user_email,
        "key_alias": key_name,
        "metadata": metadata,
    }
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
    # Store a key preview in metadata so it survives list calls
    if full_key:
        metadata["key_preview"] = _mask_key(full_key)
        kwargs["metadata"] = metadata
        try:
            await client.update_key(key=full_key, metadata=metadata)
        except Exception:
            pass  # best-effort; preview will still be in the creation response
    # Ensure metadata surfaces in the response even if LiteLLM strips it
    if "metadata" not in result:
        result["metadata"] = metadata
    else:
        result["metadata"]["key_preview"] = metadata.get("key_preview", "")
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
    formatted = [_format_key_response(k) for k in keys]
    # Active keys at top, then inactive; stable sort by created_at desc within
    formatted.sort(
        key=lambda k: (0 if k["is_active"] else 1, _neg_created(k["created_at"]))
    )
    return formatted


def _neg_created(created: str) -> str:
    # Reverse-sort by created_at by inverting a date string; simple fallback:
    # newest first. Uses lexicographic sort on ISO-8601 strings.
    return "".join(chr(255 - ord(c)) for c in (created or ""))


@router.patch("/keys/{token}")
async def update_key(token: str, body: UpdateKeyRequest, request: Request):
    client = _get_client()
    user_email = request.state.user_email

    info = await _verify_key_ownership(client, token, user_email)

    kwargs = {}
    if body.key_alias is not None:
        kwargs["key_alias"] = _normalize_key_alias(body.key_alias, user_email)
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
        # Update metadata so we keep the duration visible in the UI
        existing_meta = info.get("metadata") or {}
        if not isinstance(existing_meta, dict):
            existing_meta = {}
        merged = {**existing_meta, **(body.metadata or {}), "duration": body.duration}
        kwargs["metadata"] = merged
    elif body.metadata is not None:
        existing_meta = info.get("metadata") or {}
        if not isinstance(existing_meta, dict):
            existing_meta = {}
        kwargs["metadata"] = {**existing_meta, **body.metadata}

    try:
        result = await client.update_key(key=token, **kwargs)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"LiteLLM error: {e}")

    if "metadata" not in result and "metadata" in kwargs:
        result["metadata"] = kwargs["metadata"]
    return _format_key_response(result)


@router.delete("/keys/{token}")
async def delete_key(token: str, request: Request):
    """Soft delete: set the key's duration to 0 so it expires immediately."""
    client = _get_client()
    user_email = request.state.user_email

    info = await _verify_key_ownership(client, token, user_email)

    existing_meta = info.get("metadata") or {}
    if not isinstance(existing_meta, dict):
        existing_meta = {}
    merged_meta = {**existing_meta, "duration": "0s"}

    try:
        await client.update_key(
            key=token, duration="0s", metadata=merged_meta
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"LiteLLM error: {e}")

    return {"deleted": True}


@router.post("/keys/{token}/block")
async def block_key(token: str, request: Request):
    client = _get_client()
    user_email = request.state.user_email

    await _verify_key_ownership(client, token, user_email)

    try:
        result = await client.block_key(key=token)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"LiteLLM error: {e}")

    return _format_key_response(result)


@router.post("/keys/{token}/unblock")
async def unblock_key(token: str, request: Request):
    client = _get_client()
    user_email = request.state.user_email

    await _verify_key_ownership(client, token, user_email)

    try:
        result = await client.unblock_key(key=token)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"LiteLLM error: {e}")

    return _format_key_response(result)
