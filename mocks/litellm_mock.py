"""
Mock LiteLLM proxy server for end-to-end testing.

Implements the key management routes that the signup-app uses:
  POST /key/generate
  GET  /key/list
  GET  /key/info
  POST /key/update
  POST /key/delete
  POST /key/block
  POST /key/unblock

Run: python -m uvicorn mocks.litellm_mock:app --port 4000
"""

import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

app = FastAPI(title="Mock LiteLLM Proxy")

# In-memory stores
keys_db: dict[str, dict] = {}
users_db: dict[str, dict] = {}

ADMIN_KEY = "sk-mock-admin-key"


def check_admin(authorization: str = Header()):
    token = authorization.replace("Bearer ", "")
    if token != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Invalid admin key")


class GenerateKeyRequest(BaseModel):
    user_id: str = ""
    key_alias: str = ""
    duration: str | None = None
    models: list[str] = []
    max_budget: float | None = None
    rpm_limit: int | None = None
    tpm_limit: int | None = None
    metadata: dict = {}


class UpdateKeyRequest(BaseModel):
    key: str
    key_alias: str | None = None
    models: list[str] | None = None
    max_budget: float | None = None
    rpm_limit: int | None = None
    tpm_limit: int | None = None
    duration: str | None = None


class DeleteKeyRequest(BaseModel):
    keys: list[str] = []
    key_aliases: list[str] | None = None


class BlockKeyRequest(BaseModel):
    key: str


class NewUserRequest(BaseModel):
    user_id: str
    user_role: str = "internal_user"
    send_invite_email: bool = False
    auto_create_key: bool = False
    metadata: dict = {}
    user_email: str = ""
    rpm_limit: int | None = None
    tpm_limit: int | None = None


@app.post("/user/new")
async def create_user(body: NewUserRequest, authorization: str = Header()):
    check_admin(authorization)
    now = datetime.now(timezone.utc).isoformat()
    user = {
        "user_id": body.user_id,
        "user_role": body.user_role,
        "user_email": body.user_email,
        "metadata": body.metadata,
        "rpm_limit": body.rpm_limit,
        "tpm_limit": body.tpm_limit,
        "created_at": now,
    }
    users_db[body.user_id] = user
    return user


@app.get("/user/info")
async def get_user(user_id: str = "", authorization: str = Header()):
    check_admin(authorization)
    if user_id not in users_db:
        raise HTTPException(status_code=404, detail="User not found")
    record = dict(users_db[user_id])
    # Attach a synthetic budget posture so the dashboard has something
    # meaningful to render in local dev. Real LiteLLM returns these on
    # the user record when budgets are configured upstream.
    record.setdefault("max_budget", 100.0)
    record.setdefault("soft_budget", 80.0)
    record.setdefault("budget_duration", "30d")
    record.setdefault(
        "budget_reset_at",
        (datetime.now(timezone.utc) + timedelta(days=14)).isoformat(),
    )
    _seed_daily_activity(user_id)
    spend_total = sum(
        float(r["metrics"]["spend"])
        for r in daily_activity_db.get(user_id, [])
    )
    record["spend"] = round(spend_total, 6)
    return record


@app.post("/key/generate")
async def generate_key(body: GenerateKeyRequest, authorization: str = Header()):
    check_admin(authorization)

    # Mirror LiteLLM's real _enforce_unique_key_alias behavior so the
    # signup-app sees the same failure mode in local dev as production.
    # We return a JSONResponse directly (instead of HTTPException) so the
    # body matches the real proxy's exception-handler shape:
    #   {"error": {"message": ..., "type": ..., "param": ..., "code": ...}}
    # rather than FastAPI's default {"detail": ...} wrapper.
    if body.key_alias:
        for existing in keys_db.values():
            if existing.get("key_alias") == body.key_alias:
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": {
                            "message": (
                                "Unique key aliases are required. "
                                f"Key alias={body.key_alias} already exists."
                            ),
                            "type": "bad_request_error",
                            "param": "key_alias",
                            "code": "400",
                        }
                    },
                )

    token = "sk-" + secrets.token_hex(24)
    token_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    key_record = {
        "key": token,
        "token": token,
        "token_id": token_id,
        "key_alias": body.key_alias,
        "key_name": body.key_alias,
        "user_id": body.user_id,
        "created_at": now,
        "expires": None,
        "spend": 0.0,
        "max_budget": body.max_budget,
        "models": body.models,
        "rpm_limit": body.rpm_limit,
        "tpm_limit": body.tpm_limit,
        "blocked": False,
        "metadata": body.metadata,
    }

    keys_db[token] = key_record
    return key_record


