from app.core.config import Settings


def test_default_settings():
    s = Settings(DATABASE_URL="sqlite+aiosqlite:///test.db", DEBUG_MODE=False)
    assert s.DEBUG_MODE is False
    assert s.AUTH_USER_HEADER == "X-User-Email"
    assert s.TEST_USER == "test@test.com"
    assert s.FEATURE_KEY_LAST_USED is False
    assert s.FEATURE_KEY_EXPIRATION is False
    assert s.FEATURE_KEY_RATE_LIMIT is False
    assert s.FEATURE_KEY_SCOPES is False
    assert s.FEATURE_PROXY_SECRET_ENABLED is False


def test_debug_mode_enabled():
    s = Settings(DATABASE_URL="sqlite+aiosqlite:///test.db", DEBUG_MODE=True)
    assert s.DEBUG_MODE is True
