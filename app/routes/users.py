from fastapi import APIRouter, Request, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_session
from app.models.user import User

router = APIRouter(prefix="/api")


async def get_or_create_user(email: str, session: AsyncSession) -> User:
    """Get existing user or create on first request."""
    result = await session.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if user is None:
        user = User(email=email)
        session.add(user)
        await session.commit()
        await session.refresh(user)
    return user


@router.get("/me")
async def me(request: Request, session: AsyncSession = Depends(get_session)):
    user = await get_or_create_user(request.state.user_email, session)
    return {
        "id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "created_at": user.created_at.isoformat(),
    }
