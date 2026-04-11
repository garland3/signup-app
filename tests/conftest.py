"""Pytest root conftest.

Important: this module is loaded before test files are imported. We set
environment variables here so that when test modules do
``import app.main as main_mod`` the module-level ``app = create_app()``
call passes the startup safety guard with test-appropriate defaults.
"""
import os

# Provide a shared proxy secret at the env level so the module-level
# ``app = create_app()`` in app.main passes the startup safety guard when
# pytest imports it. Individual tests override via explicit Settings(...)
# kwargs so they exercise whatever configuration they want.
os.environ.setdefault("DEBUG_MODE", "false")
os.environ.setdefault("PROXY_SECRET", "test-suite-proxy-secret")
os.environ.setdefault("LITELLM_ADMIN_KEY", "sk-test-admin-key")

from app.core.config import Settings  # noqa: E402
from app.core.rate_limit import limiter  # noqa: E402


def create_test_app(*, strip_user_domain: bool = False):
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
        STRIP_USER_DOMAIN=strip_user_domain,
        # Tests drive proxy mode with a known header and no shared secret.
        FEATURE_PROXY_SECRET_ENABLED=False,
        ALLOW_INSECURE_STARTUP=True,
    )

    # Patch get_settings to return test settings
    import app.core.config as config_mod
    config_mod.settings = settings

    # Reset the in-memory rate limiter between test apps so per-user caps
    # from one test don't leak into another.
    limiter.reset()

    app = FastAPI()
    app.state.settings = settings
    app.add_middleware(AuthMiddleware, settings=settings)
    app.include_router(health_router)
    app.include_router(users_router)
    app.include_router(keys_router)

    return app
