from app.core.config import Settings


def create_test_app():
    """Create a FastAPI app configured for testing."""
    from fastapi import FastAPI
    from app.core.middleware import AuthMiddleware
    from app.routes.health import router as health_router
    from app.routes.users import router as users_router
    from app.routes.keys import router as keys_router

    settings = Settings(
        DEBUG_MODE=False,
        LITELLM_BASE_URL="http://mock-litellm:4000",
        LITELLM_ADMIN_KEY="sk-test-admin-key",
    )

    # Patch get_settings to return test settings
    import app.core.config as config_mod
    config_mod.settings = settings

    app = FastAPI()
    app.state.settings = settings
    app.add_middleware(AuthMiddleware, settings=settings)
    app.include_router(health_router)
    app.include_router(users_router)
    app.include_router(keys_router)

    return app
