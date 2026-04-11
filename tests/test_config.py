from app.core.config import Settings


def test_default_settings():
    s = Settings(DEBUG_MODE=False, LITELLM_ADMIN_KEY="sk-test")
    assert s.DEBUG_MODE is False
    assert s.AUTH_USER_HEADER == "X-User-Email"
    assert s.TEST_USER == "test@test.com"
    assert s.LITELLM_BASE_URL == "http://localhost:4000"
    assert s.FEATURE_PROXY_SECRET_ENABLED is False


def test_debug_mode_enabled():
    s = Settings(DEBUG_MODE=True, LITELLM_ADMIN_KEY="sk-test")
    assert s.DEBUG_MODE is True


def test_nav_links_empty_by_default():
    s = Settings(LITELLM_ADMIN_KEY="sk-test")
    assert s.nav_links == []


def test_nav_links_parsing():
    s = Settings(
        LITELLM_ADMIN_KEY="sk-test",
        NAV_LINKS="Docs|https://docs.example.com,Support|https://support.example.com",
    )
    assert s.nav_links == [
        {"name": "Docs", "url": "https://docs.example.com"},
        {"name": "Support", "url": "https://support.example.com"},
    ]


def test_nav_links_strips_whitespace():
    s = Settings(
        LITELLM_ADMIN_KEY="sk-test",
        NAV_LINKS=" Docs | https://docs.example.com , Support | https://support.example.com ",
    )
    assert s.nav_links == [
        {"name": "Docs", "url": "https://docs.example.com"},
        {"name": "Support", "url": "https://support.example.com"},
    ]


def test_nav_links_preserves_url_characters():
    """URLs with ':', '=', and '?' must survive parsing."""
    s = Settings(
        LITELLM_ADMIN_KEY="sk-test",
        NAV_LINKS="Search|https://example.com:8080/q?a=1&b=2",
    )
    assert s.nav_links == [
        {"name": "Search", "url": "https://example.com:8080/q?a=1&b=2"},
    ]


def test_nav_links_skips_malformed_entries():
    s = Settings(
        LITELLM_ADMIN_KEY="sk-test",
        NAV_LINKS="NoSeparator,Good|https://good.example.com,|https://noname.com,Empty|",
    )
    assert s.nav_links == [
        {"name": "Good", "url": "https://good.example.com"},
    ]
