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
from datetime import datetime, timezone

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
    return users_db[user_id]


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