@app.get("/key/list")
async def list_keys(
    user_id: str = "",
    return_full_object: str = "false",
    authorization: str = Header(),
):
    check_admin(authorization)

    result = []
    for k in keys_db.values():
        if user_id and k["user_id"] != user_id:
            continue
        # Return a copy without the full key
        entry = {**k}
        entry["token"] = entry["token"][:8] + "..."
        entry.pop("key", None)
        result.append(entry)

    return result


@app.get("/key/info")
async def key_info(key: str = "", authorization: str = Header()):
    check_admin(authorization)

    found = _find_key(key)
    if not found:
        raise HTTPException(status_code=404, detail="Key not found")
    return keys_db[found]


@app.post("/key/update")
async def update_key(body: UpdateKeyRequest, authorization: str = Header()):
    check_admin(authorization)

    found = _find_key(body.key)
    if not found:
        raise HTTPException(status_code=404, detail="Key not found")
    record = keys_db[found]

    if body.key_alias is not None:
        record["key_alias"] = body.key_alias
        record["key_name"] = body.key_alias
    if body.models is not None:
        record["models"] = body.models
    if body.max_budget is not None:
        record["max_budget"] = body.max_budget
    if body.rpm_limit is not None:
        record["rpm_limit"] = body.rpm_limit
    if body.tpm_limit is not None:
        record["tpm_limit"] = body.tpm_limit

    return record


def _find_key(identifier: str) -> str | None:
    """Find a key by token, token_id, or key_alias."""
    if identifier in keys_db:
        return identifier
    for token, record in keys_db.items():
        if record.get("token_id") == identifier:
            return token
        if record.get("key_alias") == identifier:
            return token
    return None


@app.post("/key/delete")
async def delete_key(body: DeleteKeyRequest, authorization: str = Header()):
    check_admin(authorization)

    deleted = []
    for key in body.keys:
        found = _find_key(key)
        if found:
            del keys_db[found]
            deleted.append(key)

    return {"deleted_keys": deleted}


@app.post("/key/block")
async def block_key(body: BlockKeyRequest, authorization: str = Header()):
    check_admin(authorization)

    found = _find_key(body.key)
    if not found:
        raise HTTPException(status_code=404, detail="Key not found")
    keys_db[found]["blocked"] = True
    return keys_db[found]


@app.post("/key/unblock")
async def unblock_key(body: BlockKeyRequest, authorization: str = Header()):
    check_admin(authorization)

    found = _find_key(body.key)
    if not found:
        raise HTTPException(status_code=404, detail="Key not found")
    keys_db[found]["blocked"] = False
    return keys_db[found]


@app.get("/health")
async def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Daily activity endpoint (used by the accounting dashboard)
# ---------------------------------------------------------------------------
#
# The mock keeps a small, deterministic per-day rollup keyed by user_id
# so the dashboard has something to render in local dev. Records are
# seeded on first read (rather than at module import) so each fresh run
# gets the current date — otherwise the time-series chart in the UI
# would always bottom out at "no recent activity" once the seed data
# ages out.

daily_activity_db: dict[str, list[dict]] = {}


