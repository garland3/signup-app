import logging
import re
from datetime import UTC, datetime

from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel

from app.core.audit import audit
from app.core import durations
from app.core.config import RESERVED_KEY_METADATA, get_settings
from app.core.litellm_client import DuplicateKeyAliasError, LiteLLMClient
from app.core.rate_limit import limiter
from app.core.team_claims import refresh_groups, teams_for_groups

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
    # Team to scope the key to. Required when FEATURE_TEAMS_ENABLED (must be
    # a team the caller belongs to); ignored when the feature is off.
    team_id: str | None = None


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


def _format_key_response(
    key_data: dict, include_full_key: str | None = None, *, user_email: str = ""
) -> dict:
    """Format a LiteLLM key object for the frontend.

    Never includes the full key unless include_full_key is explicitly passed
    (only at creation time). ``display_name`` is the name as the page shows it
    (HIDE_KEY_NAME_PREFIX); ``name`` is the alias as stored.
    """
    metadata = key_data.get("metadata") or {}
    name = key_data.get("key_alias") or key_data.get("key_name", "")
    data = {
        "id": key_data.get("token_id", key_data.get("token", "")),
        "name": name,
        "display_name": get_settings().key_display_name(name, user_email),
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
        "team_id": key_data.get("team_id"),
        "team_alias": key_data.get("team_alias"),
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

# Durations ("30d", "1mo"): app/core/durations.py, shared with the startup check.
_DURATION_HELP = (
    "Invalid duration. Use a positive integer followed by a unit: "
    "s (seconds), m (minutes), h (hours), d (days), or mo (months), "
    "up to 100 years. Example: 30d, 1mo."
)



def _user_metadata(metadata: dict | None) -> dict:
    """User-supplied metadata without the names the app reserves."""
    return {k: v for k, v in (metadata or {}).items() if k not in RESERVED_KEY_METADATA}


def _is_deleted(info: dict) -> bool:
    """Whether Delete (a soft delete) was used on the key: Delete's marker
    (deleted_at, or before it existed the duration "0s"), on a key that has
    expired. Delete expires the key at once, so a deleted key is always both; a
    live key with a marker written some other way (users' metadata, before
    the app reserved these names) isn't taken for deleted."""
    meta = info.get("metadata") or {}
    if not isinstance(meta, dict) or not _is_expired(info.get("expires")):
        return False
    return bool(meta.get("deleted_at")) or meta.get("duration") == "0s"


def _is_hidden(info: dict) -> bool:
    """Whether the key is marked ``"hidden": true`` in its metadata, by tools
    that make keys for a user which the user has no use for (a gateway's
    short-lived session keys). The app leaves such keys out: they aren't
    listed, don't count toward MAX_ACTIVE_KEYS_PER_USER, and can't be changed,
    blocked or deleted through it. Only the JSON boolean counts: users'
    metadata is strings, so no key a user made is hidden."""
    meta = info.get("metadata")
    return isinstance(meta, dict) and meta.get("hidden") is True


def _validate_duration(value: str) -> None:
    """Reject duration strings LiteLLM won't accept.

    Internal callers that pass system-generated durations (e.g. the soft
    delete that uses ``"0s"``) bypass this by calling the LiteLLM client
    directly; this guard only runs on user-supplied values.
    """
    if durations.parse(value) is None:
        raise HTTPException(status_code=400, detail=_DURATION_HELP)


def _capped_duration(duration: str | None) -> str | None:
    """Apply MAX_KEY_DURATION to a user-supplied (already validated) duration.

    No limit configured: unchanged. No duration given: the limit. Longer
    than the limit: HTTP 400.
    """
    limit = get_settings().MAX_KEY_DURATION
    if not limit:
        return duration
    if duration is None:
        return limit
    if not durations.within(duration, limit):
        raise HTTPException(
            status_code=400,
            detail=f"Keys may last at most {limit}. Choose a shorter duration.",
        )
    return duration


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
    client: LiteLLMClient, token: str, user_id: str
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

    # Since LiteLLM 1.104, /key/info also answers for a key LiteLLM itself has
    # deleted (by /team/member_delete, say), from its archive of deleted keys.
    # Such a key is gone: nothing can be done to it.
    gone = info.get("status") == "deleted"
    if key_owner != user_id or _is_hidden(info) or gone:
        raise HTTPException(status_code=404, detail="Key not found")

    return info


@router.get("/config")
async def get_config():
    s = get_settings()
    return {
        "app_name": s.APP_NAME,
        "required_metadata": s.required_metadata_fields,
        "max_active_keys": s.MAX_ACTIVE_KEYS_PER_USER,
        "max_key_duration": s.MAX_KEY_DURATION or None,
        "nav_links": s.nav_links,
        "show_spend": s.SHOW_SPEND_COLUMN,
        "show_budget": s.SHOW_BUDGET_COLUMN,
        "show_project_task_breakdown": s.SHOW_PROJECT_TASK_BREAKDOWN,
        "team_label": s.team_label(),
        "team_label_plural": s.team_label(plural=True),
    }


@router.post("/keys", status_code=201)
async def create_key(body: CreateKeyRequest, request: Request):
    settings = get_settings()
    client = _get_client()
    user_email = request.state.user_email
    user_id = request.state.user_id

    # Per-user hourly cap on key creation, on top of the per-minute API cap.
    if not limiter.check(
        "key_create", user_id, settings.RATE_LIMIT_KEY_CREATE_PER_HOUR, 3600
    ):
        audit("rate_limited", bucket="key_create", user=user_email)
        raise HTTPException(status_code=429, detail="Too many key creations")

    # Build the canonical alias up-front and reject names whose
    # user-portion is empty after sanitization. This catches both plain
    # garbage (``"!!!"``) and the prefix-stripping bypass where a name
    # like ``"alice@example.com-!!!"`` would otherwise produce an alias
    # that is just the ``{user_email}-`` prefix.
    key_name = _normalize_key_alias(body.name, user_email)
    if key_name == f"{user_email}-":
        raise HTTPException(
            status_code=400,
            detail="Name must contain at least one alphanumeric character",
        )

    if body.duration:
        _validate_duration(body.duration)
    # A key without a duration gets MAX_KEY_DURATION, if one is set.
    duration = _capped_duration(body.duration or None)

    # Validate required metadata fields
    required = settings.required_metadata_fields
    metadata = _user_metadata(body.metadata)
    missing = [f for f in required if not str(metadata.get(f, "")).strip()]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Missing required metadata: {', '.join(missing)}",
        )

    # Enforce max active keys per user
    if settings.MAX_ACTIVE_KEYS_PER_USER is not None:
        try:
            existing = await client.list_keys(user_id=user_id)
        except Exception as e:
            raise _upstream_error("list_keys", e)
        keys = existing if isinstance(existing, list) else existing.get("keys", [])
        active_count = sum(
            1 for k in keys
            if not k.get("blocked", False) and not _is_expired(k.get("expires"))
            and not _is_hidden(k)
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

    # Team-scoped keys: when the teams feature is on, every key must belong
    # to a team the caller is a member of, so teams can act as an access and
    # budget boundary. Fail closed: no team_id, a team the caller isn't in,
    # or a failed membership lookup all block creation. Validate before
    # provisioning anything so a rejected request never creates a user.
    team_id = None
    from_claims = settings.TEAM_SOURCE == "claims"
    if settings.FEATURE_TEAMS_ENABLED:
        if not body.team_id:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Keys belong to {settings.team_word(True)}, and yours come from your sign-in;"
                    " ask your administrators if you have none."
                    if from_claims
                    else f"Keys belong to {settings.team_word(True)}: join one before creating a key."
                ),
            )
        if from_claims:
            # The identity provider's current groups, not the session's.
            groups = await refresh_groups(request)
        try:
            if from_claims:
                teams = await teams_for_groups(groups, client)
            else:
                teams = await client.list_teams(user_id=user_id)
        except Exception as e:
            raise _upstream_error("list_teams", e)
        if body.team_id not in {t.get("team_id") for t in teams}:
            raise HTTPException(
                status_code=400, detail=f"Invalid {settings.team_word()} for this user"
            )
        team_id = body.team_id

    # Ensure the user exists in LiteLLM before creating a key
    try:
        await client.ensure_user(user_id, email=user_email)
    except Exception as e:
        raise _upstream_error("ensure_user", e)

    # Teams from claims: the person is in the team by the identity provider's
    # say; make them a LiteLLM member too, so the team's per-member limits
    # apply to their keys. If generating the key then fails, they stay a member;
    # harmless, since membership follows the group (the reconciler, README).
    if team_id and from_claims:
        try:
            added = await client.ensure_team_member(team_id, user_id)
        except Exception as e:
            raise _upstream_error("team_member_add", e)
        if added:
            audit("team_claims_member_add", user=user_email, team_id=team_id)

    # Persist duration in metadata so we can show it in the UI
    if duration:
        metadata["duration"] = duration

    kwargs = {
        "user_id": user_id,
        "key_alias": key_name,
        "metadata": metadata,
    }
    if team_id:
        kwargs["team_id"] = team_id
    if duration:
        kwargs["duration"] = durations.for_litellm(duration)  # months as days
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
    except DuplicateKeyAliasError:
        # Don't echo the user-supplied name back in the message: it is
        # untrusted input and the frontend has full control over how
        # the error is presented. A fixed message also keeps the error
        # body safe to render directly as textContent.
        audit(
            "key_create_conflict",
            user=user_email,
            key_alias=key_name,
        )
        raise HTTPException(
            status_code=409,
            detail="A key with this name already exists. Please choose a different name.",
        )
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
        duration=duration,
    )
    return _format_key_response(result, include_full_key=full_key, user_email=user_email)


