import os

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.database import Base, get_session
from app.core.config import Settings

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://signup:signup@localhost:5433/signup_app",
)


async def create_test_app():
    """Create a FastAPI app configured for testing with PostgreSQL.

    Creates fresh tables each time. Must be awaited.
    """
    from fastapi import FastAPI
    from app.core.middleware import AuthMiddleware
    from app.routes.health import router as health_router
    from app.routes.users import router as users_router
    from app.routes.keys import router as keys_router

    settings = Settings(
        DATABASE_URL=TEST_DB_URL,
        DEBUG_MODE=False,
    )

    test_engine = create_async_engine(TEST_DB_URL)
    test_session_factory = async_sessionmaker(test_engine, expire_on_commit=False)

    # Setup tables
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    app = FastAPI()
    app.state.settings = settings
    app.add_middleware(AuthMiddleware, settings=settings)
    app.include_router(health_router)
    app.include_router(users_router)
    app.include_router(keys_router)

    async def override_get_session():
        async with test_session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session

    return app
