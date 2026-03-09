from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Core
    DEBUG_MODE: bool = False

    # LiteLLM proxy connection
    LITELLM_BASE_URL: str = "http://localhost:4000"
    LITELLM_ADMIN_KEY: str = ""

    # Auth
    AUTH_USER_HEADER: str = "X-User-Email"
    TEST_USER: str = "test@test.com"
    PROXY_SECRET_HEADER: str = "X-Proxy-Secret"
    PROXY_SECRET: str = ""
    FEATURE_PROXY_SECRET_ENABLED: bool = False

    model_config = {"env_file": ".env", "extra": "ignore"}


settings: Settings | None = None


def get_settings() -> Settings:
    global settings
    if settings is None:
        settings = Settings()
    return settings
