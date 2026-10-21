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
    assert s.SHOW_PROJECT_TASK_BREAKDOWN is True
    # Teams feature is opt-in.
    assert s.FEATURE_TEAMS_ENABLED is False


def test_feature_teams_enabled_toggle():
    s = Settings(LITELLM_ADMIN_KEY="sk-test", FEATURE_TEAMS_ENABLED=True)
    assert s.FEATURE_TEAMS_ENABLED is True


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


def test_trusted_origins_empty_by_default():
    s = Settings(LITELLM_ADMIN_KEY="sk-test")
    assert s.trusted_origins == []


def test_trusted_origins_parsing_and_normalization():
    s = Settings(
        LITELLM_ADMIN_KEY="sk-test",
        TRUSTED_ORIGINS=(
            "https://App.Example.COM, "
            "https://staging.example.com:8443/some/path, "
            "http://localhost:3000"
        ),
    )
    # Host lowercased; path stripped; explicit ports preserved.
    assert s.trusted_origins == [
        "https://app.example.com",
        "https://staging.example.com:8443",
        "http://localhost:3000",
    ]


def test_trusted_origins_skips_malformed_entries():
    s = Settings(
        LITELLM_ADMIN_KEY="sk-test",
        # No scheme, empty entries, bare hostnames - all skipped.
        TRUSTED_ORIGINS="example.com,, ,https://good.example.com,not-a-url",
    )
    assert s.trusted_origins == ["https://good.example.com"]


def test_blank_max_active_keys_means_no_limit(monkeypatch):
    # .env.example leaves it blank; that must start the app, not crash it.
    monkeypatch.setenv("MAX_ACTIVE_KEYS_PER_USER", "")
    assert Settings(LITELLM_ADMIN_KEY="sk-test", _env_file=None).MAX_ACTIVE_KEYS_PER_USER is None
    monkeypatch.setenv("MAX_ACTIVE_KEYS_PER_USER", "5")
    assert Settings(LITELLM_ADMIN_KEY="sk-test", _env_file=None).MAX_ACTIVE_KEYS_PER_USER == 5


def test_env_example_loads(tmp_path, monkeypatch):
    # Copying .env.example to .env, as the README says, must give valid settings.
    import shutil
    from pathlib import Path

    shutil.copy(Path(__file__).parent.parent / ".env.example", tmp_path / ".env")
    monkeypatch.chdir(tmp_path)
    for name in list(Settings.model_fields):
        monkeypatch.delenv(name, raising=False)
    s = Settings()
    assert s.MAX_ACTIVE_KEYS_PER_USER is None