@router.get("/keys")
async def list_keys(request: Request):
    client = _get_client()
    user_id = request.state.user_id
    user_email = request.state.user_email

    try:
        result = await client.list_keys(user_id=user_id)
    except Exception as e:
        raise _upstream_error("list_keys", e)

    keys = result if isinstance(result, list) else result.get("keys", [])
    formatted = [
        _format_key_response(k, user_email=user_email) for k in keys if not _is_hidden(k)
    ]
    # Active keys at top, then inactive; stable sort by created_at desc within
    formatted.sort(
        key=lambda k: (0 if k["is_active"] else 1, _neg_created(k["created_at"]))
    )
    return formatted


def _neg_created(created: str) -> str:
    # Reverse-sort by created_at by inverting a date string; simple fallback:
    # newest first. Uses lexicographic sort on ISO-8601 strings.
    return "".join(chr(255 - ord(c)) for c in (created or ""))


async def _require_current_team(request: Request, client: LiteLLMClient, info: dict) -> None:
    """With teams from claims, the key's team must still be one of the person's
    teams, re-read from the identity provider as for a new key. Someone taken
    out of a group can't then widen (budget, limits, models, duration) or unblock
    their keys in it before the reconciler removes them. Deleting and blocking
    only reduce access, so they don't call this.
    """
    s = get_settings()
    if not (s.FEATURE_TEAMS_ENABLED and s.TEAM_SOURCE == "claims"):
        return
    # Each check is a refresh-token grant at the identity provider, so changes
    # share key creation's hourly cap (in their own count).
    if not limiter.check(
        "key_change", request.state.user_id, s.RATE_LIMIT_KEY_CREATE_PER_HOUR, 3600
    ):
        audit("rate_limited", bucket="key_change", user=request.state.user_email)
        raise HTTPException(status_code=429, detail="Too many key changes")
    groups = await refresh_groups(request)
    try:
        teams = await teams_for_groups(groups, client)
    except Exception as e:
        raise _upstream_error("list_teams", e)
    if not info.get("team_id") or info["team_id"] not in {t["team_id"] for t in teams}:
        raise HTTPException(
            status_code=403,
            detail=f"You're no longer in this key's {s.team_word()}, so it can't be changed or "
            "unblocked. You can still block or delete it.",
        )


