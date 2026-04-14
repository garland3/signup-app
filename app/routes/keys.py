import logging
import re

from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel

from app.core.audit import audit
from app.core.config import get_settings
from app.core.litellm_client import LiteLLMClient
from app.core.rate_limit import limiter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


def _get_client() -> LiteLLMClient:
    return LiteLLMClient(get_settings())


def _upstream_error(op: str, exc: Exception) -> HTTPException:
    """Log the real reason and return a generic 502 to the client.

    Prevents raw exception text from leaking upstream internals or
    operator-only hints to end users.
    """
    logger.exception("LiteLLM %s failed: %s", op, exc)
    return HTTPException(status_code=502, detail="Upstream service error")


def _reject_raw_api_key(token: str) -> None:
    """Reject path params that look like a raw API key.

    URL path segments end up in access logs, APM traces, browser
    history, and Referer headers. Only opaque token IDs should appear
    there, never the secret key itself.
    """
    if token.startswith("sk-"):
        raise HTTPException(status_code=400, detail="Invalid key identifier")


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
    """Show first 8 chars + '...' for display."""
    if not token:
        return ""
    return token[:8] + "..." if len(token) > 8 else token


def _format_key_response(key_data: dict, include_full_key: str | None = None) -> dict:
    """Format a LiteLLM key object for the frontend.

    Never includes the full key unless include_full_key is explicitly passed
    (only at creation time).
    """
    metadata = key_data.get("metadata") or {}
    data = {
        "id": key_data.get("token_id", key_data.get("token", "")),
        "name": key_data.get("key_alias") or key_data.get("key_name", ""),
        "prefix": _mask_key(
            key_data.get("token", key_data.get("key", ""))
        ),
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


_NAME_SANITIZE_RE = re.compile(r"[^A-Za-z0-9-]+")
_COLLAPSE_DASH_RE = re.compile(r"-+")


def _sanitize_key_name(name: str) -> str:
    """Restrict a user-supplied key name to alphanumeric characters and dashes.

    Any other character (spaces, punctuation, unicode, etc.) is replaced
    with a dash; runs of dashes are collapsed and leading/trailing dashes
    are stripped so the result is a clean slug. This is applied before
    prepending the canonical ``{user_email}-`` prefix, so the email
    characters in the prefix itself are preserved.
    """
    cleaned = _NAME_SANITIZE_RE.sub("-", name)
    cleaned = _COLLAPSE_DASH_RE.sub("-", cleaned).strip("-")
    return cleaned


def _normalize_key_alias(name: str, user_email: str) -> str:
    """Ensure key alias starts with exactly one '{user_email}-' prefix.

    Collapses repeated prefixes (e.g. "alice-alice-MyKey" -> "alice-MyKey")
    and handles STRIP_USER_DOMAIN mode where user_email may be just the
    username (e.g. "alice") while the user-supplied name might contain the
    full email prefix (e.g. "alice@corp-mail.com-MyKey"). In that case the
    full-email prefix is replaced with the canonical stripped prefix so we
    avoid double-prefixing.

    The non-prefix portion is sanitized to alphanumeric and dashes only.
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
        match = re.match(
            re.escape(user_email) + r"@[^@]+\.[a-zA-Z]{2,}-", name
        )
        if match:
            name = name[match.end():]
    # Restrict the user-portion to alphanumeric + dash. The prefix is
    # system-generated from the authenticated user email and preserves
    # characters like "@" and "." that sanitization would strip.
    name = _sanitize_key_name(name)
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
        "nav_links": s.nav_links,
        "show_spend": s.SHOW_SPEND_COLUMN,
    }


@router.post("/keys", status_code=201)
async def create_key(body: CreateKeyRequest, request: Request):
    settings = get_settings()
    client = _get_client()
    user_email = request.state.user_email

    # Per-user hourly cap on key creation, on top of the per-minute API cap.
    if not limiter.check(
        "key_create", user_email, settings.RATE_LIMIT_KEY_CREATE_PER_HOUR, 3600
    ):
        audit("rate_limited", bucket="key_create", user=user_email)
        raise HTTPException(status_code=429, detail="Too many key creations")

    # Reject names that contain no alphanumeric characters before doing
    # any upstream calls: _normalize_key_alias will sanitize the name to
    # alphanumeric + dash, and a blank result would produce a key whose
    # alias is just the prefix.
    if not _sanitize_key_name(body.name):
        raise HTTPException(
            status_code=400,
            detail="Name must contain at least one alphanumeric character",
        )

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
            raise _upstream_error("list_keys", e)
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
        raise _upstream_error("ensure_user", e)

    # Persist duration in metadata so we can show it in the UI
    if body.duration:
        metadata["duration"] = body.duration

    # Force key alias to always start with "{user_email}-" prefix. The
    # user-portion is sanitized to alphanumeric + dash inside the helper.
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
        raise _upstream_error("generate_key", e)

    full_key = result.get("key", "")
    # Ensure metadata surfaces in the response even if LiteLLM strips it
    if "metadata" not in result:
        result["metadata"] = metadata
    audit(
        "key_create",
        user=user_email,
        key_id=result.get("token_id", ""),
        key_alias=key_name,
        models=body.models or [],
        max_budget=body.max_budget,
        duration=body.duration,
    )
    return _format_key_response(result, include_full_key=full_key)


@router.get("/keys")
async def list_keys(request: Request):
    client = _get_client()
    user_email = request.state.user_email

    try:
        result = await client.list_keys(user_id=user_email)
    except Exception as e:
        raise _upstream_error("list_keys", e)

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
    _reject_raw_api_key(token)
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
        raise _upstream_error("update_key", e)

    if "metadata" not in result and "metadata" in kwargs:
        result["metadata"] = kwargs["metadata"]
    audit(
        "key_update",
        user=user_email,
        key_id=token,
        changed=sorted(kwargs.keys()),
    )
    return _format_key_response(result)


@router.delete("/keys/{token}")
async def delete_key(token: str, request: Request):
    """Soft delete: set the key's duration to 0 so it expires immediately."""
    _reject_raw_api_key(token)
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
        raise _upstream_error("update_key", e)

    audit("key_delete", user=user_email, key_id=token)
    return {"deleted": True}


@router.post("/keys/{token}/block")
async def block_key(token: str, request: Request):
    _reject_raw_api_key(token)
    client = _get_client()
    user_email = request.state.user_email

    await _verify_key_ownership(client, token, user_email)

    try:
        result = await client.block_key(key=token)
    except Exception as e:
        raise _upstream_error("block_key", e)

    audit("key_block", user=user_email, key_id=token)
    return _format_key_response(result)


@router.post("/keys/{token}/unblock")
async def unblock_key(token: str, request: Request):
    _reject_raw_api_key(token)
    client = _get_client()
    user_email = request.state.user_email

    await _verify_key_ownership(client, token, user_email)

    try:
        result = await client.unblock_key(key=token)
    except Exception as e:
        raise _upstream_error("unblock_key", e)

    audit("key_unblock", user=user_email, key_id=token)
    return _format_key_response(result)
