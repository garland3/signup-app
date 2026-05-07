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
    spend_total = sum(
        float(row.get("spend") or 0)
        for row in spend_logs_db
        if row.get("user") == user_id
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
# Spend / usage endpoints (used by the accounting dashboard)
# ---------------------------------------------------------------------------
#
# The mock keeps a small, deterministic spend log keyed by user_id so the
# dashboard has something to render in local dev. Records are seeded on
# first read (rather than at module import) so each fresh run gets the
# current date — otherwise the time-series chart in the UI would always
# bottom out at "no recent activity" once the seed data ages out.

spend_logs_db: list[dict] = []
_seeded_users: set[str] = set()


def _seed_spend_logs(user_id: str) -> None:
    """Populate the spend log with synthetic activity for ``user_id``.

    The seed data is intentionally varied — multiple models, multiple
    keys, multiple Project/Task tags, and a deliberate "tag missing"
    request — so the dashboard surfaces every code path without anyone
    having to run real traffic through the proxy first.
    """
    if user_id in _seeded_users:
        return
    _seeded_users.add(user_id)
    now = datetime.now(timezone.utc)
    samples = [
        # (days_ago, model, api_key_alias, prompt, completion, spend, tags, status)
        (0, "gpt-4o", "alice-prod", 1200, 350, 0.0182, ["project:1042", "task:3.1.2"], "success"),
        (0, "gpt-4o-mini", "alice-prod", 800, 220, 0.0024, ["project:1042", "task:3.1.2"], "success"),
        (1, "gpt-4o", "alice-prod", 1500, 410, 0.0221, ["project:1042", "task:3.1"], "success"),
        (1, "claude-3-5-sonnet", "alice-research", 2200, 600, 0.0468, ["project:2001", "task:1.2"], "success"),
        (2, "claude-3-5-sonnet", "alice-research", 1800, 520, 0.0392, ["project:2001", "task:1.2"], "success"),
        (3, "gpt-4o", "alice-prod", 900, 180, 0.0117, ["project:1042", "task:3"], "success"),
        (4, "gpt-4o-mini", "alice-prod", 600, 140, 0.0014, ["project:1042"], "success"),
        (5, "gpt-4o", "alice-prod", 1100, 260, 0.0146, [], "success"),  # unattributed
        (6, "claude-3-5-sonnet", "alice-research", 700, 200, 0.0148, ["task:5.1"], "success"),  # malformed - no project
        (7, "gpt-4o", "alice-prod", 0, 0, 0.0, ["project:1042", "task:3.1.2"], "failure"),
        (10, "gpt-4o", "alice-prod", 1400, 380, 0.0203, ["project:1042", "task:3.2"], "success"),
        (15, "claude-3-5-sonnet", "alice-research", 1900, 540, 0.0408, ["project:2001", "task:1.1"], "success"),
        (25, "gpt-4o-mini", "alice-prod", 500, 100, 0.0009, ["project:1042", "task:3.1.1"], "success"),
        (35, "gpt-4o", "alice-prod", 1300, 320, 0.0173, ["project:1042", "task:3.1.2"], "success"),
    ]
    for days_ago, model, alias, ptok, ctok, spend, tags, status in samples:
        ts = (now - timedelta(days=days_ago)).isoformat()
        # Find a real key token for this alias if one exists, otherwise
        # fall back to a synthetic prefix so the dashboard still has a
        # stable identifier to group by.
        api_key = ""
        for record in keys_db.values():
            if (
                record.get("user_id") == user_id
                and record.get("key_alias", "").endswith(alias)
            ):
                api_key = record["token"]
                break
        if not api_key:
            api_key = "sk-" + alias.replace("-", "").ljust(20, "0")[:20]

        spend_logs_db.append(
            {
                "request_id": f"req-{secrets.token_hex(8)}",
                "api_key": api_key,
                "model": model,
                "call_type": "completion",
                "spend": spend,
                "prompt_tokens": ptok,
                "completion_tokens": ctok,
                "total_tokens": ptok + ctok,
                "startTime": ts,
                "endTime": ts,
                "user": user_id,
                "metadata": {
                    "status": status,
                    "status_code": 200 if status == "success" else 500,
                    "user_api_key_user_id": user_id,
                },
                "request_tags": tags,
                "cache_hit": "False",
            }
        )


@app.get("/spend/logs")
async def spend_logs(
    user_id: str = "",
    api_key: str = "",
    request_id: str = "",
    start_date: str = "",
    end_date: str = "",
    summarize: str = "true",
    authorization: str = Header(),
):
    check_admin(authorization)
    if user_id:
        _seed_spend_logs(user_id)

    out = []
    for row in spend_logs_db:
        if user_id and row.get("user") != user_id:
            continue
        if api_key and row.get("api_key") != api_key:
            continue
        if request_id and row.get("request_id") != request_id:
            continue
        out.append(row)
    return out


@app.get("/spend/tags")
async def spend_tags(
    start_date: str = "",
    end_date: str = "",
    authorization: str = Header(),
):
    """Return a tag-aggregated rollup. Mirrors LiteLLM's response shape
    closely enough that the dashboard's spot-checks against it work."""
    check_admin(authorization)
    by_tag: dict[str, dict] = {}
    for row in spend_logs_db:
        for tag in row.get("request_tags") or []:
            bucket = by_tag.setdefault(
                tag, {"individual_request_tag": tag, "spend": 0.0, "log_count": 0}
            )
            bucket["spend"] += float(row.get("spend") or 0)
            bucket["log_count"] += 1
    return list(by_tag.values())
