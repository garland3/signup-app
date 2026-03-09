from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.core.config import get_settings
from app.core.middleware import AuthMiddleware
from app.database import init_db, create_tables
from app.routes.health import router as health_router
from app.routes.users import router as users_router
from app.routes.keys import router as keys_router

STATIC_DIR = Path(__file__).parent.parent / "static"


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

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)))

        @app.get("/")
        async def serve_frontend():
            return FileResponse(STATIC_DIR / "index.html")

    return app


app = create_app()