@router.patch("/keys/{token}")
async def update_key(token: str, body: UpdateKeyRequest, request: Request):
    _reject_raw_api_key(token)
    client = _get_client()
    user_email = request.state.user_email
    user_id = request.state.user_id

    # Validate purely-local fields before the upstream ownership check so
    # we don't pay an extra round-trip on bad input.
    if body.duration is not None:
        _validate_duration(body.duration)
        _capped_duration(body.duration)

    info = await _verify_key_ownership(client, token, user_id)
    # Delete (below) is a soft delete: the key expires at once and stays listed.
    # A new duration would bring it back, so a deleted key can't be changed:
    # Delete means deleted.
    if _is_deleted(info):
        raise HTTPException(
            status_code=400,
            detail="This key was deleted, so it can't be changed. Create a new key instead.",
        )
    await _require_current_team(request, client, info)

    kwargs = {}
    if body.key_alias is not None:
        new_alias = _normalize_key_alias(body.key_alias, user_email)
        # Reject renames whose user-portion sanitizes to empty, so the
        # stored alias can't be reduced to just the ``{user_email}-`` prefix.
        if new_alias == f"{user_email}-":
            raise HTTPException(
                status_code=400,
                detail="Name must contain at least one alphanumeric character",
            )
        kwargs["key_alias"] = new_alias
    if body.models is not None:
        kwargs["models"] = body.models
    if body.max_budget is not None:
        kwargs["max_budget"] = body.max_budget
    if body.rpm_limit is not None:
        kwargs["rpm_limit"] = body.rpm_limit
    if body.tpm_limit is not None:
        kwargs["tpm_limit"] = body.tpm_limit
    if body.duration is not None:
        kwargs["duration"] = durations.for_litellm(body.duration)  # months as days
        # Update metadata so we keep the duration visible in the UI
        existing_meta = info.get("metadata") or {}
        if not isinstance(existing_meta, dict):
            existing_meta = {}
        merged = {**existing_meta, **_user_metadata(body.metadata), "duration": body.duration}
        kwargs["metadata"] = merged
    elif body.metadata is not None:
        existing_meta = info.get("metadata") or {}
        if not isinstance(existing_meta, dict):
            existing_meta = {}
        kwargs["metadata"] = {**existing_meta, **_user_metadata(body.metadata)}

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
    return _format_key_response(result, user_email=user_email)