def _seed_daily_activity(user_id: str) -> None:
    """Populate per-day rollups with synthetic activity for ``user_id``.

    The seed data spans multiple models and multiple keys so every
    section of the dashboard surfaces without anyone having to push
    real traffic through the proxy first. Shape mirrors LiteLLM's
    ``DailySpendData`` / ``BreakdownMetrics`` schema.
    """
    if user_id in daily_activity_db:
        return
    today = datetime.now(timezone.utc).date()

    # (days_ago, [(model, api_key_alias, prompt, completion, spend, ok)])
    samples = [
        (0, [
            ("gpt-4o", "alice-prod", 1200, 350, 0.0182, True),
            ("gpt-4o-mini", "alice-prod", 800, 220, 0.0024, True),
        ]),
        (1, [
            ("gpt-4o", "alice-prod", 1500, 410, 0.0221, True),
            ("claude-3-5-sonnet", "alice-research", 2200, 600, 0.0468, True),
        ]),
        (2, [("claude-3-5-sonnet", "alice-research", 1800, 520, 0.0392, True)]),
        (3, [("gpt-4o", "alice-prod", 900, 180, 0.0117, True)]),
        (4, [("gpt-4o-mini", "alice-prod", 600, 140, 0.0014, True)]),
        (5, [("gpt-4o", "alice-prod", 1100, 260, 0.0146, True)]),
        (7, [("gpt-4o", "alice-prod", 0, 0, 0.0, False)]),
        (10, [("gpt-4o", "alice-prod", 1400, 380, 0.0203, True)]),
        (15, [("claude-3-5-sonnet", "alice-research", 1900, 540, 0.0408, True)]),
        (25, [("gpt-4o-mini", "alice-prod", 500, 100, 0.0009, True)]),
        (35, [("gpt-4o", "alice-prod", 1300, 320, 0.0173, True)]),
    ]

    def _zero():
        return {
            "spend": 0.0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "api_requests": 0,
            "successful_requests": 0,
            "failed_requests": 0,
        }

    def _add(dst, ptok, ctok, spend, ok):
        dst["spend"] += spend
        dst["prompt_tokens"] += ptok
        dst["completion_tokens"] += ctok
        dst["total_tokens"] += ptok + ctok
        dst["api_requests"] += 1
        if ok:
            dst["successful_requests"] += 1
        else:
            dst["failed_requests"] += 1

    def _resolve_api_key(alias: str) -> str:
        for record in keys_db.values():
            if (
                record.get("user_id") == user_id
                and record.get("key_alias", "").endswith(alias)
            ):
                return record["token"]
        return "sk-" + alias.replace("-", "").ljust(20, "0")[:20]

    def _resolve_key_metadata(alias: str) -> dict:
        for record in keys_db.values():
            if (
                record.get("user_id") == user_id
                and record.get("key_alias", "").endswith(alias)
            ):
                return record.get("metadata") or {}
        defaults = {
            "alice-prod": {"project": "1042", "task_number": "3.1.2"},
            "alice-research": {"project": "2088", "task_number": "7.4.1"},
        }
        return defaults.get(alias, {})

    days: list[dict] = []
    for days_ago, events in samples:
        d = (today - timedelta(days=days_ago)).isoformat()
        day_metrics = _zero()
        models: dict[str, dict] = {}
        api_keys: dict[str, dict] = {}
        for model, alias, ptok, ctok, spend, ok in events:
            _add(day_metrics, ptok, ctok, spend, ok)
            m = models.setdefault(
                model, {"metrics": _zero(), "metadata": {}}
            )
            _add(m["metrics"], ptok, ctok, spend, ok)
            api_key = _resolve_api_key(alias)
            k = api_keys.setdefault(
                api_key,
                {
                    "metrics": _zero(),
                    "metadata": {
                        "key_alias": alias,
                        **_resolve_key_metadata(alias),
                    },
                },
            )
            _add(k["metrics"], ptok, ctok, spend, ok)
        days.append({
            "date": d,
            "metrics": day_metrics,
            "breakdown": {
                "models": models,
                "api_keys": api_keys,
                "providers": {},
            },
        })
    daily_activity_db[user_id] = days


@app.get("/user/daily/activity")
async def user_daily_activity(
    user_id: str = "",
    start_date: str = "",
    end_date: str = "",
    page: int = 1,
    page_size: int = 50,
    authorization: str = Header(),
):
    check_admin(authorization)
    if user_id:
        _seed_daily_activity(user_id)
    rows = list(daily_activity_db.get(user_id, []))

    if start_date:
        rows = [r for r in rows if r["date"] >= start_date]
    if end_date:
        rows = [r for r in rows if r["date"] <= end_date]

    # Aggregate totals for the metadata block, matching LiteLLM's shape.
    totals = {
        "total_spend": 0.0,
        "total_prompt_tokens": 0,
        "total_completion_tokens": 0,
        "total_tokens": 0,
        "total_api_requests": 0,
        "total_successful_requests": 0,
        "total_failed_requests": 0,
        "total_cache_read_input_tokens": 0,
        "total_cache_creation_input_tokens": 0,
    }
    for r in rows:
        m = r["metrics"]
        totals["total_spend"] += m["spend"]
        totals["total_prompt_tokens"] += m["prompt_tokens"]
        totals["total_completion_tokens"] += m["completion_tokens"]
        totals["total_tokens"] += m["total_tokens"]
        totals["total_api_requests"] += m["api_requests"]
        totals["total_successful_requests"] += m["successful_requests"]
        totals["total_failed_requests"] += m["failed_requests"]

    return {
        "results": rows,
        "metadata": {
            **totals,
            "page": page,
            "total_pages": 1,
            "has_more": False,
        },
    }
