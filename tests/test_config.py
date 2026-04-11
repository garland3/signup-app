from app.core.config import Settings


def test_default_settings():
    s = Settings(DEBUG_MODE=False, LITELLM_ADMIN_KEY="sk-test")
    assert s.DEBUG_MODE is False
    assert s.AUTH_USER_HEADER == "X-User-Email"
    assert s.TEST_USER == "test@test.com"
    assert s.LITELLM_BASE_URL == "http://localhost:4000"
    # Secure-by-default: shared proxy secret is required.
    assert s.FEATURE_PROXY_SECRET_ENABLED is True
    # Dev-only bypass flags default off.
    assert s.ALLOW_TEST_USER is False
    assert s.ALLOW_INSECURE_STARTUP is False


def test_debug_mode_enabled():
    s = Settings(DEBUG_MODE=True, LITELLM_ADMIN_KEY="sk-test")
    assert s.DEBUG_MODE is True
