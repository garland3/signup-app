from fastapi import APIRouter, Request

router = APIRouter(prefix="/api")


@router.get("/me")
async def me(request: Request):
    return {"email": request.state.user_email}