@router.delete("/keys/{token}")
async def delete_key(token: str, request: Request):
    """Soft delete: set the key's duration to 0 so it expires immediately."""
    _reject_raw_api_key(token)
    client = _get_client()
    user_email = request.state.user_email
    user_id = request.state.user_id

    info = await _verify_key_ownership(client, token, user_id)

    existing_meta = info.get("metadata") or {}
    if not isinstance(existing_meta, dict):
        existing_meta = {}
    merged_meta = {**existing_meta, "duration": "0s",
                   "deleted_at": datetime.now(UTC).isoformat(timespec="seconds")}

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
    user_id = request.state.user_id

    await _verify_key_ownership(client, token, user_id)

    try:
        result = await client.block_key(key=token)
    except Exception as e:
        raise _upstream_error("block_key", e)

    audit("key_block", user=user_email, key_id=token)
    return _format_key_response(result, user_email=user_email)


@router.post("/keys/{token}/unblock")
async def unblock_key(token: str, request: Request):
    _reject_raw_api_key(token)
    client = _get_client()
    user_email = request.state.user_email
    user_id = request.state.user_id

    info = await _verify_key_ownership(client, token, user_id)
    await _require_current_team(request, client, info)

    try:
        result = await client.unblock_key(key=token)
    except Exception as e:
        raise _upstream_error("unblock_key", e)

    audit("key_unblock", user=user_email, key_id=token)
    return _format_key_response(result, user_email=user_email)
