from datetime import datetime, timezone

from fastapi import APIRouter, Request, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_session
from app.models.api_key import APIKey
from app.models.user import User
from app.core.security import generate_api_key, hash_key
from app.routes.users import get_or_create_user

router = APIRouter(prefix="/api")


class CreateKeyRequest(BaseModel):
    name: str


class UpdateKeyRequest(BaseModel):
    name: str | None = None


class VerifyKeyRequest(BaseModel):
    key: str


def _key_response(key: APIKey, include_full_key: str | None = None) -> dict:
    data = {
        "id": key.id,
        "name": key.name,
        "prefix": key.prefix,
        "created_at": key.created_at.isoformat(),
        "is_active": key.is_active,
    }
    if include_full_key:
        data["key"] = include_full_key
    if key.last_used_at:
        data["last_used_at"] = key.last_used_at.isoformat()
    if key.revoked_at:
        data["revoked_at"] = key.revoked_at.isoformat()
    if key.expires_at:
        data["expires_at"] = key.expires_at.isoformat()
    return data


@router.post("/keys", status_code=201)
async def create_key(
    body: CreateKeyRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    user = await get_or_create_user(request.state.user_email, session)
    full_key, prefix = generate_api_key()
    api_key = APIKey(
        user_id=user.id,
        name=body.name,
        prefix=prefix,
        key_hash=hash_key(full_key),
    )
    session.add(api_key)
    await session.commit()
    await session.refresh(api_key)
    return _key_response(api_key, include_full_key=full_key)


@router.get("/keys")
async def list_keys(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    user = await get_or_create_user(request.state.user_email, session)
    result = await session.execute(
        select(APIKey)
        .where(APIKey.user_id == user.id)
        .order_by(APIKey.created_at.desc())
    )
    keys = result.scalars().all()
    return [_key_response(k) for k in keys]


@router.patch("/keys/{key_id}")
async def update_key(
    key_id: str,
    body: UpdateKeyRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    user = await get_or_create_user(request.state.user_email, session)
    result = await session.execute(
        select(APIKey).where(APIKey.id == key_id, APIKey.user_id == user.id)
    )
    api_key = result.scalar_one_or_none()
    if not api_key:
        raise HTTPException(status_code=404, detail="Key not found")
    if body.name is not None:
        api_key.name = body.name
    await session.commit()
    await session.refresh(api_key)
    return _key_response(api_key)


@router.delete("/keys/{key_id}")
async def revoke_key(
    key_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    user = await get_or_create_user(request.state.user_email, session)
    result = await session.execute(
        select(APIKey).where(APIKey.id == key_id, APIKey.user_id == user.id)
    )
    api_key = result.scalar_one_or_none()
    if not api_key:
        raise HTTPException(status_code=404, detail="Key not found")
    api_key.is_active = False
    api_key.revoked_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(api_key)
    return _key_response(api_key)


@router.post("/keys/verify")
async def verify_key(
    body: VerifyKeyRequest,
    session: AsyncSession = Depends(get_session),
):
    key_hash_value = hash_key(body.key)
    result = await session.execute(
        select(APIKey).where(APIKey.key_hash == key_hash_value)
    )
    api_key = result.scalar_one_or_none()

    if not api_key or not api_key.is_active:
        return {"valid": False}

    if api_key.expires_at and api_key.expires_at < datetime.now(timezone.utc):
        return {"valid": False}

    user_result = await session.execute(
        select(User).where(User.id == api_key.user_id)
    )
    user = user_result.scalar_one()

    return {"valid": True, "user_email": user.email, "key_name": api_key.name}
