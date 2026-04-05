from fastapi import APIRouter, Request

from app.core.config import get_settings

router = APIRouter(prefix="/api")


@router.get("/me")
async def me(request: Request):
    s = get_settings()
    return {
        "email": request.state.user_email,
        "auth_mode": s.AUTH_MODE,
    }
