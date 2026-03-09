from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Core
    DEBUG_MODE: bool = False
    DATABASE_URL: str

    # Auth
    AUTH_USER_HEADER: str = "X-User-Email"
    TEST_USER: str = "test@test.com"
    PROXY_SECRET_HEADER: str = "X-Proxy-Secret"
    PROXY_SECRET: str = ""
    FEATURE_PROXY_SECRET_ENABLED: bool = False

    # Feature Flags
    FEATURE_KEY_LAST_USED: bool = False
    FEATURE_KEY_EXPIRATION: bool = False
    FEATURE_KEY_RATE_LIMIT: bool = False
    FEATURE_KEY_SCOPES: bool = False

    model_config = {"env_file": ".env", "extra": "ignore"}


settings: Settings | None = None


def get_settings() -> Settings:
    global settings
    if settings is None:
        settings = Settings()
    return settings
