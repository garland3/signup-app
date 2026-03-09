from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.core.config import get_settings
from app.core.middleware import AuthMiddleware
from app.database import init_db, create_tables
from app.routes.health import router as health_router
from app.routes.users import router as users_router
from app.routes.keys import router as keys_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    init_db(settings.DATABASE_URL)
    await create_tables()
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Signup App", lifespan=lifespan)
    app.state.settings = settings
    app.add_middleware(AuthMiddleware, settings=settings)
    app.include_router(health_router)
    app.include_router(users_router)
    app.include_router(keys_router)

    static_dir = Path(__file__).parent.parent / "static"
    if static_dir.exists():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True))

    return app


app = create_app()
